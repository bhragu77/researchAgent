"""PII detection and redaction on the outbound answer.

Deliberately narrow. Regex over structured identifiers only — emails, phone
numbers, national IDs, card numbers. No NER: name detection at this scale
produces more false positives than it prevents leaks, and a redacted
"Accounts Payable" helps nobody.

The exemption that matters: a value the tenant's own internal document already
contains is not leaked by repeating it back to that tenant. Only PII that
entered through an external source is redacted, which is checked per value
rather than per answer — one external document does not force redaction of the
tenant's own contact details.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Agents whose evidence is the tenant's own material.
INTERNAL_AGENTS = {"enterprise", "analytics"}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    # Requires an international prefix or a trunk zero, so currency figures and
    # years cannot match.
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s-]?)?(?:\(0\)|0)?\d{3,5}[\s-]\d{3}[\s-]?\d{3,4}\b")),
    ("uk_nino", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b")),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("india_pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("india_aadhaar", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]


@dataclass
class PIISpan:
    """One detected identifier."""

    kind: str
    value: str
    start: int
    end: int


def detect(text: str) -> list[PIISpan]:
    """Find PII spans in a string."""
    spans: list[PIISpan] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text or ""):
            spans.append(
                PIISpan(kind=kind, value=match.group(0), start=match.start(), end=match.end())
            )
    return spans


def redact(text: str, allow: set[str] | None = None) -> tuple[str, list[str]]:
    """Replace detected PII with typed placeholders.

    Args:
        text: the string to scrub.
        allow: values exempt from redaction — those already present in the
            tenant's own internal documents.

    Returns:
        (redacted text, kinds that were redacted).
    """
    allow = allow or set()
    redacted_kinds: list[str] = []
    result = text or ""

    for span in sorted(detect(result), key=lambda s: -len(s.value)):
        if span.value in allow:
            continue
        if span.value in result:
            result = result.replace(span.value, f"[REDACTED_{span.kind.upper()}]")
            redacted_kinds.append(span.kind)

    return result, redacted_kinds


def internal_values(evidence: list[dict[str, Any]]) -> set[str]:
    """Collect PII values that the tenant's own documents already contain."""
    allowed: set[str] = set()
    for item in evidence:
        agent = (item.get("metadata") or {}).get("agent", "enterprise")
        if agent not in INTERNAL_AGENTS:
            continue
        for span in detect(item.get("text", "")):
            allowed.add(span.value)
    return allowed


def redact_answer(
    answer: dict[str, Any], evidence: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Scrub PII from every free-text field of a final answer.

    Returns:
        (answer, report). The report is attached to the trace so a redaction is
        visible rather than silent.
    """
    allow = internal_values(evidence)
    kinds: list[str] = []

    def scrub(value: str) -> str:
        cleaned, found = redact(value, allow=allow)
        kinds.extend(found)
        return cleaned

    answer["recommendation"] = scrub(answer.get("recommendation", ""))
    answer["limitations"] = [scrub(str(item)) for item in answer.get("limitations", [])]

    for section in ("key_factors", "conflicts"):
        for entry in answer.get(section) or []:
            if not isinstance(entry, dict):
                continue
            for key in ("factor", "detail", "on", "description", "authority_note"):
                if key in entry:
                    entry[key] = scrub(str(entry[key]))

    report = {"redacted": bool(kinds), "kinds": sorted(set(kinds)), "exempt_values": len(allow)}
    if kinds:
        logger.warning("redacted %d PII span(s) from final answer: %s", len(kinds), report["kinds"])

    return answer, report
