"""Векторный индекс в Qdrant: чанки логов и регламенты.

Без QDRANT_URL Qdrant работает встроенно (файлы в data/index), сервер поднимать не нужно.
Встроенный режим держит блокировку на папку: одновременно индекс открывает только один процесс
(CLI, веб-интерфейс или MCP-сервер). Для параллельной работы — Qdrant в Docker и QDRANT_URL.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from triage.logparse import Chunk

LOGS = "log_chunks"
RUNBOOKS = "runbooks"


def load_runbooks(runbooks_dir: Path) -> list[dict]:
    docs = []
    for path in sorted(runbooks_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = next((m.group(1) for m in re.finditer(r"^#\s+(.+)$", text, re.M)), path.stem)
        docs.append({"doc_id": path.stem, "title": title.strip(), "path": f"runbooks/{path.name}", "text": text})
    return docs


class Index:
    def __init__(self, embedder, location: str | Path = ":memory:", url: str | None = None):
        from qdrant_client import QdrantClient

        self.embedder = embedder
        self.location = location
        if url:
            self.client = QdrantClient(url=url)
        elif str(location) == ":memory:":
            self.client = QdrantClient(location=":memory:")
        else:
            Path(location).mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(location))

    # --- построение ---

    def _recreate(self, name: str) -> None:
        from qdrant_client import models

        if self.client.collection_exists(name):
            self.client.delete_collection(name)
        self.client.create_collection(
            name, vectors_config=models.VectorParams(size=self.embedder.dim, distance=models.Distance.COSINE)
        )

    def _upsert(self, name: str, texts: list[str], payloads: list[dict], batch: int = 128) -> None:
        from qdrant_client import models

        for start in range(0, len(texts), batch):
            vectors = self.embedder.embed_documents(texts[start : start + batch])
            points = [
                models.PointStruct(id=start + i, vector=vec.tolist(), payload=payloads[start + i])
                for i, vec in enumerate(vectors)
            ]
            self.client.upsert(name, points=points)

    def build(self, chunks: list[Chunk], runbooks: list[dict]) -> dict:
        t0 = time.perf_counter()
        self._recreate(LOGS)
        self._upsert(LOGS, [c.text for c in chunks], [c.payload() for c in chunks])
        self._recreate(RUNBOOKS)
        self._upsert(RUNBOOKS, [d["text"] for d in runbooks], runbooks)
        meta = {
            "embedder": self.embedder.name,
            "dim": self.embedder.dim,
            "chunks": len(chunks),
            "runbooks": len(runbooks),
            "build_seconds": round(time.perf_counter() - t0, 1),
            "built_at": datetime.now().isoformat(timespec="seconds"),
        }
        return meta

    def count(self, name: str = LOGS) -> int:
        return self.client.count(name).count if self.client.collection_exists(name) else 0

    # --- поиск ---

    def search_logs(
        self,
        query: str,
        limit: int = 5,
        service: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        errors_only: bool = False,
    ) -> list[dict]:
        from qdrant_client import models

        must = []
        if service:
            must.append(models.FieldCondition(key="services", match=models.MatchValue(value=service)))
        if since:
            must.append(models.FieldCondition(key="end_ts", range=models.Range(gte=since.timestamp())))
        if until:
            must.append(models.FieldCondition(key="start_ts", range=models.Range(lte=until.timestamp())))
        if errors_only:
            must.append(models.FieldCondition(key="max_severity", range=models.Range(gte=40)))
        hits = self.client.query_points(
            LOGS,
            query=self.embedder.embed_query(query).tolist(),
            limit=limit,
            query_filter=models.Filter(must=must) if must else None,
            with_payload=True,
        ).points
        return [{**hit.payload, "score": round(float(hit.score), 4)} for hit in hits]

    def search_runbooks(self, query: str, limit: int = 3) -> list[dict]:
        hits = self.client.query_points(
            RUNBOOKS, query=self.embedder.embed_query(query).tolist(), limit=limit, with_payload=True
        ).points
        return [{**hit.payload, "score": round(float(hit.score), 4)} for hit in hits]

    def close(self) -> None:
        self.client.close()
