"""شكل الطلبات إلى مزودي النماذج عبر SDKs الرسمية مع نقل HTTP محاكى (لا شبكة ولا مفاتيح حقيقية)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import httpx2
import pytest

from app.agents.gateway import strict_schema
from app.agents.providers import AnthropicClient, GeminiClient, OpenAIClient, ProviderError
from app.services.connection_tests import Ping

SCHEMA = strict_schema(Ping)


def capture2(
    response: dict[str, Any], status: int = 200
) -> tuple[list[httpx2.Request], httpx2.AsyncClient]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=response)

    return seen, httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


ANTHROPIC_OK = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": '{"ok": true, "reply": "pong"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 7},
}


def test_strict_schema_is_closed_and_portable() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert SCHEMA["required"] == ["ok", "reply"]
    assert "maxLength" not in json.dumps(SCHEMA)


async def test_anthropic_opus5_uses_structured_output_and_default_fallbacks() -> None:
    seen, http = capture2(ANTHROPIC_OK)
    client = AnthropicClient("sk-test", http_client=http)
    res = await client.complete_json(
        model="claude-opus-5",
        system="s",
        user="u",
        schema=SCHEMA,
        schema_name="connection_ping",
        max_output_tokens=600,
    )
    assert res.text.startswith("{") and (res.input_tokens, res.output_tokens) == (12, 7)
    body = json.loads(seen[0].content)
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers.get("anthropic-beta", "")
    assert seen[0].headers["x-api-key"] == "sk-test"


async def test_anthropic_other_models_skip_fallback_beta() -> None:
    seen, http = capture2({**ANTHROPIC_OK, "model": "claude-sonnet-5"})
    client = AnthropicClient("sk-test", http_client=http)
    await client.complete_json(
        model="claude-sonnet-5",
        system="s",
        user="u",
        schema=SCHEMA,
        schema_name="p",
        max_output_tokens=600,
    )
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body and "anthropic-beta" not in seen[0].headers


async def test_anthropic_refusal_and_truncation_are_errors() -> None:
    for stop, code in (("refusal", "refusal"), ("max_tokens", "truncated")):
        _, http = capture2({**ANTHROPIC_OK, "stop_reason": stop})
        with pytest.raises(ProviderError) as err:
            await AnthropicClient("k", http_client=http).complete_json(
                model="claude-opus-5",
                system="s",
                user="u",
                schema=SCHEMA,
                schema_name="p",
                max_output_tokens=10,
            )
        assert err.value.code == code


async def test_anthropic_auth_error_mapped() -> None:
    _, http = capture2(
        {"type": "error", "error": {"type": "authentication_error", "message": "bad"}}, status=401
    )
    with pytest.raises(ProviderError) as err:
        await AnthropicClient("bad", http_client=http).complete_json(
            model="claude-opus-5",
            system="s",
            user="u",
            schema=SCHEMA,
            schema_name="p",
            max_output_tokens=10,
        )
    assert err.value.code == "auth" and not err.value.retryable


async def test_openai_responses_api_json_schema() -> None:
    seen, http = capture2(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-x",
            "output": [
                {
                    "type": "message",
                    "id": "m1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"ok": true, "reply": "pong"}',
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 9,
                "output_tokens": 4,
                "total_tokens": 13,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }
    )
    res = await OpenAIClient("sk-o", http_client=http).complete_json(
        model="gpt-x",
        system="sys",
        user="usr",
        schema=SCHEMA,
        schema_name="connection_ping",
        max_output_tokens=500,
    )
    body = json.loads(seen[0].content)
    assert seen[0].url.path.endswith("/responses")
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "connection_ping",
        "schema": SCHEMA,
        "strict": True,
    }
    assert body["instructions"] == "sys" and res.output_tokens == 4


async def test_openai_compatible_uses_chat_completions_with_base_url() -> None:
    seen, http = capture2(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok": true, "reply": "pong"}'},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
    )
    client = OpenAIClient(
        "k", base_url="https://router.example/api/v1", compatible=True, http_client=http
    )
    res = await client.complete_json(
        model="m", system="s", user="u", schema=SCHEMA, schema_name="p", max_output_tokens=100
    )
    assert str(seen[0].url).startswith("https://router.example/api/v1/chat/completions")
    assert json.loads(seen[0].content)["response_format"]["type"] == "json_schema"
    assert (res.input_tokens, res.output_tokens) == (5, 3)


async def test_gemini_generate_content_json_schema() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": '{"ok": true, "reply": "pong"}'}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 7,
                    "candidatesTokenCount": 3,
                    "thoughtsTokenCount": 2,
                },
            },
        )

    client = GeminiClient(
        "g-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    res = await client.complete_json(
        model="gemini-x",
        system="s",
        user="u",
        schema=SCHEMA,
        schema_name="p",
        max_output_tokens=100,
    )
    body = json.loads(seen[0].content)
    assert ":generateContent" in seen[0].url.path
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["responseJsonSchema"] == SCHEMA
    assert (res.input_tokens, res.output_tokens) == (7, 5)
