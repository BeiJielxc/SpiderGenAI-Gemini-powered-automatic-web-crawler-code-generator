"""Prompt loader utility.

Design goals:
- Single source of truth for prompt text files (Markdown under this package).
- Zero template engine dependency: rely on :meth:`str.format` for placeholder
  substitution. Any literal ``{`` / ``}`` in a file that uses placeholders
  must be escaped as ``{{`` / ``}}``.
- Cache file reads via :func:`functools.lru_cache` for performance; expose a
  :func:`reload` helper for hot-reloading while iterating on prompts.
- Files without any placeholder variables skip ``format()`` entirely, so code
  blocks that naturally contain ``{`` characters (JSON, dict literals, etc.)
  do not need any escaping.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PROMPTS_ROOT = Path(__file__).parent

# When truthy (env var PYGEN_DEBUG_DUMP_PROMPT=1), each ``load()`` call will
# write the fully-rendered prompt to ``PROMPTS_ROOT/_debug_dump/`` for diffing
# against the legacy inline strings. The filename mirrors the relative path
# with ``/`` replaced by ``__`` so files stay flat and cross-platform.
DEBUG_DUMP_DIR = PROMPTS_ROOT / "_debug_dump"


def _debug_dump_enabled() -> bool:
    return os.environ.get("PYGEN_DEBUG_DUMP_PROMPT", "").strip() not in ("", "0", "false", "False")


def _maybe_dump(rel_path: str, rendered: str) -> None:
    if not _debug_dump_enabled():
        return
    try:
        DEBUG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = rel_path.replace("/", "__").replace("\\", "__")
        (DEBUG_DUMP_DIR / safe_name).write_text(rendered, encoding="utf-8")
    except Exception:
        # Debug dump is best-effort; never break the caller.
        pass


@lru_cache(maxsize=256)
def _read(rel_path: str) -> str:
    """Read a prompt file from disk with caching."""
    path = PROMPTS_ROOT / rel_path
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def load(rel_path: str, **variables: Any) -> str:
    """Load a prompt template by path relative to :data:`PROMPTS_ROOT`.

    Parameters
    ----------
    rel_path: str
        Relative path such as ``"planner/system.md"``.
    **variables:
        Optional placeholder values. If provided, ``str.format`` is applied
        to the template text. When no variables are given, the file is
        returned as-is (useful for templates containing raw ``{`` chars).
    """
    text = _read(rel_path)
    rendered = text if not variables else text.format(**variables)
    _maybe_dump(rel_path, rendered)
    return rendered


def render(text: str, **variables: Any) -> str:
    """Format an arbitrary string with the same semantics as :func:`load`."""
    if not variables:
        return text
    return text.format(**variables)


def reload() -> None:
    """Clear the in-memory template cache.

    Call this during development after editing a prompt file so subsequent
    :func:`load` calls pick up fresh content without restarting the process.
    """
    _read.cache_clear()
