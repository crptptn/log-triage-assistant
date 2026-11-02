"""Эмбеддинги.

По умолчанию BAAI/bge-m3: мультиязычная модель, поэтому русский запрос тестировщика находит
английские строки логов. На слабом ноутбуке можно взять intfloat/multilingual-e5-small,
а без интернета и без torch — «hashing» (символьные n-граммы, годится для тестов и офлайн-показа).
"""

from __future__ import annotations

import numpy as np


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 1024):
        from sklearn.feature_extraction.text import HashingVectorizer

        self.dim = dim
        self._vectorizer = HashingVectorizer(
            n_features=dim, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False, norm="l2", lowercase=True
        )

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._vectorizer.transform(texts).toarray().astype("float32")

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, batch_size: int = 16):
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")
        self.dim = int(self.model.get_sentence_embedding_dimension())
        self.batch_size = batch_size
        # e5 обучена с префиксами «query: » и «passage: », bge-m3 они не нужны
        self._e5 = "e5" in model_name.lower()

    def _encode(self, texts: list[str]) -> np.ndarray:
        emb = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=len(texts) > 64,
        )
        return np.asarray(emb, dtype="float32")

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode([f"passage: {t}" if self._e5 else t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([f"query: {text}" if self._e5 else text])[0]


def make_embedder(name: str):
    if name == "hashing":
        return HashingEmbedder()
    return SentenceTransformerEmbedder(name)
