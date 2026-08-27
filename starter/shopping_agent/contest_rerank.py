"""Optional FlashRank cross-encoder over a short hard-filter pool."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

ScoreFn = Callable[[str, Sequence[str]], list[float]]


class PoolReranker:
    """Lazy TinyBERT reranker; injectable ``score`` for tests."""

    def __init__(
        self,
        model_name: str = "ms-marco-TinyBERT-L-2-v2",
        *,
        cache_dir: str | Path | None = None,
        score: ScoreFn | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path("data/.retrieval_cache/flashrank")
        self.score = score
        self._ranker = None
        self._ready: bool | None = True if score is not None else None

    def available(self) -> bool:
        if self.score is not None:
            return True
        self._ensure()
        return bool(self._ready)

    def _ensure(self) -> None:
        if self._ready is not None:
            return
        try:
            from flashrank import Ranker

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._ranker = Ranker(model_name=self.model_name, cache_dir=str(self.cache_dir))
        except Exception:
            self._ready = False
            return
        self._ready = True

    def pool_scores(self, query: str, docs: Sequence[str]) -> list[float] | None:
        if not docs:
            return []
        if self.score is not None:
            raw = self.score(query, docs)
        else:
            self._ensure()
            if not self._ready or self._ranker is None:
                return None
            try:
                from flashrank import RerankRequest

                passages = [{"id": str(i), "text": (doc or query)[:800]} for i, doc in enumerate(docs)]
                ranked = self._ranker.rerank(RerankRequest(query=query[:500] or "product", passages=passages))
            except Exception:
                return None
            by_id: dict[str, float] = {}
            for row in ranked:
                if isinstance(row, dict):
                    key = str(row.get("id", ""))
                    try:
                        value = float(row.get("score") or 0.0)
                    except (TypeError, ValueError):
                        value = 0.0
                else:
                    key = str(getattr(row, "id", ""))
                    try:
                        value = float(getattr(row, "score", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        value = 0.0
                by_id[key] = value
            raw = [by_id.get(str(i), 0.0) for i in range(len(docs))]
        if len(raw) != len(docs):
            return None
        lo = min(raw)
        hi = max(raw)
        if hi - lo < 1e-9:
            return [0.0] * len(raw)
        return [(value - lo) / (hi - lo) for value in raw]


_RERANKER: PoolReranker | None = None


def get_reranker() -> PoolReranker:
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = PoolReranker()
    return _RERANKER


def set_reranker(reranker: PoolReranker | None) -> None:
    global _RERANKER
    _RERANKER = reranker
