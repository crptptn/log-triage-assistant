"""Оценка на наборе инцидентов с известной причиной (data/incidents.json).

Поиск: попал ли в top-k фрагмент, содержащий строки инцидента (hit@k), и на какой позиции (MRR).
Считается в двух режимах — со временем из описания и без него.
Классификация: совпала ли категория с истинной. Доказательства: доля фрагментов, на которые сослался
анализ, действительно относящихся к инциденту (evidence precision).
"""

from __future__ import annotations

import json
from datetime import datetime

from triage.config import ROOT
from triage.pipeline import extract_time, retrieve, triage
from triage.tools import LogTools

KS = (1, 3, 6)


def _overlaps(chunk: dict, signature: set[tuple[str, int]]) -> bool:
    return any((service, line) in signature for service, line in chunk.get("line_refs", []))


def _rank(chunks: list[dict], signature: set[tuple[str, int]]) -> int | None:
    for pos, chunk in enumerate(chunks, start=1):
        if _overlaps(chunk, signature):
            return pos
    return None


def evaluate(tools: LogTools, llm=None, incidents_path=None) -> dict:
    incidents = json.loads((incidents_path or tools.settings.incidents_path).read_text(encoding="utf-8"))
    rows = []
    for inc in incidents:
        signature = {(s, n) for s, n in inc["signature"]}
        ranks = {}
        for mode in ("with_time", "no_time"):
            at = extract_time(inc["title"], tools) if mode == "with_time" else None
            ranks[mode] = _rank(retrieve(inc["title"], tools, at, k=max(KS)), signature)
        result = triage(inc["title"], tools, llm=llm)
        by_id = {c["chunk_id"]: c for c in result.chunks}
        cited = [by_id[e] for e in result.evidence if e in by_id]
        rows.append(
            {
                "id": inc["id"],
                "category": inc["category"],
                "predicted": result.category,
                "correct": result.category == inc["category"],
                "mode": result.mode,
                "rank_with_time": ranks["with_time"],
                "rank_no_time": ranks["no_time"],
                "evidence_precision": (sum(_overlaps(c, signature) for c in cited) / len(cited)) if cited else None,
                "seconds": result.timings.get("total_s"),
            }
        )

    def hit(mode: str, k: int) -> float:
        return sum(1 for r in rows if r[f"rank_{mode}"] and r[f"rank_{mode}"] <= k) / len(rows)

    def mrr(mode: str) -> float:
        return sum(1 / r[f"rank_{mode}"] for r in rows if r[f"rank_{mode}"]) / len(rows)

    precisions = [r["evidence_precision"] for r in rows if r["evidence_precision"] is not None]
    summary = {
        "n_incidents": len(rows),
        "embedder": tools.index.embedder.name,
        "llm": getattr(llm, "model", None),
        "retrieval": {
            mode: {**{f"hit@{k}": round(hit(mode, k), 3) for k in KS}, "mrr": round(mrr(mode), 3)}
            for mode in ("with_time", "no_time")
        },
        "classification_accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3),
        "evidence_precision": round(sum(precisions) / len(precisions), 3) if precisions else None,
        "mean_seconds": round(sum(r["seconds"] or 0 for r in rows) / len(rows), 2),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return {"summary": summary, "incidents": rows}


def save(report: dict) -> str:
    out = ROOT / "results" / "eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)
