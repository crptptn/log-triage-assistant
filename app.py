"""Веб-интерфейс для демонстрации: streamlit run app.py"""

from __future__ import annotations

import json

import streamlit as st

from triage.agent import run_agent
from triage.config import get_settings
from triage.llm import make_llm
from triage.pipeline import triage
from triage.tools import open_tools

st.set_page_config(page_title="Разбор инцидентов по логам", page_icon="🔎", layout="wide")


@st.cache_resource
def load():
    settings = get_settings()
    return settings, open_tools(settings), make_llm(settings)


try:
    settings, tools, llm = load()
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()

incidents = json.loads(settings.incidents_path.read_text(encoding="utf-8")) if settings.incidents_path.exists() else []

with st.sidebar:
    st.header("Стенд")
    st.write(f"Сервисы: {', '.join(tools.known_services())}")
    st.write(f"Дата логов: {tools.log_date()} (время в UTC)")
    st.write(f"Эмбеддинги: `{settings.embedder}`")
    st.write(f"LLM: `{llm.model}`" if llm else "LLM: не подключена — классификация по правилам")
    use_llm = st.toggle("Использовать LLM", value=llm is not None, disabled=llm is None)
    st.divider()
    st.caption("Примеры инцидентов")
    for inc in incidents:
        if st.button(f"{inc['id']}: {inc['title'][:60]}…", key=inc["id"], width="stretch"):
            st.session_state["query"] = inc["title"]

st.title("Разбор инцидентов по логам")
tab_triage, tab_agent, tab_audit = st.tabs(["Разбор", "Агент", "Журнал вызовов"])

with tab_triage:
    query = st.text_area(
        "Что случилось", key="query", height=90, placeholder="Тест печати упал около 14:30: принтер не отвечает"
    )
    at = st.text_input("Время инцидента (UTC), если его нет в описании", placeholder="14:30")
    if st.button("Разобрать", type="primary", disabled=not query):
        with st.spinner("Ищу фрагменты логов и анализирую…"):
            result = triage(query, tools, llm=llm if use_llm else None, at=at or None)
        c1, c2, c3 = st.columns(3)
        c1.metric("Категория", result.category_name)
        c2.metric("Уверенность", f"{result.confidence:.0%}")
        c3.metric("Время разбора", f"{result.timings.get('total_s', 0)} с")
        st.caption(f"Режим: {result.mode}; окно поиска вокруг {result.at or 'без привязки ко времени'}")
        st.subheader("Черновик баг-репорта")
        st.markdown(result.bug_report)
        st.download_button("Скачать .md", result.bug_report, file_name="bug-report.md")
        with st.expander(f"Найденные фрагменты логов ({len(result.chunks)})"):
            for chunk in result.chunks:
                mark = "✅ " if chunk["chunk_id"] in result.evidence else ""
                st.markdown(f"{mark}`{chunk['chunk_id']}` · score {chunk['score']}")
                st.code(chunk["text"], language="text")
        with st.expander("Регламенты"):
            for doc in result.runbooks:
                st.markdown(doc["text"])

with tab_agent:
    if llm is None:
        st.info("Агентному режиму нужна LLM: задайте LLM_BASE_URL и LLM_MODEL в .env.")
    else:
        question = st.text_area(
            "Вопрос агенту", height=90, placeholder="Почему около 11:40 перестали формироваться отчёты?"
        )
        if st.button("Спросить агента", disabled=not question):
            with st.spinner("Агент вызывает инструменты…"):
                answer = run_agent(question, tools, llm)
            for i, step in enumerate(answer["steps"], start=1):
                with st.expander(f"Шаг {i}: {step['tool']} {step['arguments']}"):
                    st.json(step["result"])
            st.markdown(answer["answer"])

with tab_audit:
    st.caption("Каждый вызов инструмента — из интерфейса, агента или MCP — пишется в data/audit.jsonl")
    if settings.audit_path.exists():
        rows = [json.loads(line) for line in settings.audit_path.read_text(encoding="utf-8").splitlines()[-50:]]
        st.dataframe(list(reversed(rows)), width="stretch")
