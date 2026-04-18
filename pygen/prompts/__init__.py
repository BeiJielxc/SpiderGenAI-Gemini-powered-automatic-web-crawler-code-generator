"""Prompt templates package.

Central registry of all LLM prompts used across the pygen project.
All long-form system/user prompts live as Markdown files under this package
and are loaded through :func:`load` in :mod:`pygen.prompts.loader`.
"""

from .loader import load, reload, render, PROMPTS_ROOT

__all__ = ["load", "reload", "render", "PROMPTS_ROOT"]
