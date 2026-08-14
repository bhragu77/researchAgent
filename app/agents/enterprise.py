"""Enterprise agent: searches tenant-internal knowledge.

Phase 1's only agent. Runs hybrid retrieval, fuses, reranks, and returns the
final cited evidence set.
"""

import logging
from typing import Any

from app.agents.base import Agent, AgentCard
from app.config.settings import get_settings
from app.knowledge import rerank as rerank_module
from app.knowledge import retrieval
from app.knowledge.evidence import Evidence
from app.providers.tools.permissions import check_tool

logger = logging.getLogger(__name__)

AGENT_NAME = "enterprise"


class EnterpriseAgent(Agent):
    """Retrieves evidence from the tenant's own documents."""

    card = AgentCard(
        name=AGENT_NAME,
        description="Searches tenant-internal documents via hybrid retrieval.",
    )

    def research(
        self,
        question: str,
        tenant: str,
        retrieve_top_k: int | None = None,
        rerank_top_k: int | None = None,
        datastore_plan: list[str] | None = None,
    ) -> list[Evidence]:
        """Retrieve, fuse, and rerank internal evidence for a question."""
        # Checked here, at the point of use — declaring the allowlist entry is
        # not enforcement.
        check_tool(AGENT_NAME, "hybrid_retrieval")

        settings = get_settings()
        retrieve_top_k = retrieve_top_k or settings.retrieve_top_k
        plan = datastore_plan or ["vector", "bm25"]

        dense = (
            retrieval.dense_search(question, tenant, retrieve_top_k)
            if "vector" in plan
            else []
        )
        lexical = (
            retrieval.bm25_search(question, tenant, retrieve_top_k)
            if "bm25" in plan
            else []
        )
        logger.info(
            "enterprise retrieval: dense=%d bm25=%d (plan=%s)", len(dense), len(lexical), plan
        )

        fused = retrieval.rrf_fuse(dense, lexical, top_k=retrieve_top_k)
        ranked = rerank_module.rerank(question, fused, top_k=rerank_top_k)

        # Tag provenance so fusion can normalize and group per agent.
        for item in ranked:
            item.metadata["agent"] = AGENT_NAME
        return ranked

    async def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Agent-interface entrypoint (unused in Phase 1's synchronous graph)."""
        evidence = self.research(task["question"], context["tenant"])
        return {"evidence": [e.to_dict() for e in evidence]}


def enterprise_node(state: dict[str, Any]) -> dict[str, Any]:
    """Graph node: run internal research and store evidence in state."""
    plan = state.get("route", {})
    evidence = EnterpriseAgent().research(
        question=state["question"],
        tenant=state["tenant"],
        retrieve_top_k=plan.get("retrieve_top_k"),
        rerank_top_k=plan.get("rerank_top_k"),
        datastore_plan=plan.get("datastore_plan"),
    )
    return {
        "evidence": [e.to_dict() for e in evidence],
        "trace": {**state.get("trace", {}), "evidence_count": len(evidence)},
    }
