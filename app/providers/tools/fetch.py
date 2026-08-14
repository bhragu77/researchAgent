"""Fetch a URL and extract its main text.

Only domains on the allowlist in `permissions.py` may be fetched. When a search
result already carries raw content (Tavily's `raw_content`, or a fixture's
`content`), that is used directly and no request is made — the network is a last
resort, not the default path.
"""

import logging
import re

from app.config.settings import get_settings
from app.providers.tools.permissions import ToolPermissionError, check_domain, check_tool

logger = logging.getLogger(__name__)

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_main_text(html: str, max_chars: int = 4000) -> str:
    """Strip markup and collapse whitespace.

    Deliberately dependency-free: good enough to turn an article into evidence
    text, and it cannot execute anything it downloads.
    """
    if not html:
        return ""

    # Prefer <article> or <main> when present — they bound the real content.
    for tag in ("article", "main"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.DOTALL | re.IGNORECASE)
        if match:
            html = match.group(1)
            break

    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:max_chars]


def web_fetch(url: str, agent: str = "external", max_chars: int = 4000) -> str:
    """Fetch `url` and return its extracted main text.

    Raises:
        ToolPermissionError: if the agent or the domain is not allowed.
    """
    check_tool(agent, "web_fetch")
    check_domain(url)

    import httpx

    logger.info("web_fetch %s", url)
    response = httpx.get(
        url,
        timeout=get_settings().fetch_timeout_s,
        follow_redirects=True,
        headers={"User-Agent": "ResearchAgent/0.2 (+internal research tool)"},
    )
    response.raise_for_status()

    # A redirect can land on a host that was never authorized.
    check_domain(str(response.url))
    return extract_main_text(response.text, max_chars=max_chars)


def safe_fetch(url: str, agent: str = "external", max_chars: int = 4000) -> str:
    """`web_fetch` that returns empty string instead of raising.

    One unreachable source should degrade the evidence set, not fail the run.
    """
    try:
        return web_fetch(url, agent=agent, max_chars=max_chars)
    except ToolPermissionError as exc:
        logger.info("fetch denied: %s", exc)
    except Exception as exc:  # noqa: BLE001 - network failures are expected
        logger.warning("fetch failed for %s: %s", url, exc)
    return ""
