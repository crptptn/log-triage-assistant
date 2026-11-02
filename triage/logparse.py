"""Разбор логов, нормализация и нарезка на чанки для индекса.

Логи режутся двумя способами:
  window — окна по времени внутри одного сервиса (5 минут, не больше 60 строк);
  trace  — все строки одного trace_id сквозь сервисы, если среди них есть WARN/ERROR.
Первый вариант находит проблему «где-то в сервисе в это время», второй — сквозную цепочку запроса.
Повторяющиеся строки внутри чанка схлопываются по шаблону («×12»), чтобы шум не забивал эмбеддинг.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from triage.masking import mask

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40, "FATAL": 50}
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(?P<level>[A-Z]+)\s+"
    r"(?P<service>[\w-]+)\s+\[trace=(?P<trace>[^\]]*)\]\s?(?P<msg>.*)$"
)
_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.I), "<HEX>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<N>"),
]


@dataclass(frozen=True)
class LogLine:
    service: str
    line_no: int
    ts: datetime
    level: str
    trace: str
    message: str

    @property
    def severity(self) -> int:
        return LEVELS.get(self.level, 20)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_lines(lines: Iterable[str], service: str | None = None) -> list[LogLine]:
    """Строки без заголовка (например, стектрейс) приклеиваются к сообщению предыдущей записи."""
    parsed: list[LogLine] = []
    for line_no, raw in enumerate(lines, start=1):
        raw = raw.rstrip("\n")
        match = LINE_RE.match(raw)
        if match:
            trace = match["trace"] if match["trace"] not in ("", "-") else ""
            parsed.append(
                LogLine(
                    service or match["service"], line_no, parse_ts(match["ts"]), match["level"], trace, match["msg"]
                )
            )
        elif parsed and raw.strip():
            prev = parsed[-1]
            parsed[-1] = LogLine(prev.service, prev.line_no, prev.ts, prev.level, prev.trace, f"{prev.message}\n{raw}")
    return parsed


def parse_file(path: Path) -> list[LogLine]:
    with path.open(encoding="utf-8", errors="replace") as f:
        return parse_lines(f, service=path.stem)


def normalize(message: str) -> str:
    """Шаблон сообщения: изменчивые части (id, IP, числа) заменены метками."""
    for pattern, placeholder in _VOLATILE:
        message = pattern.sub(placeholder, message)
    return message


@dataclass
class Chunk:
    chunk_id: str
    kind: str
    lines: list[LogLine]
    text: str = field(default="")

    @property
    def services(self) -> list[str]:
        return sorted({line.service for line in self.lines})

    @property
    def start(self) -> datetime:
        return min(line.ts for line in self.lines)

    @property
    def end(self) -> datetime:
        return max(line.ts for line in self.lines)

    @property
    def max_severity(self) -> int:
        return max(line.severity for line in self.lines)

    def payload(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "kind": self.kind,
            "services": self.services,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_ts": self.start.timestamp(),
            "end_ts": self.end.timestamp(),
            "max_severity": self.max_severity,
            "line_refs": [[line.service, line.line_no] for line in self.lines],
            "text": self.text,
        }


def render(lines: list[LogLine], with_service: bool = False) -> str:
    """Текст чанка: замаскированные строки, повторы одного шаблона схлопнуты в одну с «(×N)»."""
    counts = Counter((line.level, line.service, normalize(line.message)) for line in lines)
    seen: set = set()
    out = []
    for line in lines:
        key = (line.level, line.service, normalize(line.message))
        if key in seen:
            continue
        seen.add(key)
        prefix = f"{line.ts:%H:%M:%S} {line.level:<5} " + (f"{line.service}: " if with_service else "")
        suffix = f" (×{counts[key]})" if counts[key] > 1 else ""
        out.append(prefix + mask(line.message) + suffix)
    return "\n".join(out)


def _header(chunk: Chunk) -> str:
    levels = Counter(line.level for line in chunk.lines if line.severity >= 30)
    level_info = ", ".join(f"{lvl}×{n}" for lvl, n in sorted(levels.items())) or "без ошибок"
    where = ", ".join(chunk.services)
    trace = f" trace={chunk.lines[0].trace}" if chunk.kind == "trace" else ""
    return f"[{where}]{trace} {chunk.start:%Y-%m-%d %H:%M:%S}–{chunk.end:%H:%M:%S} UTC | {level_info}"


def window_chunks(lines: list[LogLine], window_seconds: int = 300, max_lines: int = 60) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[LogLine] = []
    for line in lines:
        if current and ((line.ts - current[0].ts).total_seconds() >= window_seconds or len(current) >= max_lines):
            chunks.append(_make_chunk("window", current))
            current = []
        current.append(line)
    if current:
        chunks.append(_make_chunk("window", current))
    return chunks


def trace_chunks(lines: Iterable[LogLine], min_severity: int = 30) -> list[Chunk]:
    by_trace: dict[str, list[LogLine]] = {}
    for line in lines:
        if line.trace:
            by_trace.setdefault(line.trace, []).append(line)
    chunks = []
    for group in by_trace.values():
        if max(line.severity for line in group) >= min_severity:
            chunks.append(_make_chunk("trace", sorted(group, key=lambda x: x.ts)))
    return chunks


def _make_chunk(kind: str, lines: list[LogLine]) -> Chunk:
    first = lines[0]
    if kind == "trace":
        chunk_id = f"trace:{first.trace}"
    else:
        chunk_id = f"{first.service}:{first.line_no}-{lines[-1].line_no}"
    chunk = Chunk(chunk_id, kind, list(lines))
    chunk.text = _header(chunk) + "\n" + render(chunk.lines, with_service=(kind == "trace"))
    return chunk


def build_chunks(lines_by_service: dict[str, list[LogLine]]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for service in sorted(lines_by_service):
        chunks.extend(window_chunks(lines_by_service[service]))
    chunks.extend(trace_chunks(line for lines in lines_by_service.values() for line in lines))
    return chunks


def load_logs(logs_dir: Path) -> dict[str, list[LogLine]]:
    return {path.stem: parse_file(path) for path in sorted(logs_dir.glob("*.log"))}
