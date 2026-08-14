"""Conflict validation: shared utilities for processing conflicts the
synthesizer itself detects.

Contradictions are surfaced, not resolved. When an internal measurement and a
vendor claim disagree, the answer should say so and cite both — silently
picking a side is how a research tool becomes untrustworthy.

Conflict detection used to be its own LLM call, run between fusion and
synthesis. It is now folded into the synthesis prompt itself (see
synthesis.txt) — synthesis already reads every piece of evidence to write the
answer, so asking it to also flag contradictions in the same pass costs
nothing extra, where a separate call cost a full round trip (and its own
exposure to provider fallback latency) for a job synthesis was already well
positioned to do. What stays here is the part that must not be left to the
model: checking that every referenced E-id is real, and ranking authority
deterministically so the prose rationale and the ranking can never disagree.
"""

import logging
from typing import Any

from app.knowledge.evidence import Evidence
from app.orchestration.nodes.fusion import AUTHORITY_WEIGHTS

logger = logging.getLogger(__name__)


def validate_conflicts(payload: dict[str, Any], valid_ids: set[str]) -> list[dict[str, Any]]:
    """Keep only conflicts that reference at least two real, distinct E-ids."""
    raw = payload.get("conflicts")
    if not isinstance(raw, list):
        return []

    conflicts: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        between = [
            str(e).strip().upper()
            for e in (entry.get("between") or [])
            if str(e).strip().upper() in valid_ids
        ]
        if len(set(between)) < 2:
            logger.info("dropping conflict with unusable E-ids: %s", entry.get("between"))
            continue
        conflicts.append(
            {
                "between": sorted(set(between), key=lambda c: int(c[1:])),
                "on": str(entry.get("on", "")).strip(),
                "description": str(entry.get("description", "")).strip(),
                "authority_note": str(entry.get("authority_note", "")).strip(),
            }
        )
    return conflicts


def annotate_authority(
    conflicts: list[dict[str, Any]], by_id: dict[str, Evidence]
) -> list[dict[str, Any]]:
    """Attach the deterministic authority ranking of each side.

    The model writes the prose rationale; the ordering itself comes from the
    same weights fusion used, so the two cannot disagree.
    """
    for conflict in conflicts:
        ranked = sorted(
            conflict["between"],
            key=lambda cid: AUTHORITY_WEIGHTS.get(
                by_id[cid].authority.lower() if cid in by_id else "unknown", 0.4
            ),
            reverse=True,
        )
        conflict["authority_ranking"] = [
            {
                "id": cid,
                "authority": by_id[cid].authority if cid in by_id else "unknown",
                "source": by_id[cid].source if cid in by_id else "unknown",
                "date": by_id[cid].date if cid in by_id else "unknown",
            }
            for cid in ranked
        ]
    return conflicts
