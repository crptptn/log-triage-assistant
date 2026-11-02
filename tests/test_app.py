"""Смоук-тест веб-интерфейса без браузера (streamlit AppTest). Пропускается, если streamlit не установлен."""

import json

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from triage.config import ROOT  # noqa: E402


def test_app_triages_example_incident(tmp_path, monkeypatch):
    from triage.cli import main

    monkeypatch.setenv("TRIAGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDER", "hashing")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    main(["generate"])
    main(["index"])
    incident = json.loads((tmp_path / "incidents.json").read_text(encoding="utf-8"))[0]

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not app.exception
    app.text_area(key="query").set_value(incident["title"]).run()
    next(b for b in app.button if b.label == "Разобрать").click().run()
    assert not app.exception
    assert any("Черновик баг-репорта" in s.value for s in app.subheader)
    assert app.metric[0].value  # категория определена
