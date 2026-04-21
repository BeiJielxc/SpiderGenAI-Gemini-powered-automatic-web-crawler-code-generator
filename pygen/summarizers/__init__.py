"""Rule-based artifact summarizers.

Each summarizer is a pure function (no LLM, no I/O) that converts a raw
artifact payload into a compact dict of decision signals. The dict is
stored alongside the artifact by ``ArtifactStore.put_with_summary`` so
the planner LLM sees small structured candidates in messages instead of
multi-thousand-character HTML / JSON dumps.

Design contract for a summarizer:

    summarize(content) -> dict

* Never raises. Catch everything; return ``{"_summary_error": "..."}``.
* Output keys are stable strings (the LLM will learn to look them up).
* When applicable, return a ``fallback_hints`` list of ready-made
  ``read_artifact`` invocation strings the LLM can copy verbatim.
* Lists candidates with evidence; never claim a single "answer".
"""

from __future__ import annotations

from .html import summarize_html
from .json_payload import summarize_json_payload, summarize_analyze_page
from .llm_fallback import enrich_summary_via_llm, is_weak_summary

__all__ = [
    "summarize_html",
    "summarize_json_payload",
    "summarize_analyze_page",
    "enrich_summary_via_llm",
    "is_weak_summary",
]
