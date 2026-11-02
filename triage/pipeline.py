"""Цепочка разбора инцидента: поиск → классификация и анализ → черновик баг-репорта.

1. Время из описания («около 14:30») превращается в окно ±15 минут, поиск идёт только по фрагментам с ERROR.
2. LLM получает найденные фрагменты и регламенты и отвечает строго по JSON-схеме: категория, уверенность,
   что случилось, причина, номера фрагментов-доказательств. Ссылки на фрагменты, которых не было
   в выдаче, отбрасываются — так модель не может сослаться на выдуманное.
3. Баг-репорт собирается по шаблону из проверенных полей, без отдельного вызова LLM.
Без LLM (или если модель ответила не по схеме) категория определяется по правилам — демо не ломается.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from triage.categories import CATEGORIES, category_name, score_text
from triage.tools import LogTools, parse_time

_TIME_IN_TEXT = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "confidence", "summary", "root_cause", "evidence", "next_steps"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "Ты помогаешь тестировщику разобрать инцидент на тестовом стенде по логам. "
    "Опирайся только на приведённые фрагменты логов и регламенты, ничего не додумывай. "
    "Если данных не хватает, так и напиши и снизь confidence. "
    "В evidence перечисли id фрагментов (как в квадратных скобках), на которых основан вывод. "
    "summary — что произошло, одним предложением; root_cause — вероятная причина; "
    "next_steps — 2–4 конкретных шага, как подтвердить и что исправить. Пиши по-русски."
)


@dataclass
class TriageResult:
    query: str
    at: str | None
    mode: str
    category: str
    category_name: str
    confidence: float
    summary: str
    root_cause: str
    evidence: list[str]
    next_steps: list[str]
    chunks: list[dict]
    runbooks: list[dict]
    bug_report: str = ""
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def extract_time(text: str, tools: LogTools) -> datetime | None:
    match = _TIME_IN_TEXT.search(text)
    day = tools.log_date()
    if not match or day is None:
        return None
    return parse_time(f"{match[1]}:{match[2]}", day)


def retrieve(query: str, tools: LogTools, at: datetime | None, window_minutes: int = 15, k: int = 6) -> list[dict]:
    since = (at - timedelta(minutes=window_minutes)).isoformat() if at else None
    until = (at + timedelta(minutes=window_minutes)).isoformat() if at else None
    return tools.search_logs(query, since=since, until=until, errors_only=True, limit=k, include_refs=True)["results"]


def classify_rules(chunks: list[dict]) -> tuple[str, float]:
    """Взвешенный подсчёт признаков: чем выше фрагмент в выдаче, тем больше его вес."""
    totals = {code: 0.0 for code in CATEGORIES if code != "other"}
    for rank, chunk in enumerate(chunks):
        for code, n in score_text(chunk["text"]).items():
            totals[code] += n / (rank + 1)
    best = max(totals, key=totals.get)
    total = sum(totals.values())
    if total == 0:
        return "other", 0.0
    return best, round(totals[best] / total, 2)


def _analyze_llm(llm, query: str, chunks: list[dict], runbooks: list[dict]) -> dict:
    fragments = "\n\n".join(f"[{c['chunk_id']}]\n{c['text']}" for c in chunks) or "(ничего не найдено)"
    docs = "\n\n".join(f"### {d['title']}\n{d['text'][:1500]}" for d in runbooks) or "(нет)"
    categories = "\n".join(f"- {code}: {c['name']}" for code, c in CATEGORIES.items())
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Описание от тестировщика:\n{query}\n\nКатегории:\n{categories}\n\n"
            f"Фрагменты логов:\n{fragments}\n\nРегламенты:\n{docs}",
        },
    ]
    return llm.json(messages, ANALYSIS_SCHEMA, "incident_analysis")


def triage(
    query: str, tools: LogTools, llm=None, at: str | datetime | None = None, use_time: bool = True, k: int = 6
) -> TriageResult:
    timings = {}
    t0 = time.perf_counter()
    when = parse_time(at, tools.log_date()) if at else (extract_time(query, tools) if use_time else None)
    chunks = retrieve(query, tools, when, k=k)
    if not chunks and when is not None:
        # во времени тестировщик мог ошибиться сильнее, чем на 15 минут
        chunks = retrieve(query, tools, None, k=k)
    top_text = " ".join(c["text"][:300] for c in chunks[:2])
    runbooks = tools.search_runbooks(f"{query}\n{top_text}", limit=2)["results"]
    timings["retrieval_s"] = round(time.perf_counter() - t0, 2)

    mode = "rules"
    analysis = None
    if llm is not None:
        t1 = time.perf_counter()
        try:
            analysis = _analyze_llm(llm, query, chunks, runbooks)
            mode = "llm"
        except Exception as exc:  # noqa: BLE001 — сбой модели (сеть, формат ответа) не должен ронять разбор
            mode = f"rules (LLM недоступна: {type(exc).__name__})"
        timings["llm_s"] = round(time.perf_counter() - t1, 2)

    known_ids = {c["chunk_id"] for c in chunks}
    if analysis:
        evidence = [e.strip("[] ") for e in analysis["evidence"] if e.strip("[] ") in known_ids]
        category, confidence = analysis["category"], float(analysis["confidence"])
        summary, root_cause, next_steps = analysis["summary"], analysis["root_cause"], analysis["next_steps"]
    else:
        category, confidence = classify_rules(chunks)
        evidence = [c["chunk_id"] for c in chunks if score_text(c["text"]).get(category, 0) > 0][:3]
        summary = (
            f"По логам похоже на «{category_name(category)}»."
            if category != "other"
            else "Явных признаков известных проблем не найдено."
        )
        root_cause = runbooks[0]["title"] if runbooks and category != "other" else "Нужен ручной разбор."
        next_steps = [
            "Открыть фрагменты-доказательства и сверить с регламентом",
            "Проверить соседние сервисы в том же окне времени",
        ]

    result = TriageResult(
        query=query,
        at=when.isoformat() if when else None,
        mode=mode,
        category=category,
        category_name=category_name(category),
        confidence=round(confidence, 2),
        summary=summary,
        root_cause=root_cause,
        evidence=evidence,
        next_steps=next_steps,
        chunks=chunks,
        runbooks=runbooks,
        timings=timings,
    )
    result.bug_report = render_bug_report(result)
    result.timings["total_s"] = round(time.perf_counter() - t0, 2)
    return result


def render_bug_report(r: TriageResult) -> str:
    by_id = {c["chunk_id"]: c for c in r.chunks}
    evidence = [by_id[e] for e in r.evidence if e in by_id] or r.chunks[:1]
    services = sorted({s for c in evidence for s in c["services"]})
    if evidence:
        period = f"{min(c['start'] for c in evidence)[11:19]}–{max(c['end'] for c in evidence)[11:19]} UTC"
    else:
        period = "не определено"
    logs = "\n\n".join("\n".join(c["text"].splitlines()[:15]) for c in evidence) or "(фрагменты не найдены)"
    steps = "\n".join(f"- {s}" for s in r.next_steps)
    docs = "\n".join(f"- {d['title']} ({d['path']})" for d in r.runbooks) or "- нет"
    return (
        f"## [{r.category_name}] {r.summary}\n\n"
        f"**Окружение:** тестовый стенд; сервисы: {', '.join(services) or '—'}\n"
        f"**Время:** {period}\n"
        f"**Уверенность анализа:** {r.confidence:.0%} (режим: {r.mode})\n\n"
        f"### Исходное описание\n{r.query}\n\n"
        f"### Вероятная причина\n{r.root_cause}\n\n"
        f"### Фрагменты логов\n```text\n{logs}\n```\n\n"
        f"### Что проверить\n{steps}\n\n"
        f"### Регламенты\n{docs}\n\n"
        "_Черновик подготовлен автоматически — проверьте перед заведением._\n"
    )
