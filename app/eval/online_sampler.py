"""Online evaluation: re-judge a sample of live runs, out of band.

Inline faithfulness is produced by the same request that produced the answer,
under the same conditions. Re-judging a sample later, independently, is what
catches drift the inline judge misses — a retrieval change, a prompt edit, or a
model swap that quietly degrades grounding.

Nothing here runs on the request path. `schedule_sample` hands work to a
background thread and returns immediately; the CLI entry point (`main`) sweeps
recent runs in bulk.
"""

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.knowledge.retrieval import connect
from app.reliability import groundedness
from app.telemetry import persistence, tracing

logger = logging.getLogger(__name__)

ONLINE_TABLE = "online_eval"


def should_sample(run_id: str, rate: float | None = None) -> bool:
    """Deterministically decide whether a run is sampled.

    Hashing the run id rather than calling `random` makes the decision stable:
    the same run is either always sampled or never, so a retry cannot flip it
    and a sampled set is reproducible.
    """
    rate = get_settings().online_sample_rate if rate is None else rate
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    bucket = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def _store_result(
    run_id: str,
    tenant_id: str,
    online_score: float,
    inline_score: float | None,
    flagged: bool,
    detail: dict[str, Any],
) -> None:
    """Write the online score alongside the inline one."""
    delta = None if inline_score is None else round(online_score - inline_score, 4)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {ONLINE_TABLE}
                (run_id, tenant_id, online_faithfulness, inline_faithfulness, delta, flagged, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (run_id) DO UPDATE SET
                online_faithfulness = EXCLUDED.online_faithfulness,
                inline_faithfulness = EXCLUDED.inline_faithfulness,
                delta = EXCLUDED.delta,
                flagged = EXCLUDED.flagged,
                detail = EXCLUDED.detail,
                ts = now();
            """,  # noqa: S608 - table name is a module constant
            (run_id, tenant_id, online_score, inline_score, delta, flagged, json.dumps(detail)),
        )
        conn.commit()


def append_failure(record: dict[str, Any]) -> Path:
    """Append a failing case to the online-failures feed.

    This file is the input to golden-set promotion: a human reviews it and moves
    confirmed regressions into `golden_set.jsonl`. It is deliberately append-only
    and never auto-promoted.
    """
    path = Path(get_settings().online_failure_feed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    logger.warning("online eval failure appended: run=%s", record.get("run_id"))
    return path


def rejudge_run(run_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
    """Re-run the groundedness judge over a stored run.

    Returns None when the run cannot be re-judged — it was not answered, or the
    answer/evidence needed to judge it was not retained.
    """
    run = persistence.get_run(run_id, tenant_id=tenant_id)
    if run is None:
        logger.info("online eval: run %s not found", run_id)
        return None
    if run["verdict"] != "answered":
        logger.debug("online eval: skipping %s (verdict=%s)", run_id, run["verdict"])
        return None

    extra = run.get("extra") or {}
    answer = extra.get("answer_snapshot")
    evidence = extra.get("evidence_snapshot")
    if not answer or not evidence:
        logger.info("online eval: run %s has no snapshot to re-judge", run_id)
        return None

    settings = get_settings()
    assessment = groundedness.assess(answer, evidence)
    online_score = float(assessment.get("faithfulness_score", 0.0))
    inline_score = run.get("faithfulness")
    flagged = online_score < settings.online_drift_threshold

    detail = {
        "unsupported_claims": assessment.get("unsupported_claims", []),
        "citation_valid": assessment.get("citation_valid"),
        "verdict": assessment.get("verdict"),
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }
    _store_result(run_id, run["tenant_id"], online_score, inline_score, flagged, detail)
    tracing.score_run(run_id, "online_faithfulness", online_score, "online re-judge")

    result = {
        "run_id": run_id,
        "tenant_id": run["tenant_id"],
        "online_faithfulness": online_score,
        "inline_faithfulness": inline_score,
        "delta": None if inline_score is None else round(online_score - inline_score, 4),
        "flagged": flagged,
        **detail,
    }

    if flagged:
        append_failure(
            {
                "run_id": run_id,
                "tenant_id": run["tenant_id"],
                "question_hash": run["question_hash"],
                "intent": run["intent"],
                "online_faithfulness": online_score,
                "inline_faithfulness": inline_score,
                "unsupported_claims": detail["unsupported_claims"],
                "threshold": settings.online_drift_threshold,
                "ts": detail["judged_at"],
                "promote_to_golden": False,
            }
        )

    return result


def drift_metric(tenant_id: str | None = None, window: int | None = None) -> dict[str, Any]:
    """Rolling online faithfulness over the last `window` scored runs.

    Flags when the rolling mean falls below the configured floor. Reported
    alongside the inline mean so a gap between the two is visible — that gap,
    not the absolute value, is what indicates the inline judge has drifted.
    """
    settings = get_settings()
    window = window or settings.online_drift_window
    threshold = settings.online_drift_threshold

    sql = f"""
        SELECT online_faithfulness, inline_faithfulness, flagged
        FROM {ONLINE_TABLE}
        {{where}}
        ORDER BY ts DESC LIMIT %s
    """  # noqa: S608
    params: list[Any] = []
    where = ""
    if tenant_id is not None:
        where = "WHERE tenant_id = %s"
        params.append(tenant_id)
    params.append(window)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql.format(where=where), tuple(params))
        rows = cur.fetchall()

    if not rows:
        return {
            "scope": tenant_id or "all-tenants",
            "window": window,
            "scored_runs": 0,
            "rolling_online_faithfulness": None,
            "rolling_inline_faithfulness": None,
            "threshold": threshold,
            "drift_detected": False,
            "flagged_runs": 0,
        }

    online = [float(r[0]) for r in rows if r[0] is not None]
    inline = [float(r[1]) for r in rows if r[1] is not None]
    rolling_online = round(sum(online) / len(online), 4) if online else None
    rolling_inline = round(sum(inline) / len(inline), 4) if inline else None

    return {
        "scope": tenant_id or "all-tenants",
        "window": window,
        "scored_runs": len(rows),
        "rolling_online_faithfulness": rolling_online,
        "rolling_inline_faithfulness": rolling_inline,
        "inline_vs_online_gap": (
            None if rolling_online is None or rolling_inline is None
            else round(rolling_inline - rolling_online, 4)
        ),
        "threshold": threshold,
        "drift_detected": rolling_online is not None and rolling_online < threshold,
        "flagged_runs": sum(1 for r in rows if r[2]),
    }


def sweep(tenant_id: str | None = None, limit: int = 50, force: bool = False) -> dict[str, Any]:
    """Re-judge the sampled share of recent answered runs."""
    runs = persistence.recent_runs(tenant_id=tenant_id, limit=limit, verdict="answered")
    selected = [r for r in runs if force or should_sample(r["run_id"])]

    logger.info("online eval sweep: %d/%d runs sampled", len(selected), len(runs))

    results, errors = [], []
    for run in selected:
        try:
            outcome = rejudge_run(run["run_id"], tenant_id=run["tenant_id"])
            if outcome:
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
            logger.exception("online eval failed for %s", run["run_id"])
            errors.append({"run_id": run["run_id"], "error": str(exc)})

    return {
        "candidates": len(runs),
        "sampled": len(selected),
        "scored": len(results),
        "flagged": sum(1 for r in results if r["flagged"]),
        "errors": errors,
        "drift": drift_metric(tenant_id),
        "results": results,
    }


def schedule_sample(run_id: str, tenant_id: str) -> bool:
    """Queue one run for out-of-band re-judging. Never blocks the caller.

    Returns whether the run was sampled. The work runs on a daemon thread so a
    slow judge call cannot extend the user's request, and a process exit cannot
    be held open by it.
    """
    if not should_sample(run_id):
        return False

    def _work() -> None:
        try:
            rejudge_run(run_id, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 - background work must not raise
            logger.exception("background online eval failed for %s", run_id)

    threading.Thread(target=_work, name=f"online-eval-{run_id[:8]}", daemon=True).start()
    logger.info("online eval scheduled for run %s", run_id)
    return True


def main() -> int:
    """CLI: python -m app.eval.online_sampler [--tenant X] [--force]"""
    import argparse

    parser = argparse.ArgumentParser(description="Re-judge a sample of recent runs.")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--force", action="store_true", help="ignore the sample rate")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = sweep(tenant_id=args.tenant, limit=args.limit, force=args.force)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
