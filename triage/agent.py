"""Агентный режим: модель сама решает, какие инструменты вызвать, в пределах лимита шагов.

Инструменты те же, что у MCP-сервера, и с теми же ограничениями. Ошибка инструмента
(чужой сервис, слишком широкое окно) возвращается модели текстом — она может поправить запрос.
"""

from __future__ import annotations

import json

from triage.tools import TOOL_SPECS, LogTools

AGENT_SYSTEM = (
    "Ты агент разбора инцидентов на тестовом стенде. Доступны инструменты только для чтения логов и регламентов; "
    "время в логах — UTC. Сначала найди релевантные фрагменты, при необходимости посмотри строки лога вокруг ошибки "
    "и сверься с регламентом. В ответе по-русски: что произошло, вероятная причина, на каких строках это видно "
    "(сервис и номер строки), что проверить дальше. Не выдумывай того, чего нет в результатах инструментов."
)


def run_agent(question: str, tools: LogTools, llm, max_steps: int = 6, max_tool_chars: int = 6000) -> dict:
    messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM}, {"role": "user", "content": question}]
    steps: list[dict] = []
    for _ in range(max_steps):
        reply = llm.chat_tools(messages, TOOL_SPECS)
        if not reply["tool_calls"]:
            return {"answer": reply["content"] or "", "steps": steps}
        messages.append(
            {
                "role": "assistant",
                "content": reply["content"],
                "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in reply["tool_calls"]
                ],
            }
        )
        for call in reply["tool_calls"]:
            result = tools.dispatch(call["name"], call["arguments"])
            steps.append({"tool": call["name"], "arguments": call["arguments"], "result": result})
            content = json.dumps(result, ensure_ascii=False, default=str)
            if len(content) > max_tool_chars:
                content = content[:max_tool_chars] + "… (обрезано)"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
    return {"answer": "Достигнут лимит шагов агента — вывод не сформирован.", "steps": steps}
