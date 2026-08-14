"""A2A client wrapper used by the manager node.

Speaks the a2a-sdk REST binding over HTTP to a separate process. Discovery goes
through the Agent Card at the well-known path, so the manager learns the peer's
skills and endpoint rather than having them hardcoded.

Every call is logged with its request and response state, which is what makes
the cross-process hop visible in the trace.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

AGENT_CARD_PATH = "/.well-known/agent-card.json"
MESSAGE_SEND_PATH = "/message:send"
VERSION_HEADER = "A2A-Version"
PROTOCOL_VERSION = "1.0"

TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}


class A2AClientError(Exception):
    """Raised when the remote agent cannot be reached or fails a task."""


@dataclass
class A2ACallRecord:
    """Observability record of one cross-process agent call."""

    url: str
    task_id: str = ""
    state: str = ""
    subquestion: str = ""
    evidence_count: int = 0
    latency_ms: float = 0.0
    error: str = ""
    agent_card: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": "a2a/http",
            "url": self.url,
            "task_id": self.task_id,
            "state": self.state,
            "subquestion": self.subquestion,
            "evidence_count": self.evidence_count,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "agent_card": self.agent_card,
        }


class A2AExternalClient:
    """Client for the external research agent."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.external_agent_url).rstrip("/")
        self.timeout = timeout or settings.per_agent_timeout_s
        self._card: dict[str, Any] | None = None

    def get_agent_card(self, refresh: bool = False) -> dict[str, Any]:
        """Fetch and cache the peer's Agent Card."""
        if self._card is not None and not refresh:
            return self._card

        url = f"{self.base_url}{AGENT_CARD_PATH}"
        logger.info("A2A discovery GET %s", url)
        try:
            response = httpx.get(url, timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise A2AClientError(f"agent card fetch failed: {exc}") from exc

        self._card = response.json()
        logger.info(
            "A2A agent card: name=%s version=%s skills=%s",
            self._card.get("name"),
            self._card.get("version"),
            [s.get("id") for s in self._card.get("skills", [])],
        )
        return self._card

    def is_reachable(self) -> bool:
        """Whether the peer answers its health probe."""
        try:
            return httpx.get(f"{self.base_url}/health", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False

    def research(self, subquestion: str) -> tuple[list[dict[str, Any]], A2ACallRecord]:
        """Send a sub-question as an A2A task; return evidence and a call record.

        Raises:
            A2AClientError: on transport failure or a non-completed task state.
        """
        import time

        record = A2ACallRecord(url=f"{self.base_url}{MESSAGE_SEND_PATH}", subquestion=subquestion)

        try:
            record.agent_card = {
                k: v
                for k, v in self.get_agent_card().items()
                if k in {"name", "version", "skills", "supportedInterfaces"}
            }
        except A2AClientError as exc:
            record.error = str(exc)
            raise

        body = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": subquestion}],
            }
        }

        started = time.perf_counter()
        logger.info("A2A POST %s :: %r", record.url, subquestion)
        try:
            response = httpx.post(
                record.url,
                json=body,
                headers={VERSION_HEADER: PROTOCOL_VERSION, "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            record.error = str(exc)
            record.latency_ms = (time.perf_counter() - started) * 1000
            raise A2AClientError(f"A2A call failed: {exc}") from exc

        record.latency_ms = (time.perf_counter() - started) * 1000
        payload = response.json()

        if "error" in payload:
            record.error = json.dumps(payload["error"])[:300]
            raise A2AClientError(f"remote agent error: {record.error}")

        task = payload.get("task", payload)
        record.task_id = task.get("id", "")
        record.state = (task.get("status") or {}).get("state", "")

        if record.state != "TASK_STATE_COMPLETED":
            record.error = f"task ended in state {record.state}"
            raise A2AClientError(record.error)

        evidence = self._extract_evidence(task)
        record.evidence_count = len(evidence)
        logger.info(
            "A2A task %s -> %s (%d evidence, %.0f ms)",
            record.task_id,
            record.state,
            record.evidence_count,
            record.latency_ms,
        )
        return evidence, record

    @staticmethod
    def _extract_evidence(task: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the evidence payload out of the task's artifacts."""
        for artifact in task.get("artifacts") or []:
            for part in artifact.get("parts") or []:
                text = part.get("text")
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and "evidence" in payload:
                    return payload["evidence"]
        return []
