"""Persistent memory layer for the multi-agent system.

Two complementary stores live under ``pygen/output/memory/``:

* ``episode/episodes.jsonl`` — append-only log of *committed* tasks (one
  JSON per line). Each row records the facts of a single run (URL,
  duration, iterations, verified selectors, html_fingerprint) plus the
  *human* verdict (``user_verdict`` ∈ {"correct", "wrong"}) and any
  LLM-distilled ``lessons``. Rows enter this file only after a user
  submits feedback.

* ``episode/pending/<task_id>.json`` — *draft* episodes written by the
  ``summarize_node`` at task end. They wait here until the user evaluates
  the run in the frontend feedback Modal. After ``pending_gc_days`` of
  inactivity the garbage collector removes them so site profiles never
  get polluted by un-validated runs.

* ``site/<domain>.json|.md`` — per-site *aggregate* profile. Updated only
  from committed (verdict-bearing) episodes. The ``confidence`` field
  decays over time and is penalized on user-marked failures; HTML
  fingerprints are tracked to detect structural drift; selectors must
  win ``promote_min_wins`` times before joining ``stable_selectors``.
  Quarantined profiles get sealed under ``site/_quarantine/`` for audit.

Public surface:

* :func:`MemoryStore` — facade over all I/O. Cheap to instantiate,
  thread-safe at the file level.
* :class:`Episode` — TypedDict-style dataclass for a single run.
* :class:`SiteProfile` — TypedDict-style dataclass for a per-site aggregate.
* :func:`compute_list_page_fingerprint` — list-page HTML hashing.
* :func:`render_site_memory_hint` / :func:`render_feedback_replay_hint` —
  prompt-side rendering helpers used by the planner read path.
* :func:`run_auto_findings` — heuristic "model self-check report" used
  by the summarize node and shown to the user in the feedback Modal.
* :func:`commit_episode` — Stage 2 entry point: takes user verdict +
  suggestion, runs LLM enrichment, moves the draft into the committed log
  and updates the site profile.

All helpers are designed to fail soft: any I/O exception is logged and
swallowed so memory bookkeeping cannot take down the main agent flow.
"""

from __future__ import annotations

from .auto_findings import run_auto_findings
from .commit import commit_episode
from .episode import Episode, extract_facts_from_state, new_draft_episode
from .fingerprint import compute_list_page_fingerprint
from .render import (
    find_recent_task_id_for_domain,
    render_feedback_replay_hint,
    render_site_memory_hint,
    should_inject_profile,
    walk_rerun_chain,
)
from .site_profile import SiteProfile, update_profile_from_episode
from .store import MemoryStore

__all__ = [
    "Episode",
    "MemoryStore",
    "SiteProfile",
    "commit_episode",
    "compute_list_page_fingerprint",
    "extract_facts_from_state",
    "find_recent_task_id_for_domain",
    "new_draft_episode",
    "render_feedback_replay_hint",
    "render_site_memory_hint",
    "run_auto_findings",
    "should_inject_profile",
    "update_profile_from_episode",
    "walk_rerun_chain",
]
