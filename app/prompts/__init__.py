"""Prompt loading.

Prompts live as plain `.txt` files next to this module so they can be edited and
diffed without touching Python. Each load returns the template together with a
version tag derived from a hash of the file contents — that tag is recorded on
every response, so a quality regression can be traced to an exact prompt
revision without needing a manual version bump.

Templates are cached in memory after first read.
"""

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

_PROMPT_DIR = Path(__file__).parent


class Prompt(NamedTuple):
    """A prompt template plus its content-derived version tag."""

    name: str
    template: str
    version: str

    def render(self, **kwargs: object) -> str:
        """Substitute `{placeholder}` fields in the template.

        Deliberately not `str.format`: these templates contain literal JSON
        braces in their output specs, which `str.format` would try to interpret
        as fields. Only the named placeholders are replaced; every other brace
        is left exactly as written, so prompts can be authored as plain text
        without escaping.
        """
        rendered = self.template
        for key, value in kwargs.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered

    def placeholders(self) -> set[str]:
        """Return the `{name}` placeholders this template declares.

        Only simple identifiers count, so JSON braces in the output spec are
        ignored.
        """
        return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", self.template))


@lru_cache(maxsize=None)
def load_prompt(name: str) -> Prompt:
    """Read a prompt template by name (without the `.txt` extension).

    The version tag is `<name>@<first 8 hex of sha256(content)>`, so editing the
    file changes the tag automatically.

    Raises:
        FileNotFoundError: if the template does not exist.
        ValueError: if the template is empty — an empty prompt would silently
            produce a garbage LLM call, so fail loudly instead.
    """
    path = _PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty: {path}")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return Prompt(name=name, template=text, version=f"{name}@{digest}")


def available() -> list[str]:
    """List the prompt names present on disk."""
    return sorted(p.stem for p in _PROMPT_DIR.glob("*.txt"))


def clear_cache() -> None:
    """Drop the in-memory cache so edited prompts are re-read."""
    load_prompt.cache_clear()
