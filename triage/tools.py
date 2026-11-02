"""Инструменты для агента и MCP — только чтение и с ограничениями.

Чем ограничен агент:
  - только чтение: инструментов, которые что-то меняют, нет вообще;
  - сервис — только из списка известных (имена файлов в data/logs), произвольный путь передать нельзя;
  - окно выгрузки логов — не больше max_window_minutes, строк — не больше max_lines, результатов поиска — max_results;
  - всё, что уходит наружу (в том числе в LLM), проходит маскирование секретов и персональных данных;
  - каждый вызов пишется в журнал аудита data/audit.jsonl.
"""

from __future__ import annotations

import atexit
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from triage.config import Settings
from triage.logparse import LogLine, parse_file
from triage.masking import mask
from triage.store import Index

_TIME_ONLY = re.compile(r"^\s*(\d{1,2})[:.](\d{2})(?::(\d{2}))?\s*$")


class ToolError(ValueError):
    """Нарушение ограничений инструмента — возвращается агенту как текст ошибки."""


def parse_time(value: str | datetime | None, default_date: date | None = None) -> datetime | None:
    """«14:30», «2026-05-14 14:30», ISO с Z. Время без зоны считается UTC — как в логах."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    match = _TIME_ONLY.match(value)
    if match:
        if default_date is None:
            raise ToolError(f"для времени «{value}» неизвестна дата")
        h, m, s = int(match[1]), int(match[2]), int(match[3] or 0)
        return datetime(default_date.year, default_date.month, default_date.day, h, m, s, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError as exc:
        raise ToolError(f"не удалось разобрать время «{value}»") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class LogTools:
    def __init__(self, settings: Settings, index: Index):
        self.settings = settings
        self.index = index
        self._cache: dict[str, tuple[float, list[LogLine]]] = {}

    # --- служебное ---

    def _audit(self, tool: str, args: dict, ok: bool, detail: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool, "args": args, "ok": ok}
        record["result" if ok else "error"] = detail
        self.settings.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def known_services(self) -> list[str]:
        return sorted(p.stem for p in self.settings.logs_dir.glob("*.log"))

    def _check_service(self, service: str | None) -> str | None:
        if service in (None, ""):
            return None
        if service not in self.known_services():
            raise ToolError(f"неизвестный сервис «{service}», доступны: {', '.join(self.known_services())}")
        return service

    def _lines(self, service: str) -> list[LogLine]:
        path: Path = self.settings.logs_dir / f"{service}.log"
        mtime = path.stat().st_mtime
        cached = self._cache.get(service)
        if cached is None or cached[0] != mtime:
            self._cache[service] = (mtime, parse_file(path))
        return self._cache[service][1]

    def log_date(self) -> date | None:
        """Дата логов стенда — чтобы «14:30» из описания превратить в полную метку времени."""
        for service in self.known_services():
            lines = self._lines(service)
            if lines:
                return lines[0].ts.date()
        return None

    def _limit(self, value: int, maximum: int) -> int:
        return max(1, min(int(value), maximum))

    # --- инструменты ---

    def list_services(self) -> dict:
        services = self.known_services()
        self._audit("list_services", {}, True, len(services))
        return {"services": services, "log_date": str(self.log_date())}

    def search_logs(
        self,
        query: str,
        service: str | None = None,
        since: str | None = None,
        until: str | None = None,
        errors_only: bool = False,
        limit: int = 5,
        include_refs: bool = False,
    ) -> dict:
        args = {
            "query": query,
            "service": service,
            "since": since,
            "until": until,
            "errors_only": errors_only,
            "limit": limit,
        }
        try:
            if not query or not query.strip():
                raise ToolError("пустой запрос")
            day = self.log_date()
            hits = self.index.search_logs(
                query,
                limit=self._limit(limit, self.settings.max_results),
                service=self._check_service(service),
                since=parse_time(since, day),
                until=parse_time(until, day),
                errors_only=errors_only,
            )
        except ToolError as exc:
            self._audit("search_logs", args, False, str(exc))
            raise
        results = []
        for hit in hits:
            item = {k: hit[k] for k in ("chunk_id", "kind", "services", "start", "end", "score", "text")}
            if include_refs:
                item["line_refs"] = hit["line_refs"]
            results.append(item)
        self._audit("search_logs", args, True, [r["chunk_id"] for r in results])
        return {"results": results}

    def get_log_window(self, service: str, start: str, end: str, max_lines: int = 100) -> dict:
        args = {"service": service, "start": start, "end": end, "max_lines": max_lines}
        try:
            service = self._check_service(service)
            if service is None:
                raise ToolError("не указан сервис")
            day = self.log_date()
            t_start, t_end = parse_time(start, day), parse_time(end, day)
            if t_start is None or t_end is None or t_end < t_start:
                raise ToolError("нужны start и end, причём end не раньше start")
            if t_end - t_start > timedelta(minutes=self.settings.max_window_minutes):
                raise ToolError(f"окно больше {self.settings.max_window_minutes} минут — сузьте интервал")
        except ToolError as exc:
            self._audit("get_log_window", args, False, str(exc))
            raise
        limit = self._limit(max_lines, self.settings.max_lines)
        selected = [line for line in self._lines(service) if t_start <= line.ts <= t_end]
        lines = [
            f"{line.line_no}: {line.ts:%H:%M:%S} {line.level:<5} [trace={line.trace or '-'}] {mask(line.message)}"
            for line in selected[:limit]
        ]
        self._audit("get_log_window", args, True, len(lines))
        return {
            "service": service,
            "start": t_start.isoformat(),
            "end": t_end.isoformat(),
            "lines": lines,
            "truncated": len(selected) > limit,
        }

    def search_runbooks(self, query: str, limit: int = 3) -> dict:
        args = {"query": query, "limit": limit}
        if not query or not query.strip():
            self._audit("search_runbooks", args, False, "пустой запрос")
            raise ToolError("пустой запрос")
        hits = self.index.search_runbooks(query, limit=self._limit(limit, 5))
        results = [{k: h[k] for k in ("doc_id", "title", "path", "score", "text")} for h in hits]
        self._audit("search_runbooks", args, True, [r["doc_id"] for r in results])
        return {"results": results}

    # --- вызов по имени (для агента) ---

    def dispatch(self, name: str, arguments: str | dict) -> dict:
        handlers = {
            "list_services": self.list_services,
            "search_logs": self.search_logs,
            "get_log_window": self.get_log_window,
            "search_runbooks": self.search_runbooks,
        }
        if name not in handlers:
            return {"error": f"инструмент «{name}» недоступен"}
        try:
            args = json.loads(arguments or "{}") if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError:
            return {"error": "аргументы не являются JSON"}
        args.pop("include_refs", None)  # служебный параметр, агенту недоступен
        try:
            return handlers[name](**args)
        except ToolError as exc:
            return {"error": str(exc)}
        except TypeError as exc:
            return {"error": f"неверные аргументы: {exc}"}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "Список сервисов стенда, по которым есть логи, и дата логов.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_logs",
            "description": "Семантический поиск по фрагментам логов. Время в UTC: «14:30» или «2026-05-14 14:30».",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что ищем, своими словами"},
                    "service": {"type": "string", "description": "Ограничить одним сервисом"},
                    "since": {"type": "string", "description": "Не раньше этого времени"},
                    "until": {"type": "string", "description": "Не позже этого времени"},
                    "errors_only": {"type": "boolean", "description": "Только фрагменты с ERROR"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_log_window",
            "description": "Строки лога одного сервиса за интервал не длиннее 15 минут.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["service", "start", "end"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_runbooks",
            "description": "Поиск по регламентам: известные проблемы, как их подтвердить и что делать.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 5}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]


def open_tools(settings: Settings) -> LogTools:
    """Открывает готовый индекс с диска (или Qdrant по QDRANT_URL)."""
    from triage.embedder import make_embedder

    meta_path = settings.index_meta_path
    if not settings.qdrant_url:
        if not meta_path.exists():
            raise RuntimeError("индекса нет — сначала: python -m triage generate && python -m triage index")
        built_with = json.loads(meta_path.read_text(encoding="utf-8"))["embedder"]
        if built_with != settings.embedder:
            raise RuntimeError(
                f"индекс построен моделью {built_with}, а EMBEDDER={settings.embedder} — "
                "поправьте .env или пересоберите: python -m triage index"
            )
    index = Index(make_embedder(settings.embedder), location=settings.index_dir, url=settings.qdrant_url)
    atexit.register(index.close)  # закрыть встроенный Qdrant до завершения интерпретатора
    if index.count() == 0:
        raise RuntimeError("индекс пуст — сначала: python -m triage index")
    return LogTools(settings, index)
