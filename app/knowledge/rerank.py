"""Cross-encoder reranking with FlashRank.

Retrieval optimizes recall; reranking optimizes precision on the small set that
actually reaches the synthesis context window. E-ids are assigned here, after
the final ordering is known, so citation handles always match what the model is
shown.
"""

import logging
from functools import lru_cache
from typing import Any

from app.config.settings import get_settings
from app.knowledge.evidence import Evidence, normalize_scores

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_reranker() -> Any:
    """Load the FlashRank cross-encoder once per process."""
    from flashrank import Ranker

    settings = get_settings()
    logger.info("loading reranker %s", settings.rerank_model)
    return Ranker(model_name=settings.rerank_model, cache_dir="/tmp/flashrank")


def assign_ids(items: list[Evidence]) -> list[Evidence]:
    """Label evidence E1..En in presentation order."""
    for position, item in enumerate(items, start=1):
        item.id = f"E{position}"
    return items


def rerank(query: str, candidates: list[Evidence], top_k: int | None = None) -> list[Evidence]:
    """Reorder candidates by query relevance, truncate, and assign E-ids.

    Falls back to the incoming fusion order if FlashRank is unavailable, so a
    missing model degrades quality rather than breaking the pipeline.
    """
    if not candidates:
        return []

    top_k = get_settings().rerank_top_k if top_k is None else top_k

    try:
        from flashrank import RerankRequest

        passages = [
            {"id": position, "text": item.text, "meta": {}}
            for position, item in enumerate(candidates)
        ]
        results = get_reranker().rerank(RerankRequest(query=query, passages=passages))

        ordered: list[Evidence] = []
        for result in results[:top_k]:
            item = candidates[int(result["id"])]
            item.score = float(result["score"])
            item.metadata["reranked"] = True
            ordered.append(item)
    except Exception as exc:  # noqa: BLE001 - reranking is a quality step, not critical path
        logger.warning("reranking failed (%s); falling back to fusion order", exc)
        ordered = candidates[:top_k]
        for item in ordered:
            item.metadata["reranked"] = False

    normalize_scores(ordered)
    return assign_ids(ordered)
