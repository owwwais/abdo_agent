"""محولات مزودي النماذج بالـSDKs الرسمية. كل محول يعيد JSON نصيًا مقيدًا بمخطط، وعدد التوكنات.

- Anthropic: Messages API مع output_config.format (json_schema). على claude-opus-5 وclaude-fable-5-1
  نفعّل fallbacks الخادمية "default" لرفض السياسات (beta server-side-fallback-2026-07-01).
- OpenAI: Responses API مع text.format (json_schema, strict).
- متوافق مع OpenAI (OpenRouter وغيره): Chat Completions مع response_format.
- Gemini: google-genai مع response_json_schema.
- Fake: اصطناعي حتمي للاختبار وبيئة demo؛ لا شبكة ولا تكلفة.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """خطأ من المزود. retryable=True للأخطاء المؤقتة (شبكة، 429، 5xx)."""

    def __init__(
        self, message: str, *, retryable: bool = False, code: str = "provider_error"
    ) -> None:
        self.retryable = retryable
        self.code = code
        super().__init__(message)


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    served_by: str | None = None


class ModelClient(Protocol):
    name: str

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderResult: ...

    async def list_models(self) -> list[str]: ...


_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")


class AnthropicClient:
    name = "anthropic"

    def __init__(self, api_key: str, *, http_client: Any = None) -> None:
        import anthropic

        self._sdk = anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=2, timeout=180.0, http_client=http_client
        )

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderResult:
        a = self._sdk
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        try:
            if model in _FALLBACK_MODELS:
                resp = await self._client.beta.messages.create(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                resp = await self._client.messages.create(**kwargs)
        except a.AuthenticationError as exc:
            raise ProviderError("مفتاح Anthropic غير صالح", code="auth") from exc
        except a.PermissionDeniedError as exc:
            raise ProviderError("المفتاح لا يملك صلاحية هذا النموذج", code="permission") from exc
        except a.NotFoundError as exc:
            raise ProviderError(f"النموذج {model} غير موجود", code="model_not_found") from exc
        except a.BadRequestError as exc:
            raise ProviderError(
                f"طلب مرفوض من Anthropic: {exc.message[:200]}", code="bad_request"
            ) from exc
        except a.RateLimitError as exc:
            raise ProviderError(
                "تجاوز حد الطلبات لدى Anthropic", retryable=True, code="rate_limit"
            ) from exc
        except a.APIStatusError as exc:
            raise ProviderError(
                f"خطأ خادم Anthropic ({exc.status_code})", retryable=exc.status_code >= 500
            ) from exc
        except a.APIConnectionError as exc:
            raise ProviderError("تعذر الاتصال بـAnthropic", retryable=True, code="network") from exc
        if resp.stop_reason == "refusal":
            raise ProviderError("رفض النموذج الطلب لأسباب سياسة", code="refusal")
        if resp.stop_reason == "max_tokens":
            raise ProviderError(
                "انقطع الرد عند حد التوكنات؛ ارفع الحد الأقصى في الإعدادات", code="truncated"
            )
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return ProviderResult(
            text=text,
            input_tokens=int(resp.usage.input_tokens or 0),
            output_tokens=int(resp.usage.output_tokens or 0),
            model=model,
            served_by=getattr(resp, "model", model),
        )

    async def list_models(self) -> list[str]:
        try:
            return [m.id async for m in self._client.models.list(limit=100)]
        except self._sdk.AuthenticationError as exc:
            raise ProviderError("مفتاح Anthropic غير صالح", code="auth") from exc
        except self._sdk.APIError as exc:
            raise ProviderError(f"تعذر جلب نماذج Anthropic: {exc}", retryable=True) from exc


class OpenAIClient:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        compatible: bool = False,
        http_client: Any = None,
    ) -> None:
        import openai

        self._sdk = openai
        self._compatible = compatible
        self.name = "openai_compatible" if compatible else "openai"
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            max_retries=2,
            timeout=180.0,
            http_client=http_client,
        )

    def _map(self, exc: Exception) -> ProviderError:
        o = self._sdk
        if isinstance(exc, o.AuthenticationError):
            return ProviderError("مفتاح المزود غير صالح", code="auth")
        if isinstance(exc, o.NotFoundError):
            return ProviderError("النموذج غير موجود لدى المزود", code="model_not_found")
        if isinstance(exc, o.RateLimitError):
            return ProviderError(
                "تجاوز حد الطلبات أو الرصيد لدى المزود", retryable=True, code="rate_limit"
            )
        if isinstance(exc, o.BadRequestError):
            return ProviderError(f"طلب مرفوض: {str(exc)[:200]}", code="bad_request")
        if isinstance(exc, o.APIStatusError):
            return ProviderError(
                f"خطأ خادم المزود ({exc.status_code})", retryable=exc.status_code >= 500
            )
        if isinstance(exc, o.APIConnectionError):
            return ProviderError("تعذر الاتصال بالمزود", retryable=True, code="network")
        return ProviderError(str(exc)[:200])

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderResult:
        try:
            if self._compatible:
                return await self._chat(model, system, user, schema, schema_name, max_output_tokens)
            resp = await self._client.responses.create(
                model=model,
                instructions=system,
                input=user,
                max_output_tokens=max_output_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
        except self._sdk.OpenAIError as exc:
            raise self._map(exc) from exc
        if getattr(resp, "status", "completed") == "incomplete":
            raise ProviderError(
                "انقطع الرد قبل اكتماله؛ ارفع الحد الأقصى للتوكنات", code="truncated"
            )
        usage = resp.usage
        return ProviderResult(
            text=resp.output_text or "",
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=model,
        )

    async def _chat(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
    ) -> ProviderResult:
        messages: Any = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
            )
        except self._sdk.BadRequestError:
            # بعض المزودين المتوافقين لا يدعمون json_schema؛ نطلب JSON عامًا ونتحقق من المخطط محليًا.
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise ProviderError("انقطع الرد عند حد التوكنات", code="truncated")
        usage = resp.usage
        return ProviderResult(
            text=choice.message.content or "",
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            model=model,
        )

    async def list_models(self) -> list[str]:
        try:
            return sorted([m.id async for m in self._client.models.list()])
        except self._sdk.OpenAIError as exc:
            raise self._map(exc) from exc


class GeminiClient:
    name = "gemini"

    def __init__(self, api_key: str, *, http_client: Any = None) -> None:
        from google import genai
        from google.genai import types

        self._genai = genai
        options = (
            types.HttpOptions(httpx_async_client=http_client) if http_client is not None else None
        )
        self._client = genai.Client(api_key=api_key, http_options=options)

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderResult:
        from google.genai import errors, types

        try:
            resp = await self._client.aio.models.generate_content(
                model=model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    max_output_tokens=max_output_tokens,
                ),
            )
        except errors.ClientError as exc:
            code = "auth" if getattr(exc, "code", 0) in (401, 403) else "bad_request"
            retry = getattr(exc, "code", 0) == 429
            raise ProviderError(
                f"Gemini رفض الطلب: {str(exc)[:200]}", retryable=retry, code=code
            ) from exc
        except errors.ServerError as exc:
            raise ProviderError("خطأ خادم Gemini", retryable=True) from exc
        except errors.APIError as exc:
            raise ProviderError(f"خطأ Gemini: {str(exc)[:200]}") from exc
        meta = resp.usage_metadata
        output = int(getattr(meta, "candidates_token_count", 0) or 0) + int(
            getattr(meta, "thoughts_token_count", 0) or 0
        )
        return ProviderResult(
            text=resp.text or "",
            input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=output,
            model=model,
        )

    async def list_models(self) -> list[str]:
        from google.genai import errors

        try:
            out = []
            async for m in await self._client.aio.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if not actions or "generateContent" in actions:
                    out.append((m.name or "").removeprefix("models/"))
            return sorted(n for n in out if n)
        except errors.APIError as exc:
            raise ProviderError(f"تعذر جلب نماذج Gemini: {str(exc)[:200]}") from exc


FakeResponder = Callable[[str, dict[str, Any]], dict[str, Any]]


class FakeClient:
    """نموذج اصطناعي حتمي. يستخرج سياق JSON من نص المستخدم (بين وسمي <context>) ويمرره لمستجيب
    مسجل باسم المخطط. لا شبكة ولا تكلفة؛ مخرجاته موسومة كاصطناعية في السجلات."""

    name = "fake"
    responders: dict[str, FakeResponder] = {}

    def __init__(self, overrides: dict[str, FakeResponder] | None = None) -> None:
        self._overrides = overrides or {}

    @staticmethod
    def context_of(user: str) -> dict[str, Any]:
        start, end = user.find("<context>"), user.find("</context>")
        if start == -1 or end == -1:
            return {}
        try:
            data = json.loads(user[start + len("<context>") : end])
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderResult:
        responder = self._overrides.get(schema_name) or self.responders.get(schema_name)
        if responder is None:
            raise ProviderError(f"FakeModel لا يعرف المخطط {schema_name}", code="bad_request")
        data = responder(user, self.context_of(user))
        text = json.dumps(data, ensure_ascii=False)
        return ProviderResult(
            text=text, input_tokens=len(user) // 4, output_tokens=len(text) // 4, model="fake"
        )

    async def list_models(self) -> list[str]:
        return ["fake"]
