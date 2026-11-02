import json

import pytest

from triage.synth import INCIDENTS, generate
from triage.tools import ToolError, parse_time


def test_generation_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a, seed=3)
    generate(b, seed=3)
    for path in (a / "logs").glob("*.log"):
        assert path.read_text() == (b / "logs" / path.name).read_text()


def test_incidents_point_to_real_lines(stand, incidents):
    assert {inc["category"] for inc in incidents} == set(INCIDENTS)
    for inc in incidents:
        assert inc["signature"]
        for service, line_no in inc["signature"]:
            line = (stand.settings.logs_dir / f"{service}.log").read_text().splitlines()[line_no - 1]
            assert service in line


def test_raw_logs_contain_secrets_that_tools_mask(stand):
    raw = (stand.settings.logs_dir / "report-service.log").read_text()
    assert "postgresql://report:" in raw and "<PASSWORD>" not in raw
    first = next(line for line in raw.splitlines() if "postgresql://" in line)
    hhmm = first[11:16]
    h, m = map(int, hhmm.split(":"))
    window = stand.get_log_window("report-service", f"{h:02d}:{m:02d}", f"{h:02d}:{min(m + 1, 59):02d}")
    assert any("<PASSWORD>" in line for line in window["lines"])
    assert not any("@example.ru" in line for line in json.dumps(window, ensure_ascii=False).splitlines())


def test_search_filters(stand):
    hits = stand.search_logs("printer label error", service="device-agent", errors_only=True, limit=3)["results"]
    assert hits and all("device-agent" in h["services"] for h in hits)
    assert "line_refs" not in hits[0]  # служебные поля наружу не отдаются
    timed = stand.search_logs("error", since="09:00", until="09:20", limit=10)["results"]
    assert all(h["start"] <= "2026-05-14T09:20" and h["end"] >= "2026-05-14T09:00" for h in timed)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda t: t.get_log_window("../../etc/passwd", "09:00", "09:05"), "неизвестный сервис"),
        (lambda t: t.get_log_window("report-service", "09:00", "10:00"), "окно больше 15 минут"),
        (lambda t: t.get_log_window("report-service", "09:10", "09:00"), "end не раньше start"),
        (lambda t: t.search_logs("ошибка", service="db-main"), "неизвестный сервис"),
        (lambda t: t.search_logs("  "), "пустой запрос"),
    ],
)
def test_tool_guards(stand, call, message):
    with pytest.raises(ToolError, match=message):
        call(stand)


def test_limits_are_capped(stand):
    assert len(stand.search_logs("error", limit=1000)["results"]) <= stand.settings.max_results
    window = stand.get_log_window("api-gateway", "09:00", "09:15", max_lines=5)
    assert len(window["lines"]) == 5 and window["truncated"]


def test_dispatch_returns_errors_as_data(stand):
    assert "error" in stand.dispatch("delete_logs", "{}")
    assert "error" in stand.dispatch("get_log_window", '{"service": "nope", "start": "09:00", "end": "09:01"}')
    assert "error" in stand.dispatch("search_logs", "{not json")
    assert "services" in stand.dispatch("list_services", "{}")


def test_audit_log_records_calls(stand):
    stand.list_services()
    with pytest.raises(ToolError):
        stand.get_log_window("x", "09:00", "09:01")
    records = [json.loads(line) for line in stand.settings.audit_path.read_text().splitlines()]
    assert records[-2]["tool"] == "list_services" and records[-2]["ok"]
    assert records[-1]["tool"] == "get_log_window" and not records[-1]["ok"]


def test_parse_time():
    from datetime import date

    assert parse_time("14:30", date(2026, 5, 14)).isoformat() == "2026-05-14T14:30:00+00:00"
    assert parse_time("2026-05-14 14:30").hour == 14
    assert parse_time("2026-05-14T14:30:00Z").tzinfo is not None
    with pytest.raises(ToolError):
        parse_time("вчера", date(2026, 5, 14))
