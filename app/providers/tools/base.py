"""Tool interface.

Tools are the only way agents touch the outside world. Each declares a JSON
schema so it can be exposed to the model and validated before execution.
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """Base class for an executable tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = {}

    @abstractmethod
    async def run(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        """Execute the tool with validated arguments."""
        raise NotImplementedError

    def to_spec(self) -> dict[str, Any]:
        """Render this tool as a model-facing function spec."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
