"""Evidence model and score normalization.

Every retriever, regardless of source, emits `Evidence` — this is the contract
that makes fusion, reranking, and citation possible.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """A single retrieved chunk with provenance.

    `id` is the citation handle (E1, E2, ...). It is assigned late — after
    reranking picks the final set — so ids always match what the model was
    shown.
    """

    text: str
    source: str
    authority: str = "unknown"
    date: str = "unknown"
    score: float = 0.0
    id: str = ""
    chunk_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for graph state and API payloads."""
        return {
            "id": self.id,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "source": self.source,
            "authority": self.authority,
            "date": self.date,
            "score": self.score,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Evidence":
        """Rebuild from a serialized graph-state entry."""
        return cls(
            text=payload["text"],
            source=payload.get("source", "unknown"),
            authority=payload.get("authority", "unknown"),
            date=payload.get("date", "unknown"),
            score=float(payload.get("score", 0.0)),
            id=payload.get("id", ""),
            chunk_id=payload.get("chunk_id", ""),
            metadata=payload.get("metadata", {}),
        )


def normalize_scores(items: list[Evidence]) -> list[Evidence]:
    """Min-max normalize scores to [0, 1] in place, then return the list.

    Dense cosine similarity and BM25 live on incompatible scales, so raw scores
    cannot be compared or fused directly. When every score is identical (or
    there is only one item) each is set to 1.0 — with no spread there is no
    ranking information to preserve.
    """
    if not items:
        return items

    scores = [item.score for item in items]
    lo, hi = min(scores), max(scores)
    span = hi - lo

    for item in items:
        item.score = 1.0 if span == 0 else (item.score - lo) / span
    return items
