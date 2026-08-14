"""Tool registry.

Single lookup point mapping tool names to instances, so agents never import
tool modules directly.
"""

from app.providers.tools.base import Tool

_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    """Add a tool to the registry.

    TODO: reject duplicate names; validate input_schema at registration time.
    """
    raise NotImplementedError("tool registration not implemented")


def get(name: str) -> Tool:
    """Look up a tool by name.

    TODO: raise a typed ToolNotFound error.
    """
    raise NotImplementedError("tool lookup not implemented")


def list_tools() -> list[Tool]:
    """Return all registered tools.

    TODO: filter by the caller's permissions before exposing to a model.
    """
    raise NotImplementedError("tool listing not implemented")
