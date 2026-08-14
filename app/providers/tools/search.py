"""Pluggable web search.

Uses Tavily when `TAVILY_API_KEY` is set. Otherwise falls back to a fixture
loader reading `data/fixtures/external/*.json`, so demos and tests never depend
on a live, rate-limited, or flaky third-party call. Both backends return the
same `SearchResult` shape, so callers cannot tell them apart.
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.settings import get_settings
from app.providers.tools.permissions import check_tool

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """One hit from a web search backend."""

    title: str
    url: str
    snippet: str
    published: str = "unknown"
    source: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class SearchBackend(ABC):
    """Interface for a web search provider."""

    name: str

    @abstractmethod
    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return ranked results for a query."""
        raise NotImplementedError


class TavilyBackend(SearchBackend):
    """Live web search via Tavily."""

    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        import httpx

        # `content` is Tavily's own pre-summarized snippet -- fast, and the
        # entire reason to use Tavily over a raw search API in the first
        # place. `include_raw_content` was previously set to True, which
        # makes Tavily additionally crawl and extract each result's full page
        # server-side; measured at ~14s for 4 results versus ~1-3s without
        # it, and was the dominant contributor to 50-100s end-to-end research
        # latency. Left off (Tavily defaults to False).
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._api_key,
                "query": query,
                "max_results": max_results,
            },
            timeout=get_settings().search_timeout_s,
        )
        response.raise_for_status()
        payload = response.json()

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                published=item.get("published_date", "unknown"),
                source=item.get("url", ""),
                score=float(item.get("score", 0.0)),
                metadata={"backend": "tavily"},
            )
            for item in payload.get("results", [])
        ]


class FixtureBackend(SearchBackend):
    """Offline search over `data/fixtures/external/*.json`.

    Each fixture file holds a list of documents. Matching is a simple token
    overlap score — enough to make the right fixture surface for a given
    sub-question without pretending to be a real ranker.
    """

    name = "fixtures"

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir

    def _load(self) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        if not self.fixtures_dir.exists():
            logger.warning("fixtures dir %s does not exist", self.fixtures_dir)
            return docs

        for path in sorted(self.fixtures_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                logger.warning("skipping malformed fixture %s: %s", path.name, exc)
                continue
            entries = payload if isinstance(payload, list) else payload.get("results", [])
            for entry in entries:
                entry.setdefault("_fixture", path.name)
                docs.append(entry)
        return docs

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        terms = {t for t in query.lower().split() if len(t) > 3}
        scored: list[tuple[float, dict[str, Any]]] = []

        for doc in self._load():
            haystack = " ".join(
                str(doc.get(k, "")) for k in ("title", "snippet", "content", "keywords")
            ).lower()
            overlap = sum(1 for t in terms if t in haystack)
            if overlap:
                scored.append((overlap / max(len(terms), 1), doc))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        return [
            SearchResult(
                title=doc.get("title", ""),
                url=doc.get("url", ""),
                # Fixtures carry a fuller "content" alongside the short
                # "snippet" -- prefer it, since reading it costs nothing
                # (a local file, not a network fetch) unlike the live
                # backend's now-removed raw-content path.
                snippet=doc.get("content") or doc.get("snippet", ""),
                published=str(doc.get("published", "unknown")),
                source=doc.get("source", doc.get("url", "")),
                score=score,
                metadata={"backend": "fixtures", "fixture": doc.get("_fixture", "")},
            )
            for score, doc in scored[:max_results]
        ]


def get_backend() -> SearchBackend:
    """Select the search backend from configuration."""
    settings = get_settings()
    if settings.tavily_api_key:
        logger.info("web search backend: tavily")
        return TavilyBackend(settings.tavily_api_key)
    logger.info("web search backend: fixtures (no TAVILY_API_KEY set)")
    return FixtureBackend(Path(settings.fixtures_dir))


def web_search(query: str, max_results: int | None = None, agent: str = "external") -> list[SearchResult]:
    """Allowlist-checked entry point for web search."""
    check_tool(agent, "web_search")
    max_results = max_results or get_settings().external_max_results
    results = get_backend().search(query, max_results)
    logger.info("web_search(%r) -> %d results", query, len(results))
    return results
