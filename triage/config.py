"""Настройки из переменных окружения и файла .env в корне проекта."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Минимальный парсер .env: KEY=VALUE, комментарии через #. Уже заданные переменные не перетирает."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    runbooks_dir: Path
    embedder: str
    qdrant_url: str | None
    llm_base_url: str | None
    llm_model: str | None
    llm_api_key: str
    # ограничения инструментов: агент не может выгрузить «всё и сразу»
    max_window_minutes: int = 15
    max_lines: int = 200
    max_results: int = 10

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def index_meta_path(self) -> Path:
        return self.data_dir / "index_meta.json"

    @property
    def incidents_path(self) -> Path:
        return self.data_dir / "incidents.json"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)


def get_settings(**overrides) -> Settings:
    load_dotenv()
    settings = Settings(
        data_dir=Path(os.getenv("TRIAGE_DATA_DIR", str(ROOT / "data"))),
        runbooks_dir=Path(os.getenv("TRIAGE_RUNBOOKS_DIR", str(ROOT / "runbooks"))),
        embedder=os.getenv("EMBEDDER", "BAAI/bge-m3"),
        qdrant_url=os.getenv("QDRANT_URL") or None,
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or "EMPTY",
    )
    return replace(settings, **overrides)
