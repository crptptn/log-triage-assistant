"""Командная строка: python -m triage <команда>."""

from __future__ import annotations

import argparse
import json
import sys

from triage.config import get_settings


def cmd_generate(args) -> None:
    from triage.synth import generate

    settings = get_settings()
    stats = generate(settings.data_dir, seed=args.seed, per_category=args.per_category)
    print(f"сгенерировано {stats['lines']} строк логов, {stats['incidents']} инцидентов → {stats['logs_dir']}")


def cmd_index(args) -> None:
    from triage.embedder import make_embedder
    from triage.logparse import build_chunks, load_logs
    from triage.store import Index, load_runbooks

    settings = get_settings(**({"embedder": args.embedder} if args.embedder else {}))
    logs = load_logs(settings.logs_dir)
    if not logs:
        sys.exit("нет логов — сначала: python -m triage generate")
    chunks = build_chunks(logs)
    hint = "" if settings.embedder == "hashing" else " (первый запуск скачает модель)"
    print(f"чанков: {len(chunks)}; эмбеддинги: {settings.embedder}{hint}")
    index = Index(make_embedder(settings.embedder), location=settings.index_dir, url=settings.qdrant_url)
    meta = index.build(chunks, load_runbooks(settings.runbooks_dir))
    index.close()
    settings.index_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"индекс готов за {meta['build_seconds']} с: {meta['chunks']} фрагментов логов, {meta['runbooks']} регламентов"
    )
    if args.embedder:
        print(f"не забудьте EMBEDDER={args.embedder} в .env — запросы должны идти той же моделью")


def _tools():
    from triage.tools import open_tools

    return open_tools(get_settings())


def cmd_search(args) -> None:
    result = _tools().search_logs(args.query, args.service, args.since, args.until, args.errors_only, args.limit)
    for hit in result["results"]:
        print(f"\n[{hit['chunk_id']}] score={hit['score']}\n{hit['text']}")


def cmd_window(args) -> None:
    result = _tools().get_log_window(args.service, args.start, args.end, args.max_lines)
    print("\n".join(result["lines"]))
    if result["truncated"]:
        print("… (обрезано по лимиту строк)")


def cmd_triage(args) -> None:
    from triage.llm import make_llm
    from triage.pipeline import triage

    settings = get_settings()
    llm = None if args.no_llm else make_llm(settings)
    result = triage(args.text, _tools(), llm=llm, at=args.at)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    print(f"Категория: {result.category_name} ({result.confidence:.0%}, режим: {result.mode})")
    print(f"Время: {result.timings}\n")
    print(result.bug_report)


def cmd_agent(args) -> None:
    from triage.agent import run_agent
    from triage.llm import make_llm

    llm = make_llm(get_settings())
    if llm is None:
        sys.exit("агенту нужна LLM: задайте LLM_BASE_URL и LLM_MODEL в .env")
    result = run_agent(args.text, _tools(), llm, max_steps=args.max_steps)
    for i, step in enumerate(result["steps"], start=1):
        status = "ошибка: " + step["result"]["error"] if "error" in step["result"] else "ok"
        print(f"шаг {i}: {step['tool']}({step['arguments']}) → {status}")
    print("\n" + result["answer"])


def cmd_eval(args) -> None:
    from triage.evaluate import evaluate, save
    from triage.llm import make_llm

    llm = None if args.no_llm else make_llm(get_settings())
    report = evaluate(_tools(), llm=llm)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"подробности: {save(report)}")


def cmd_incidents(args) -> None:
    for inc in json.loads(get_settings().incidents_path.read_text(encoding="utf-8")):
        print(f"{inc['id']}: {inc['title']}")
        if args.answers:
            print(f"    → {inc['category']}: {inc['root_cause']}")


def cmd_mcp(args) -> None:
    from triage.mcp_server import main

    main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m triage", description="Разбор инцидентов по логам")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("generate", help="сгенерировать синтетические логи и инциденты")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--per-category", type=int, default=2)
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("index", help="построить векторный индекс логов и регламентов")
    p.add_argument("--embedder", help="BAAI/bge-m3 | intfloat/multilingual-e5-small | hashing")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="семантический поиск по логам")
    p.add_argument("query")
    p.add_argument("--service")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--errors-only", action="store_true")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("window", help="строки лога сервиса за интервал")
    p.add_argument("service")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("--max-lines", type=int, default=100)
    p.set_defaults(fn=cmd_window)

    p = sub.add_parser("triage", help="разобрать инцидент по описанию")
    p.add_argument("text")
    p.add_argument("--at", help="время инцидента, если его нет в описании (UTC)")
    p.add_argument("--no-llm", action="store_true", help="только правила, без модели")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_triage)

    p = sub.add_parser("agent", help="агентный разбор с вызовом инструментов")
    p.add_argument("text")
    p.add_argument("--max-steps", type=int, default=6)
    p.set_defaults(fn=cmd_agent)

    p = sub.add_parser("eval", help="оценка на инцидентах с известной причиной")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("incidents", help="список инцидентов для демонстрации")
    p.add_argument("--answers", action="store_true", help="показать истинные причины")
    p.set_defaults(fn=cmd_incidents)

    p = sub.add_parser("mcp", help="запустить MCP-сервер (stdio)")
    p.set_defaults(fn=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except RuntimeError as exc:
        sys.exit(str(exc))
