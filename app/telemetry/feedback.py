"""CSAT capture.

One rating per run, linked to `research_trace`. Writes are tenant-scoped: the
tenant on the row is taken from the caller's verified JWT claim and the run is
looked up under that same tenant, so a token for tenant A cannot rate — or
confirm the existence of — a run belonging to tenant B.

The rating is also mirrored to Langfuse as a score, so a run's human judgement
sits next to its automated confidence and faithfulness scores.
"""

import logging
from typing import Any

from app.knowledge.retrieval import connect
from app.telemetry import persistence, tracing

logger = logging.getLogger(__name__)

FEEDBACK_TABLE = persistence.FEEDBACK_TABLE

MIN_RATING = 1
MAX_RATING = 5


class FeedbackError(Exception):
    """Raised when feedback cannot be attached to a run."""


class RunNotFound(FeedbackError):
    """The run does not exist, or does not belong to this tenant."""


def record_feedback(
    run_id: str, tenant_id: str, rating: int, comment: str | None = None
) -> dict[str, Any]:
    """Store a 1-5 rating against a run.

    Raises:
        ValueError: if the rating is out of range.
        RunNotFound: if the run is not this tenant's.
    """
    if not isinstance(rating, int) or not MIN_RATING <= rating <= MAX_RATING:
        raise ValueError(f"rating must be an integer {MIN_RATING}-{MAX_RATING}, got {rating!r}")

    # Tenant-scoped lookup. A run belonging to another tenant is reported as
    # not found rather than forbidden — the difference would leak its existence.
    run = persistence.get_run(run_id, tenant_id=tenant_id)
    if run is None:
        raise RunNotFound(f"run {run_id!r} not found for this tenant")

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {FEEDBACK_TABLE} (run_id, tenant_id, rating, comment)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                rating = EXCLUDED.rating,
                comment = EXCLUDED.comment,
                ts = now()
            RETURNING id, ts;
            """,  # noqa: S608 - table name is a module constant
            (run_id, tenant_id, rating, comment),
        )
        feedback_id, ts = cur.fetchone()
        conn.commit()

    # Normalized to 0-1 so it sits on the same axis as the other trace scores.
    tracing.score_run(run_id, "csat", (rating - 1) / 4.0, comment or "")
    tracing.flush()

    logger.info("feedback recorded: run=%s tenant=%s rating=%d", run_id, tenant_id, rating)
    return {
        "id": feedback_id,
        "run_id": run_id,
        "tenant_id": tenant_id,
        "rating": rating,
        "comment": comment,
        "ts": ts,
    }


def get_feedback(run_id: str, tenant_id: str) -> dict[str, Any] | None:
    """Fetch this tenant's feedback for a run, if any."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, run_id, tenant_id, rating, comment, ts FROM {FEEDBACK_TABLE} "
            f"WHERE run_id = %s AND tenant_id = %s",  # noqa: S608
            (run_id, tenant_id),
        )
        row = cur.fetchone()

    if row is None:
        return None
    return {
        "id": row[0], "run_id": row[1], "tenant_id": row[2],
        "rating": row[3], "comment": row[4], "ts": row[5],
    }
