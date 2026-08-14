"""External agent: searches public/market sources.

Runs allowlisted web search and returns normalized `Evidence` carrying real
provenance (url, publication date, source name). Authority is fixed at
"external" so downstream fusion and conflict detection can weigh these
against higher-authority internal documents.

Uses the search backend's own snippet directly rather than fetching each
result's full page — the snippet (Tavily's `content`, pre-summarized for LLM
consumption) is the entire reason to use a search API built for agents rather
than scraping pages ourselves. An earlier version fetched every result's raw
page as a sequential fallback; that was the dominant contributor to 50-100s
end-to-end research latency (up to ~10s per item, one at a time, on top of an
already-slow raw-content search call). Removed rather than parallelized: the
snippet is normally sufficient, so there is no good reason to pay for a fetch
most of the time just to make the rare case faster.

No tenant data is included in outbound queries — only the sub-question text.
"""

import logging
from typing import Any

from app.agents.base import Agent, AgentCard
from app.config.settings import get_settings
from app.knowledge.evidence import Evidence
from app.providers.tools.search import SearchResult, web_search

logger = logging.getLogger(__name__)

AGENT_NAME = "external"

# A snippet has no length ceiling of its own -- unusual, but not impossible,
# for a backend to return one. Internal chunks are bounded at ingestion
# (CHUNK_SIZE_CHARS); external text needs the same discipline applied here,
# or a handful of long items can push a single request past a provider's
# per-request token limit outright — observed as Groq rejecting the whole
# request ("reduce the length of the messages") regardless of which model in
# the fallback chain served it.
_MAX_TEXT_CHARS = 2000


def _truncate(text: str, limit: int = _MAX_TEXT_CHARS) -> str:
    """Cap one evidence item's text, breaking on a word boundary."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …[truncated]"


def _to_evidence(result: SearchResult, text: str) -> Evidence:
    """Convert a search hit plus its extracted text into Evidence."""
    return Evidence(
        text=text,
        source=result.source or result.url or result.title,
        authority="external",
        date=result.published or "unknown",
        score=result.score,
        chunk_id=f"ext:{result.url}",
        metadata={
            "agent": AGENT_NAME,
            "url": result.url,
            "title": result.title,
            "backend": result.metadata.get("backend", "unknown"),
        },
    )


class ExternalAgent(Agent):
    """Retrieves evidence from public web and market research sources."""

    card = AgentCard(
        name=AGENT_NAME,
        description="Searches public web and market research sources for external evidence.",
        skills=[
            {
                "id": "external_research",
                "name": "External Research",
                "description": "Search public sources and return cited evidence for a sub-question.",
            }
        ],
    )

    def research(self, subquestion: str, max_results: int | None = None) -> list[Evidence]:
        """Search and normalize external evidence from the backend's own snippets."""
        settings = get_settings()
        max_results = max_results or settings.external_max_results

        results = web_search(subquestion, max_results=max_results, agent=AGENT_NAME)

        evidence: list[Evidence] = []
        for result in results:
            text = result.snippet.strip()
            if not text:
                continue
            evidence.append(_to_evidence(result, _truncate(text)))

        logger.info("external research %r -> %d evidence", subquestion, len(evidence))
        return evidence

    async def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Agent-interface entrypoint used by the A2A executor."""
        evidence = self.research(task["question"])
        return {"evidence": [e.to_dict() for e in evidence]}
