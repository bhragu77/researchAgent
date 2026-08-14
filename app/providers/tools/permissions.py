"""Tool allowlisting per agent, and domain allowlisting for outbound fetches.

Deny by default. Two independent gates:

1. Which tools an agent may call at all — so the analytics agent can never
   reach the web, and the external agent can never reach the database.
2. Which domains `web_fetch` may retrieve — so a search result pointing at an
   arbitrary host cannot make the process fetch it.

Both checks run at call time, not just when tool specs are generated.
"""

import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class ToolPermissionError(PermissionError):
    """Raised when an agent invokes a tool or domain it is not allowed."""


# Every denial is recorded, not just raised. A caller that swallows the
# exception (see `safe_fetch`) would otherwise make an enforcement event
# invisible, and "the allowlist is working" is a claim that needs evidence.
_DENIALS: list[dict[str, Any]] = []


def record_denial(kind: str, principal: str, target: str, reason: str) -> None:
    """Log and retain one denial for the run trace."""
    entry = {"kind": kind, "principal": principal, "target": target, "reason": reason}
    _DENIALS.append(entry)
    logger.warning("PERMISSION DENIED [%s] %s -> %s: %s", kind, principal, target, reason)


def denial_log() -> list[dict[str, Any]]:
    """Return denials recorded so far this process."""
    return list(_DENIALS)


def clear_denials() -> None:
    """Reset the denial log (used between eval cases and in tests)."""
    _DENIALS.clear()


# Per-agent tool allowlist.
AGENT_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "enterprise": {"hybrid_retrieval"},
    "external": {"web_search", "web_fetch"},
    "analytics": {"sql_metrics"},
}

# Hosts `web_fetch` may retrieve. Subdomains are allowed; everything else is not.
DOMAIN_ALLOWLIST: set[str] = {
    "example.com",
    "gartner.com",
    "forrester.com",
    "mckinsey.com",
    "deloitte.com",
    "pwc.com",
    "ey.com",
    "kpmg.com",
    "hbr.org",
    "ifs.org.uk",
    "gov.uk",
    "iso.org",
    "sap.com",
    "oracle.com",
    "workday.com",
}


def check_tool(agent: str, tool_name: str) -> None:
    """Authorize an agent to invoke a tool. Raises on denial.

    Called at the top of every tool entry point, so an agent that reaches a
    tool by any path — direct import, registry lookup, model-chosen call — is
    checked at the moment of use rather than only when its specs were built.
    """
    allowed = AGENT_TOOL_ALLOWLIST.get(agent, set())
    if tool_name not in allowed:
        reason = f"not in allowlist (allowed: {sorted(allowed) or 'none'})"
        record_denial("tool", agent, tool_name, reason)
        raise ToolPermissionError(
            f"agent {agent!r} may not call tool {tool_name!r} (allowed: {sorted(allowed) or 'none'})"
        )


def domain_of(url: str) -> str:
    """Return the lowercase hostname of a URL, without port or credentials."""
    return (urlparse(url).hostname or "").lower()


def is_domain_allowed(url: str) -> bool:
    """Whether `url`'s host is the allowlist or a subdomain of an entry."""
    host = domain_of(url)
    if not host:
        return False
    return any(host == entry or host.endswith("." + entry) for entry in DOMAIN_ALLOWLIST)


def check_domain(url: str, agent: str = "external") -> None:
    """Authorize an outbound fetch. Raises on denial."""
    if not is_domain_allowed(url):
        record_denial("domain", agent, domain_of(url) or url, "not in domain allowlist")
        raise ToolPermissionError(
            f"domain not in allowlist: {domain_of(url) or url!r}"
        )


def filter_allowed_urls(urls: list[str], agent: str = "external") -> list[str]:
    """Drop disallowed URLs, recording each one rather than failing the batch."""
    kept = []
    for url in urls:
        if is_domain_allowed(url):
            kept.append(url)
        else:
            record_denial("domain", agent, domain_of(url) or url, "not in domain allowlist")
    return kept
