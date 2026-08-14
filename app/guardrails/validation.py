"""Output schema validation for the synthesizer, with one repair pass.

The synthesizer is asked for a fixed JSON shape. When it deviates — a missing
field, a string where a list belongs, a confidence outside [0, 1] — the answer
is not shipped on the assumption that downstream code will cope. It is either
repaired in one bounded round-trip or routed to abstention.

One repair attempt, never a loop: if a model cannot produce its own declared
schema on the second try, more attempts spend quota without changing the
outcome.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

_EID_RE = re.compile(r"^E\d+$")


class KeyFactorModel(BaseModel):
    """One supporting factor behind the recommendation."""

    model_config = ConfigDict(extra="ignore")

    factor: str = ""
    detail: str = ""
    citations: list[str] = Field(default_factory=list)


class ConflictModel(BaseModel):
    """A disagreement the synthesizer chose to surface.

    `between` is validated for real (E-ids that actually exist) by
    `conflict.validate_conflicts` right after this schema check -- this model
    only needs to get the shape right, not the content.
    """

    model_config = ConfigDict(extra="ignore")

    between: list[str] = Field(default_factory=list)
    on: str = ""
    description: str = ""
    authority_note: str = ""


class SynthesisOutput(BaseModel):
    """The contract the synthesizer must satisfy before an answer ships."""

    model_config = ConfigDict(extra="ignore")

    recommendation: str = Field(min_length=1)
    key_factors: list[KeyFactorModel] = Field(default_factory=list)
    conflicts: list[ConflictModel] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)

    @field_validator("citations")
    @classmethod
    def _citations_are_eids(cls, value: list[str]) -> list[str]:
        """Reject anything that is not an E-id.

        A citation list containing prose is a sign the model drifted out of the
        schema, and it would silently break citation checking downstream.
        """
        bad = [c for c in value if not _EID_RE.match(str(c).strip().upper())]
        if bad:
            raise ValueError(f"citations must be E-ids, got: {bad[:5]}")
        return [str(c).strip().upper() for c in value]


@dataclass
class ValidationResult:
    """Outcome of validating one synthesizer payload."""

    ok: bool
    data: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    repaired: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for traces."""
        return {"ok": self.ok, "errors": self.errors, "repaired": self.repaired}


def _format_errors(exc: ValidationError) -> list[str]:
    """Render pydantic errors compactly enough to paste into a repair prompt."""
    return [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()
    ]


def validate_output(payload: dict[str, Any]) -> ValidationResult:
    """Check a synthesizer response against `SynthesisOutput`."""
    try:
        model = SynthesisOutput.model_validate(payload)
    except ValidationError as exc:
        errors = _format_errors(exc)
        logger.warning("synthesis output failed schema validation: %s", errors)
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(ok=True, data=model.model_dump())


REPAIR_PROMPT = """You returned a JSON object that failed schema validation.
Fix it. Do not add, remove, or reword any factual content — only correct the
structure so it validates.

Schema errors:
{errors}

Required shape:
{
  "recommendation": non-empty string,
  "key_factors": array of {"factor": string, "detail": string, "citations": [E-ids]},
  "conflicts": array of {"between": [E-ids], "on": string, "description": string, "authority_note": string},
  "confidence": float between 0.0 and 1.0,
  "limitations": array of strings,
  "citations": array of E-ids like "E1", "E2"
}

Your invalid output:
{payload}

Return ONLY the corrected JSON object. No prose, no markdown fences."""


def repair_output(
    payload: dict[str, Any],
    errors: list[str],
    complete: Callable[[str], dict[str, Any]],
) -> ValidationResult:
    """Attempt exactly one schema repair round-trip.

    Args:
        payload: the invalid response.
        errors: rendered validation errors, shown to the model.
        complete: callable taking a prompt and returning parsed JSON. Injected
            so this module stays independent of the provider chain (and so the
            caller chooses which model pays for the repair).

    Returns:
        A `ValidationResult`; `ok` is False when the repair also failed, which
        is the caller's signal to abstain.
    """
    import json

    prompt = (
        REPAIR_PROMPT.replace("{errors}", "\n".join(f"- {e}" for e in errors))
        .replace("{payload}", json.dumps(payload, ensure_ascii=False)[:4000])
    )

    try:
        repaired = complete(prompt)
    except Exception as exc:  # noqa: BLE001 - any provider failure means abstain
        logger.error("schema repair call failed: %s", exc)
        return ValidationResult(ok=False, errors=[*errors, f"repair call failed: {exc}"])

    result = validate_output(repaired)
    if result.ok:
        logger.info("schema repair succeeded")
        return ValidationResult(ok=True, data=result.data, repaired=True)

    logger.warning("schema repair failed; routing to abstain: %s", result.errors)
    return ValidationResult(ok=False, errors=[*errors, *result.errors], repaired=True)
