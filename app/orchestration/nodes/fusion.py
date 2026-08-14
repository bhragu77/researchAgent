"""Fusion node: merges evidence from every agent into one ranked, cited set.

Each agent scores on its own scale — reranker probabilities, RRF sums, a flat
1.0 from analytics. Comparing those raw would let one agent's scale dominate,
so scores are normalized within each agent before they are combined.

Also computes `retrieval_signals`, the retrieval-side quality evidence that
Phase 3's confidence model will consume instead of relying on the synthesis
model's self-reported confidence.
"""

import logging
import re
from collections import defaultdict
from typing import Any

from app.knowledge.evidence import Evidence, normalize_scores
from app.tenancy.isolation import assert_same_tenant

logger = logging.getLogger(__name__)

# Authority ranking used to weight evidence and to break ties in conflicts.
AUTHORITY_WEIGHTS = {"high": 1.0, "medium": 0.7, "external": 0.5, "low": 0.3, "unknown": 0.4}

# Fraction of the fused score contributed by the retrieval score vs. the
# authority/freshness prior. Retrieval relevance still dominates.
_RETRIEVAL_WEIGHT = 0.7
_PRIOR_WEIGHT = 0.3

# Backstop on the total evidence set reaching synthesis, independent of any
# single agent's top_k. A broad multi-part question can pull evidence from
# several sub-questions across several agents, and per-agent limits do not
# bound that sum -- observed as a request degrading outright (every provider
# rejecting an oversized prompt) rather than degrading gracefully. Ranking
# already runs first, so this drops the least relevant items, not arbitrary
# ones.
_MAX_FUSED_EVIDENCE = 20

_YEAR_RE = re.compile(r"(20\d{2})")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Content tokens used for near-duplicate detection."""
    return {t for t in _WORD_RE.findall(text.lower()) if len(t) > 3}


def jaccard(a: set[str], b: set[str]) -> float:
    """Token overlap ratio between two texts."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def freshness_score(date: str, newest_year: int | None = None) -> float:
    """Score recency from any date string containing a year.

    Deliberately coarse: evidence dates arrive as "2026-Q1", "2026-01-19", and
    "unknown", and a year is the only field reliably present across all of them.
    """
    match = _YEAR_RE.search(str(date))
    if not match:
        return 0.4
    year = int(match.group(1))
    newest = newest_year or year
    age = max(0, newest - year)
    return max(0.2, 1.0 - 0.2 * age)


def dedupe(items: list[Evidence], threshold: float = 0.85) -> list[Evidence]:
    """Drop near-duplicate passages, keeping the highest-scoring copy.

    Agents overlap by design — the same document can surface for several
    sub-questions — so this runs after merging rather than inside each agent.
    """
    ordered = sorted(items, key=lambda e: e.score, reverse=True)
    kept: list[Evidence] = []
    kept_tokens: list[set[str]] = []

    for item in ordered:
        tokens = _tokens(item.text)
        duplicate_of = None
        for index, existing in enumerate(kept_tokens):
            if jaccard(tokens, existing) >= threshold:
                duplicate_of = index
                break

        if duplicate_of is None:
            kept.append(item)
            kept_tokens.append(tokens)
        else:
            # Record that another agent independently surfaced this passage —
            # corroboration is a signal, not noise.
            survivor = kept[duplicate_of]
            agents = set(survivor.metadata.get("also_found_by", []))
            agents.add(item.metadata.get("agent", "unknown"))
            survivor.metadata["also_found_by"] = sorted(agents)

    dropped = len(items) - len(kept)
    if dropped:
        logger.info("fusion: dropped %d near-duplicate evidence items", dropped)
    return kept


def compute_signals(evidence: list[Evidence]) -> dict[str, Any]:
    """Retrieval-side quality signals for downstream confidence scoring."""
    if not evidence:
        return {
            "top_rerank_score": 0.0,
            "score_spread": 0.0,
            "num_sources": 0,
            "source_agreement": 0.0,
            "num_evidence": 0,
            "mean_authority": 0.0,
        }

    scores = [e.score for e in evidence]
    sources = {e.source for e in evidence}
    agents = {e.metadata.get("agent", "unknown") for e in evidence}

    # Agreement proxy: how much independent corroboration exists. Evidence
    # surfaced by more than one agent, or repeated across distinct sources,
    # raises it.
    corroborated = sum(1 for e in evidence if e.metadata.get("also_found_by"))
    agreement = (corroborated / len(evidence)) if evidence else 0.0
    if len(agents) > 1:
        agreement = min(1.0, agreement + 0.25 * (len(agents) - 1))

    # Mean authority of the evidence actually used. Consumed by
    # `app.reliability.confidence`, which weights whose evidence it is.
    mean_authority = sum(
        AUTHORITY_WEIGHTS.get(e.authority.lower(), 0.4) for e in evidence
    ) / len(evidence)

    return {
        "top_rerank_score": round(max(scores), 4),
        "score_spread": round(max(scores) - min(scores), 4),
        "num_sources": len(sources),
        "source_agreement": round(agreement, 4),
        "num_evidence": len(evidence),
        "num_agents": len(agents),
        "mean_authority": round(mean_authority, 4),
    }


def datastores_touched(evidence: list[Evidence], tenant: str) -> dict[str, Any]:
    """Report which datastores actually served this request, and under which tenant.

    Derived from evidence provenance rather than from the plan, so it shows what
    ran instead of what was requested — the two differ whenever a planned store
    returns nothing.
    """
    counts: dict[str, int] = defaultdict(int)

    for item in evidence:
        agent = item.metadata.get("agent", "enterprise")
        if agent == "analytics":
            counts["analytics_sql"] += 1
        elif agent == "external":
            counts["external"] += 1
        else:
            for retriever in item.metadata.get("retrievers", []) or []:
                counts["vector" if retriever == "dense" else retriever] += 1

    stores = ("vector", "bm25", "analytics_sql", "external")
    return {
        store: {
            "used": counts.get(store, 0) > 0,
            "hits": counts.get(store, 0),
            # External sources are public material, not tenant-owned data; the
            # rest are scoped to this tenant.
            "scope": "public" if store == "external" else tenant,
        }
        for store in stores
    }


def compute_dimension_coverage(
    requirement_dimensions: list[str],
    agent_calls_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Which of the question's requested requirement dimensions were actually
    researched, versus dispatched-but-empty or never attempted at all.

    A dimension counts as covered only if a sub-question tagged with it
    returned at least one evidence item -- being dispatched is not being
    answered. This is what lets the answer stay honest on a broad question: it
    is the difference between "we don't know about RBAC" and silently not
    mentioning RBAC at all.
    """
    if not requirement_dimensions:
        return {"dimensions": [], "researched": [], "missing": [], "ratio": 1.0}

    evidence_by_dimension: dict[str, int] = defaultdict(int)
    for record in agent_calls_log:
        dimension = str(record.get("dimension") or "").strip()
        if dimension:
            evidence_by_dimension[dimension] += int(record.get("evidence_count", 0) or 0)

    researched = [d for d in requirement_dimensions if evidence_by_dimension.get(d, 0) > 0]
    missing = [d for d in requirement_dimensions if d not in researched]

    return {
        "dimensions": requirement_dimensions,
        "researched": researched,
        "missing": missing,
        "ratio": round(len(researched) / len(requirement_dimensions), 4),
        "evidence_by_dimension": dict(evidence_by_dimension),
    }


# Below this share of the strongest compared entity's evidence count, an
# entity counts as under-researched for symmetry purposes. 0.4 means one
# option cannot win the comparison on evidence volume alone by more than
# roughly 2.5x its weakest rival.
ENTITY_SYMMETRY_FLOOR = 0.4


def compute_entity_symmetry(
    compared_entities: list[str],
    agent_calls_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Whether every entity being compared got a comparably-sized amount of
    research, not just whichever one turned out easiest to find evidence for.

    "Google has more retrieved evidence" must not silently become "Google is
    better" -- this tracks evidence volume per entity (from sub-questions
    tagged with an "entity" field) and flags any entity whose share falls
    well below the strongest one, so a lopsided comparison can be named as
    such rather than quietly producing a confident-sounding winner.
    """
    if not compared_entities:
        return {
            "entities": [], "evidence_by_entity": {}, "shares": {},
            "weakest": [], "symmetric": True, "min_share": 1.0,
        }

    evidence_by_entity: dict[str, int] = defaultdict(int)
    for record in agent_calls_log:
        entity = str(record.get("entity") or "").strip()
        if entity:
            evidence_by_entity[entity] += int(record.get("evidence_count", 0) or 0)

    max_count = max((evidence_by_entity.get(e, 0) for e in compared_entities), default=0)
    if max_count == 0:
        # No entity-tagged evidence at all -- symmetry cannot be assessed, and
        # that is itself worth flagging rather than defaulting to "fine".
        return {
            "entities": compared_entities, "evidence_by_entity": {}, "shares": {},
            "weakest": compared_entities, "symmetric": False, "min_share": 0.0,
        }

    shares = {e: round(evidence_by_entity.get(e, 0) / max_count, 4) for e in compared_entities}
    weakest = [e for e in compared_entities if shares[e] < ENTITY_SYMMETRY_FLOOR]

    return {
        "entities": compared_entities,
        "evidence_by_entity": dict(evidence_by_entity),
        "shares": shares,
        "weakest": weakest,
        "symmetric": not weakest,
        "min_share": min(shares.values()),
    }


def fusion_node(state: dict[str, Any]) -> dict[str, Any]:
    """Merge, normalize, dedupe, rank, and assign global E-ids."""
    raw = [Evidence.from_dict(e) for e in state.get("evidence", [])]
    route = state.get("route", {})
    dimensions = route.get("requirement_dimensions", [])
    compared_entities = route.get("compared_entities", [])
    coverage = compute_dimension_coverage(dimensions, state.get("agent_calls_log", []))
    symmetry = compute_entity_symmetry(compared_entities, state.get("agent_calls_log", []))

    if not raw:
        logger.warning("fusion: no evidence to merge")
        return {
            "evidence": [],
            "retrieval_signals": compute_signals([]),
            "coverage": coverage,
            "entity_symmetry": symmetry,
            "trace": {
                **state.get("trace", {}),
                "fusion": {"input": 0, "output": 0},
                "coverage": coverage,
                "entity_symmetry": symmetry,
            },
        }

    # Normalize within each agent so incompatible score scales can be compared.
    by_agent: dict[str, list[Evidence]] = defaultdict(list)
    for item in raw:
        by_agent[item.metadata.get("agent", "enterprise")].append(item)
    for agent, items in by_agent.items():
        normalize_scores(items)
        logger.info("fusion: normalized %d items from %s", len(items), agent)

    merged = [item for items in by_agent.values() for item in items]
    deduped = dedupe(merged)

    years = [int(m.group(1)) for m in (_YEAR_RE.search(str(e.date)) for e in deduped) if m]
    newest_year = max(years) if years else None

    # Blend retrieval relevance with an authority/freshness prior.
    for item in deduped:
        authority = AUTHORITY_WEIGHTS.get(item.authority.lower(), 0.4)
        freshness = freshness_score(item.date, newest_year)
        prior = (authority + freshness) / 2
        item.metadata["authority_weight"] = round(authority, 3)
        item.metadata["freshness"] = round(freshness, 3)
        item.metadata["retrieval_score"] = round(item.score, 4)
        item.score = round(_RETRIEVAL_WEIGHT * item.score + _PRIOR_WEIGHT * prior, 4)

    ranked = sorted(deduped, key=lambda e: e.score, reverse=True)
    if len(ranked) > _MAX_FUSED_EVIDENCE:
        logger.info(
            "fusion: capping %d ranked items to top %d", len(ranked), _MAX_FUSED_EVIDENCE
        )
        ranked = ranked[:_MAX_FUSED_EVIDENCE]
    for position, item in enumerate(ranked, start=1):
        item.id = f"E{position}"

    # Defence in depth: several agents have contributed by now. If any retrieval
    # path ever lost its scope, catch the mixed set here rather than letting it
    # be cited into an answer.
    assert_same_tenant([e.to_dict() for e in ranked], state.get("tenant"))

    signals = compute_signals(ranked)
    per_source = defaultdict(int)
    for item in ranked:
        per_source[item.metadata.get("agent", "unknown")] += 1

    logger.info(
        "fusion: %d raw -> %d fused across %d sources", len(raw), len(ranked), signals["num_sources"]
    )

    used = datastores_touched(ranked, str(state.get("tenant", "")))
    logger.info(
        "fusion: datastores used %s (tenant=%s)",
        [s for s, info in used.items() if info["used"]],
        state.get("tenant"),
    )

    if coverage["missing"]:
        logger.info(
            "fusion: %d/%d requirement dimensions uncovered: %s",
            len(coverage["missing"]),
            len(coverage["dimensions"]),
            coverage["missing"],
        )
    if symmetry["weakest"]:
        logger.info(
            "fusion: entity comparison asymmetric, under-researched: %s (shares=%s)",
            symmetry["weakest"], symmetry.get("shares"),
        )

    return {
        "evidence": [e.to_dict() for e in ranked],
        "retrieval_signals": signals,
        "evidence_by_agent": dict(per_source),
        "datastores_used": used,
        "coverage": coverage,
        "entity_symmetry": symmetry,
        "trace": {
            **state.get("trace", {}),
            "fusion": {
                "input": len(raw),
                "output": len(ranked),
                "deduped": len(merged) - len(deduped),
                "by_agent": dict(per_source),
            },
            "retrieval_signals": signals,
            "coverage": coverage,
            "entity_symmetry": symmetry,
        },
    }
