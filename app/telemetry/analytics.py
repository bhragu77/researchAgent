"""Operational analytics over `research_trace` + `feedback`.

These are metrics about *the system*: latency, cost, CSAT, faithfulness,
abstention, blocks, fallbacks. They are not the client's business metrics —
those live in the `metrics` table and are served by `app.agents.analytics`,
which answers user questions. The two never join.

Every function takes `tenant_id`. Passing None means admin scope (all tenants)
and is only reachable from a principal whose role is admin; the route layer
enforces that, and these functions never default to unscoped.
"""

import logging
from typing import Any

from app.knowledge.retrieval import connect
from app.telemetry.persistence import FEEDBACK_TABLE, TRACE_TABLE

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 30


def _scope(tenant_id: str | None, alias: str = "t") -> tuple[str, list[Any]]:
    """Build the tenant predicate. None = admin scope, all tenants."""
    if tenant_id is None:
        return "", []
    return f" AND {alias}.tenant_id = %s", [tenant_id]


def _window(days: int, alias: str = "t") -> str:
    return f"{alias}.ts >= now() - interval '{int(days)} days'"


def csat_trend(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Daily mean rating and response count from the feedback table."""
    predicate, params = _scope(tenant_id, "f")
    sql = f"""
        SELECT date_trunc('day', f.ts)::date AS day,
               ROUND(AVG(f.rating)::numeric, 2) AS avg_rating,
               COUNT(*) AS responses
        FROM {FEEDBACK_TABLE} f
        WHERE {_window(days, 'f')}{predicate}
        GROUP BY day ORDER BY day
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        {"day": str(r[0]), "avg_rating": float(r[1]), "responses": int(r[2])} for r in rows
    ]


def faithfulness_trend(
    tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS
) -> list[dict[str, Any]]:
    """Daily mean inline faithfulness across answered runs."""
    predicate, params = _scope(tenant_id)
    sql = f"""
        SELECT date_trunc('day', t.ts)::date AS day,
               ROUND(AVG(t.faithfulness)::numeric, 4) AS avg_faithfulness,
               COUNT(*) AS runs
        FROM {TRACE_TABLE} t
        WHERE {_window(days)} AND t.faithfulness IS NOT NULL{predicate}
        GROUP BY day ORDER BY day
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        {"day": str(r[0]), "avg_faithfulness": float(r[1]), "runs": int(r[2])} for r in rows
    ]


def latency_p50_p95(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Latency percentiles. Computed in Postgres, not in Python over a page."""
    predicate, params = _scope(tenant_id)
    sql = f"""
        SELECT COUNT(*),
               ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY t.latency_ms)::numeric, 1),
               ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY t.latency_ms)::numeric, 1),
               ROUND(percentile_cont(0.99) WITHIN GROUP (ORDER BY t.latency_ms)::numeric, 1),
               ROUND(AVG(t.latency_ms)::numeric, 1),
               ROUND(MAX(t.latency_ms)::numeric, 1)
        FROM {TRACE_TABLE} t
        WHERE {_window(days)} AND t.latency_ms IS NOT NULL{predicate}
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()

    count = int(row[0] or 0)
    if not count:
        return {"runs": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "mean_ms": None, "max_ms": None}
    return {
        "runs": count,
        "p50_ms": float(row[1]), "p95_ms": float(row[2]), "p99_ms": float(row[3]),
        "mean_ms": float(row[4]), "max_ms": float(row[5]),
    }


def est_cost_per_tenant(
    tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS
) -> list[dict[str, Any]]:
    """Estimated spend, tokens, and run count grouped by tenant."""
    predicate, params = _scope(tenant_id)
    sql = f"""
        SELECT t.tenant_id,
               COUNT(*) AS runs,
               ROUND(SUM(t.est_cost_usd)::numeric, 6) AS est_cost_usd,
               SUM(t.tokens_in) AS tokens_in,
               SUM(t.tokens_out) AS tokens_out,
               ROUND(AVG(t.est_cost_usd)::numeric, 6) AS avg_cost_per_run
        FROM {TRACE_TABLE} t
        WHERE {_window(days)}{predicate}
        GROUP BY t.tenant_id ORDER BY est_cost_usd DESC
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        {
            "tenant_id": r[0], "runs": int(r[1]), "est_cost_usd": float(r[2] or 0),
            "tokens_in": int(r[3] or 0), "tokens_out": int(r[4] or 0),
            "avg_cost_per_run": float(r[5] or 0),
        }
        for r in rows
    ]


def query_type_mix(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Distribution of runs by classified intent."""
    predicate, params = _scope(tenant_id)
    sql = f"""
        SELECT COALESCE(t.intent, 'unknown') AS intent,
               COUNT(*) AS runs,
               ROUND(AVG(t.confidence)::numeric, 3) AS avg_confidence
        FROM {TRACE_TABLE} t
        WHERE {_window(days)}{predicate}
        GROUP BY intent ORDER BY runs DESC
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    total = sum(int(r[1]) for r in rows) or 1
    return [
        {
            "intent": r[0], "runs": int(r[1]),
            "share": round(int(r[1]) / total, 4),
            "avg_confidence": float(r[2]) if r[2] is not None else None,
        }
        for r in rows
    ]


def _verdict_rate(tenant_id: str | None, verdict: str, days: int) -> dict[str, Any]:
    """Share of runs ending on a given verdict."""
    predicate, params = _scope(tenant_id)
    sql = f"""
        SELECT COUNT(*) FILTER (WHERE t.verdict = %s), COUNT(*)
        FROM {TRACE_TABLE} t WHERE {_window(days)}{predicate}
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (verdict, *params))
        matched, total = cur.fetchone()

    matched, total = int(matched or 0), int(total or 0)
    return {
        "verdict": verdict,
        "count": matched,
        "total_runs": total,
        "rate": round(matched / total, 4) if total else 0.0,
    }


def abstention_rate(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Share of runs the reliability gate withheld an answer on."""
    return _verdict_rate(tenant_id, "abstained", days)


def block_rate(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Share of runs the injection guardrail blocked."""
    return _verdict_rate(tenant_id, "blocked", days)


def fallback_rate(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Share of runs that needed at least one provider fallback hop."""
    predicate, params = _scope(tenant_id)
    sql = f"""
        SELECT COUNT(*) FILTER (WHERE jsonb_array_length(t.fallbacks) > 0),
               COUNT(*),
               COALESCE(SUM(jsonb_array_length(t.fallbacks)), 0),
               COUNT(*) FILTER (WHERE t.verdict = 'degraded')
        FROM {TRACE_TABLE} t WHERE {_window(days)}{predicate}
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        with_fallback, total, hops, degraded = cur.fetchone()

    total = int(total or 0)
    return {
        "runs_with_fallback": int(with_fallback or 0),
        "total_runs": total,
        "rate": round(int(with_fallback or 0) / total, 4) if total else 0.0,
        "total_hops": int(hops or 0),
        "degraded_runs": int(degraded or 0),
    }


def summary(tenant_id: str | None, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Everything the dashboard needs, in one round of queries."""
    return {
        "scope": tenant_id or "all-tenants",
        "window_days": days,
        "csat_trend": csat_trend(tenant_id, days),
        "faithfulness_trend": faithfulness_trend(tenant_id, days),
        "latency": latency_p50_p95(tenant_id, days),
        "cost_per_tenant": est_cost_per_tenant(tenant_id, days),
        "query_type_mix": query_type_mix(tenant_id, days),
        "abstention_rate": abstention_rate(tenant_id, days),
        "block_rate": block_rate(tenant_id, days),
        "fallback_rate": fallback_rate(tenant_id, days),
    }
