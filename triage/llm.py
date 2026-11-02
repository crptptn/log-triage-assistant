"""Клиент LLM через OpenAI-совместимый API: Gemini, Ollama, vLLM, OpenRouter и т. п."""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    pass


def parse_json_object(text: str | None) -> dict | None:
    """JSON-объект из ответа модели: чистый JSON, блок ```json``` или первый {...}."""
    if not text:
        return None
    candidates = [text.strip(), *(m.strip() for m in _FENCE.findall(text))]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def schema_errors(schema: dict, data: dict) -> list[str]:
    from jsonschema import Draft202012Validator

    return [e.message for e in Draft202012Validator(schema).iter_errors(data)]


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY", timeout: float = 120.0):
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=2)

    def json(self, messages: list[dict], schema: dict, name: str) -> dict:
        """Ответ строго по JSON-схеме. Если провайдер не умеет json_schema — просим JSON текстом и проверяем сами."""
        from openai import BadRequestError

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
            )
        except BadRequestError:
            hint = "Ответь только JSON-объектом по этой схеме, без пояснений:\n" + json.dumps(
                schema, ensure_ascii=False
            )
            resp = self.client.chat.completions.create(
                model=self.model, messages=[*messages, {"role": "user", "content": hint}], temperature=0
            )
        data = parse_json_object(resp.choices[0].message.content)
        if data is None:
            raise LLMError("модель вернула не JSON")
        errors = schema_errors(schema, data)
        if errors:
            raise LLMError("ответ не соответствует схеме: " + "; ".join(errors[:3]))
        return data

    def chat_tools(self, messages: list[dict], tools: list[dict]) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=0
        )
        message = resp.choices[0].message
        return {
            "content": message.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
                for tc in (message.tool_calls or [])
            ],
        }


def make_llm(settings) -> LLM | None:
    if not settings.llm_enabled:
        return None
    return LLM(settings.llm_base_url, settings.llm_model, settings.llm_api_key)
