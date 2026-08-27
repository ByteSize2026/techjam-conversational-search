"""Optional MiniLM cosine over a short hard-filter pool.

Loads ``sentence-transformers/all-MiniLM-L6-v2`` from the local Hugging Face
cache when possible. Missing torch/transformers/weights, or any encode
error, leaves ranking unchanged (offline fallback).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from .contest_index import ContestIndex
from .contest_slots import ContestState

EncodeFn = Callable[[list[str]], list[list[float]]]


class PoolDenseEncoder:
    """Lazy MiniLM encoder with an injectable ``encode`` for tests."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        *,
        encode: EncodeFn | None = None,
    ) -> None:
        self.model_name = model_name
        self.encode = encode
        self._ready: bool | None = True if encode is not None else None
        self._cache: dict[str, list[float]] = {}
        self._tokenizer = None
        self._model = None
        self._torch = None

    def available(self) -> bool:
        if self.encode is not None:
            return True
        self._ensure()
        return bool(self._ready)

    def _ensure(self) -> None:
        if self._ready is not None:
            return
        try:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            import torch
            from transformers import AutoModel, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(self.model_name, local_files_only=True)
            model = AutoModel.from_pretrained(self.model_name, local_files_only=True)
            model.eval()
        except Exception:
            self._ready = False
            return
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch
        self._ready = True

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self.encode is not None:
            return self.encode(texts)
        self._ensure()
        if not self._ready or self._model is None or self._tokenizer is None or self._torch is None:
            return []
        torch = self._torch
        encoded = self._tokenizer(
            [str(item)[:500] for item in texts],
            padding=True,
            truncation=True,
            max_length=128,
            return_tensors="pt",
        )
        with torch.no_grad():
            output = self._model(**encoded)
            mask = encoded["attention_mask"].unsqueeze(-1).to(output.last_hidden_state.dtype)
            summed = (output.last_hidden_state * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            emb = torch.nn.functional.normalize(summed / counts, p=2, dim=1)
        return emb.cpu().tolist()

    def vector(self, text: str) -> list[float] | None:
        key = text[:500]
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rows = self._embed_batch([key])
        if not rows:
            return None
        self._cache[key] = rows[0]
        return rows[0]

    def pool_scores(self, query: str, docs: Sequence[str]) -> list[float] | None:
        if not docs:
            return []
        query_vec = self.vector(query)
        if query_vec is None:
            return None
        vectors: list[list[float]] = []
        for doc in docs:
            item = self.vector(doc)
            if item is None:
                return None
            vectors.append(item)
        raw = [_dot(query_vec, item) for item in vectors]
        lo = min(raw)
        hi = max(raw)
        if hi - lo < 1e-9:
            return [0.0] * len(raw)
        return [(value - lo) / (hi - lo) for value in raw]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


_ENCODER: PoolDenseEncoder | None = None


def get_encoder() -> PoolDenseEncoder:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = PoolDenseEncoder()
    return _ENCODER


def set_encoder(encoder: PoolDenseEncoder | None) -> None:
    global _ENCODER
    _ENCODER = encoder


def dense_query(state: ContestState) -> str:
    parts = [state.category or ""]
    parts.extend(item.text for item in state.active)
    return " ".join(part for part in parts if part)[:500]


def dense_doc(index: ContestIndex, idx: int) -> str:
    return (index.titles[idx] + " " + index.blobs[idx])[:500]
