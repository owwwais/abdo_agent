"""ModelGateway: واجهة موحدة لأدوار extractor وwriter فوق المزود الذي يختاره المالك.

المسؤوليات البرمجية (لا النموذج): التحقق من المخطط، محاولة تصحيح محدودة تُحسب ضمن حد الاستدعاءات،
حجز الميزانية قبل الطلب وتسويتها بعده، وتسجيل provider/model/prompt_version دون أسرار أو سلسلة تفكير.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.providers import (
    AnthropicClient,
    FakeClient,
    GeminiClient,
    ModelClient,
    OpenAIClient,
    ProviderError,
)
from app.api.errors import AppError
from app.config import Settings
from app.db.models import Workspace
from app.services import budget
from app.services import integrations as integ
from app.services.secrets import get_secret

Role = Literal["extractor", "writer"]
T = TypeVar("T", bound=BaseModel)
MILLION = Decimal(1_000_000)

_KEY_FOR = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
    "openai_compatible": "openai_compatible_api_key",
}


class ModelNotReady(AppError):
    status_code = 409
    code = "model_not_ready"
    message = "النموذج غير مهيأ"


class ModelOutputInvalid(AppError):
    status_code = 502
    code = "model_output_invalid"
    message = "مخرجات النموذج لم تطابق المخطط بعد محاولة التصحيح"


class CallLimitReached(AppError):
    status_code = 409
    code = "model_call_limit"
    message = "بلغت المعالجة حد استدعاءات النموذج المسموح"


@dataclass
class CallCounter:
    limit: int
    used: int = 0

    def take(self) -> None:
        if self.used >= self.limit:
            raise CallLimitReached(f"حد استدعاءات النموذج لهذه المهمة ({self.limit})")
        self.used += 1


@dataclass
class ResolvedModel:
    provider: str
    model: str
    client: ModelClient
    price: integ.ModelPrice | None
    max_output_tokens: int


@dataclass
class ModelUsage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost: Decimal = Decimal("0")
    attempts_log: list[str] = field(default_factory=list)


ClientFactory = Callable[[str, str, integ.ModelsConfig], ModelClient]


def default_client_factory(provider: str, api_key: str, cfg: integ.ModelsConfig) -> ModelClient:
    if provider == "anthropic":
        return AnthropicClient(api_key)
    if provider == "openai":
        return OpenAIClient(api_key)
    if provider == "openai_compatible":
        return OpenAIClient(api_key, base_url=cfg.openai_compatible_base_url, compatible=True)
    if provider == "gemini":
        return GeminiClient(api_key)
    return FakeClient()


def estimate_cost(price: integ.ModelPrice | None, input_tokens: int, output_tokens: int) -> Decimal:
    if price is None:
        return Decimal("0")
    return (
        Decimal(input_tokens) * price.input_per_mtok
        + Decimal(output_tokens) * price.output_per_mtok
    ) / MILLION


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: uuid.UUID,
        *,
        client_factory: ClientFactory | None = None,
        fake_client: FakeClient | None = None,
    ) -> None:
        self.settings = settings
        self.sm = sessionmaker
        self.workspace_id = workspace_id
        self._factory = client_factory or default_client_factory
        self._fake = fake_client or FakeClient()

    async def resolve(self, role: Role) -> ResolvedModel:
        async with self.sm() as db:
            cfg = await integ.get_config(
                db, self.settings, self.workspace_id, "models", integ.ModelsConfig
            )
            role_cfg = cfg.extractor if role == "extractor" else cfg.writer
            if integ.forced_fake(self.settings) or role_cfg.provider == "fake":
                return ResolvedModel("fake", "fake", self._fake, None, cfg.max_output_tokens)
            if not role_cfg.model:
                raise ModelNotReady(f"اختر نموذجًا لدور {role} في الإعدادات")
            if not cfg.processing_approved:
                raise ModelNotReady(
                    "أكد اعتماد سياسة معالجة البيانات لدى مزود النموذج في الإعدادات قبل إرسال أي محتوى له",
                    code="processing_not_approved",
                )
            price = cfg.price_for(role_cfg.provider, role_cfg.model)
            if price is None:
                raise ModelNotReady(
                    f"سعر النموذج {role_cfg.model} غير معرّف؛ أدخله في الإعدادات (لا طلبات بتكلفة مجهولة)",
                    code="price_unknown",
                )
            api_key, _ = await get_secret(
                db, self.settings, self.workspace_id, _KEY_FOR[role_cfg.provider]
            )
            if not api_key:
                raise ModelNotReady(f"مفتاح {role_cfg.provider} غير مضبوط", code="api_key_missing")
            client = self._factory(role_cfg.provider, api_key, cfg)
            return ResolvedModel(
                role_cfg.provider, role_cfg.model, client, price, cfg.max_output_tokens
            )

    async def complete(
        self,
        *,
        role: Role,
        system: str,
        user: str,
        output_model: type[T],
        schema_name: str,
        category: str,
        counter: CallCounter,
        run_id: uuid.UUID | None = None,
        opportunity_id: uuid.UUID | None = None,
        prompt_version: str = "v1",
        max_output_tokens: int | None = None,
    ) -> tuple[T, ModelUsage]:
        resolved = await self.resolve(role)
        if max_output_tokens:
            resolved.max_output_tokens = min(resolved.max_output_tokens, max_output_tokens)
        schema = strict_schema(output_model)
        usage = ModelUsage(provider=resolved.provider, model=resolved.model)
        prompt = user
        last_error = ""
        for attempt in range(2):  # المحاولة الأصلية + تصحيح واحد
            counter.take()
            text = await self._call(
                resolved,
                system,
                prompt,
                schema,
                schema_name,
                category,
                run_id,
                opportunity_id,
                role,
                usage,
                prompt_version,
            )
            try:
                return output_model.model_validate_json(text), usage
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:600]
                usage.attempts_log.append(f"attempt {attempt + 1}: invalid output")
                prompt = (
                    f"{user}\n\nالرد السابق لم يطابق المخطط المطلوب. الأخطاء:\n{last_error}\n"
                    "أعد الرد JSON صالحًا يطابق المخطط تمامًا."
                )
        raise ModelOutputInvalid(details={"error": last_error})

    async def _call(
        self,
        resolved: ResolvedModel,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        category: str,
        run_id: uuid.UUID | None,
        opportunity_id: uuid.UUID | None,
        role: str,
        usage: ModelUsage,
        prompt_version: str,
    ) -> str:
        # الحد الأعلى: كل المدخلات (تقدير محافظ للعربية) + الحد الأقصى للمخرجات.
        upper = estimate_cost(
            resolved.price, len(system + user) // 2 + 50, resolved.max_output_tokens
        )
        async with self.sm() as db, db.begin():
            ws = await db.get(Workspace, self.workspace_id)
            tz = ws.timezone if ws else self.settings.app_timezone
            ops = await integ.get_config(
                db, self.settings, self.workspace_id, "operations", integ.OperationsConfig
            )
            reservation = await budget.reserve(
                db,
                self.workspace_id,
                ops,
                amount=upper,
                category=category,
                run_id=run_id,
                tz_name=tz,
            )
        try:
            result = await resolved.client.complete_json(
                model=resolved.model,
                system=system,
                user=user,
                schema=schema,
                schema_name=schema_name,
                max_output_tokens=resolved.max_output_tokens,
            )
        except ProviderError:
            async with self.sm() as db, db.begin():
                await budget.release(db, reservation)
            raise
        cost = estimate_cost(resolved.price, result.input_tokens, result.output_tokens)
        async with self.sm() as db, db.begin():
            await budget.settle(db, reservation, cost)
            budget.record_usage(
                db,
                workspace_id=self.workspace_id,
                run_id=run_id,
                opportunity_id=opportunity_id,
                category=category,
                kind="model",
                provider=resolved.provider,
                model=resolved.model,
                role=role,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                requests=1,
                estimated_cost=cost,
                currency=resolved.price.currency if resolved.price else "USD",
                pricing_version=(resolved.price.pricing_version if resolved.price else "fake")
                + f"|{prompt_version}",
            )
        usage.input_tokens += result.input_tokens
        usage.output_tokens += result.output_tokens
        usage.calls += 1
        usage.cost += cost
        return result.text


_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "additionalProperties",
    "description",
    "anyOf",
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """مخطط JSON صارم (additionalProperties=false وكل الحقول مطلوبة) كما تشترطه المخرجات المقيدة."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].split("/")[-1]
                return fix(json.loads(json.dumps(defs[ref])))
            # نبقي الكلمات المدعومة عند كل المزودين؛ القيود الدقيقة (الأطوال والمدى) يتحقق منها pydantic محليًا.
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key not in _SCHEMA_KEYS:
                    continue
                if key == "properties":
                    out[key] = {name: fix(sub) for name, sub in value.items()}
                else:
                    out[key] = fix(value)
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(schema)
