"""Agent abstraction.

`AgentCard` deliberately mirrors the A2A (Agent-to-Agent) card shape so agents
can later be exposed over the wire without reshaping their metadata.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentCard:
    """Discovery metadata for an agent (A2A-compatible shape)."""

    name: str
    description: str
    version: str = "0.1.0"
    skills: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    # TODO: url, provider, authentication, defaultInputModes/OutputModes


class Agent(ABC):
    """Base class for all research agents."""

    card: AgentCard

    @abstractmethod
    async def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Execute a sub-task and return evidence.

        Args:
            task: sub-question plus any filters from the manager node.
            context: tenant context, budgets, and trace handles.

        Returns:
            A dict containing at least an `evidence` list.
        """
        raise NotImplementedError

    def describe(self) -> AgentCard:
        """Return this agent's discovery card."""
        return self.card
