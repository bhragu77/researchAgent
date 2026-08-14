"""Pre-load the local retrieval models.

The embedder and the cross-encoder each take tens of seconds to load on first
use. The manager dispatches agents with a per-agent timeout (default 20s), so
without this the *first* question of a batch loses its evidence to a timeout
that has nothing to do with retrieval quality.

Called by the eval runner and the demo script before the first question.
"""

import logging
import time

logger = logging.getLogger(__name__)


def warm_up() -> float:
    """Load the embedding and reranking models. Returns seconds taken."""
    started = time.monotonic()

    from app.knowledge.rerank import get_reranker
    from app.knowledge.retrieval import embed_texts, get_embedder

    get_embedder()
    embed_texts(["warm up"])
    try:
        get_reranker()
    except Exception as exc:  # noqa: BLE001 - reranking degrades gracefully
        logger.warning("reranker warm-up failed (retrieval will still work): %s", exc)

    elapsed = time.monotonic() - started
    logger.info("model warm-up complete in %.1fs", elapsed)
    return elapsed
