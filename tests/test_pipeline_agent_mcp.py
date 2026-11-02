import asyncio
import json

import pytest

from tests.conftest import completion
from triage.agent import run_agent
from triage.evaluate import evaluate
from triage.llm import LLM, LLMError, parse_json_object
from triage.pipeline import classify_rules, extract_time, triage


def _incident(incidents, category):
    return next(inc for inc in incidents if inc["category"] == category)


def test_time_is_extracted_from_description(stand):
    assert extract_time("Тест упал около 11:40, принтер молчит", stand).strftime("%H:%M") == "11:40"
    assert extract_time("Тест упал утром", stand) is None


def test_rules_mode_finds_category_and_evidence(stand, incidents):
    inc = _incident(incidents, "usb_device")
    result = triage(inc["title"], stand)
    assert result.mode == "rules"
    assert result.category == "usb_device"
    assert result.evidence and all(e in {c["chunk_id"] for c in result.chunks} for e in result.evidence)
    assert "### Фрагменты логов" in result.bug_report and "LIBUSB_ERROR_NO_DEVICE" in result.bug_report


def test_llm_mode_drops_invented_evidence(stand, incidents, fake_llm):
    inc = _incident(incidents, "db_pool")
    llm = fake_llm(
        analysis={
            "category": "db_pool",
            "confidence": 0.9,
            "summary": "Пул соединений к БД исчерпан",
            "root_cause": "Соединения не возвращаются в пул",
            "evidence": ["trace:придуманный", "report-service:1-2"],
            "next_steps": ["Проверить pg_stat_activity"],
        }
    )
    result = triage(inc["title"], stand, llm=llm)
    assert result.mode == "llm" and result.category == "db_pool"
    assert "trace:придуманный" not in result.evidence
    prompt = llm.calls[0][1]["content"]
    assert "Фрагменты логов" in prompt and "PoolTimeout" in prompt


def test_llm_failure_falls_back_to_rules(stand, incidents, fake_llm):
    inc = _incident(incidents, "disk_full")
    result = triage(inc["title"], stand, llm=fake_llm(error=LLMError("не JSON")))
    assert result.mode.startswith("rules (LLM недоступна")
    assert result.category == "disk_full"


def test_classify_rules_without_signals():
    assert classify_rules([{"text": "всё хорошо"}]) == ("other", 0.0)


def test_evaluation_report(stand):
    report = evaluate(stand)
    summary = report["summary"]
    assert summary["n_incidents"] == 16
    assert 0 <= summary["retrieval"]["no_time"]["mrr"] <= summary["retrieval"]["with_time"]["hit@6"] <= 1
    assert summary["classification_accuracy"] >= 0.8  # правила на синтетике — нижняя планка, не цель
    assert all(set(row) >= {"id", "rank_with_time", "correct"} for row in report["incidents"])


def test_agent_calls_tools_and_answers(stand, fake_llm):
    llm = fake_llm(
        tool_replies=[
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "1",
                        "name": "search_logs",
                        "arguments": '{"query": "printer not accessible", "errors_only": true}',
                    },
                    {
                        "id": "2",
                        "name": "get_log_window",
                        "arguments": '{"service": "device-agent", "start": "09:00", "end": "11:00"}',
                    },
                ],
            },
            {"content": "Принтер потерял права доступа после переподключения.", "tool_calls": []},
        ]  # fmt: skip
    )
    result = run_agent("Почему не печатает принтер?", stand, llm)
    assert result["answer"].startswith("Принтер")
    assert [s["tool"] for s in result["steps"]] == ["search_logs", "get_log_window"]
    assert "error" in result["steps"][1]["result"]  # окно 2 часа отклонено ограничением
    tool_messages = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert len(tool_messages) == 2 and "15 минут" in tool_messages[1]["content"]


def test_agent_step_limit(stand, fake_llm):
    call = {"content": None, "tool_calls": [{"id": "x", "name": "list_services", "arguments": "{}"}]}
    result = run_agent("?", stand, fake_llm(tool_replies=[call] * 3), max_steps=3)
    assert "лимит шагов" in result["answer"] and len(result["steps"]) == 3


def test_llm_json_falls_back_when_schema_mode_unsupported(openai_server):
    def responder(body):
        if "response_format" in body:
            return 400, {"error": {"message": "response_format is not supported", "type": "invalid_request_error"}}
        return 200, completion('```json\n{"answer": "ok"}\n```')

    openai_server.responder = responder
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    llm = LLM(openai_server.url, "fake")
    assert llm.json([{"role": "user", "content": "hi"}], schema, "t") == {"answer": "ok"}
    assert len(openai_server.requests) == 2
    openai_server.responder = lambda body: (200, completion('{"wrong": 1}'))
    with pytest.raises(LLMError, match="схеме"):
        llm.json([{"role": "user", "content": "hi"}], schema, "t")


def test_parse_json_object():
    assert parse_json_object('Вот: {"a": 1}.') == {"a": 1}
    assert parse_json_object("[1]") is None


def test_mcp_server_exposes_read_only_tools(stand):
    from triage import mcp_server

    mcp_server.set_tools(stand)

    async def go():
        tools = await mcp_server.server.list_tools()
        bad = await mcp_server.server.call_tool(
            "get_log_window", {"service": "/etc/passwd", "start": "09:00", "end": "09:01"}
        )
        ok = await mcp_server.server.call_tool("search_logs", {"query": "token expired", "limit": 2})
        return tools, bad, ok

    tools, bad, ok = asyncio.run(go())
    assert {t.name for t in tools} == {"list_services", "search_logs", "get_log_window", "search_runbooks"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    assert "неизвестный сервис" in bad.content[0].text
    assert len(json.loads(ok.content[0].text)["results"]) == 2
