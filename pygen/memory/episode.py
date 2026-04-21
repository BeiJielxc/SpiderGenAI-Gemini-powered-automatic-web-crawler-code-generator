"""Episode dataclass + rule-based fact extractor.

An *Episode* is the unit of persistent memory: one row per task run.
The schema intentionally separates four lifecycle stages so we can
track exactly where a given record is in the feedback pipeline:

1. **Facts** (``task_id``, ``url``, ``iterations``, ``verified_selectors``,
   ``html_fingerprint``, ...) — written by ``summarize_node`` at task end
   from the final ``AgentState`` + ``ToolContext``. Zero LLM calls.

2. **auto_findings** — also written at task end by zero-token heuristic
   scans (see :mod:`pygen.memory.auto_findings`). Surfaces redundancies
   and suspicious failures so the user can react in the Modal.

3. **User feedback** (``user_verdict``, ``user_suggestion``) — written
   when the user submits the feedback Modal. ``user_verdict`` ∈
   ``{"correct", "wrong"}``; ``user_suggestion`` may be empty when
   ``verdict == "correct"``.

4. **lessons** — written by the LLM enrichment step at commit time
   (see :mod:`pygen.memory.commit`). Three sub-blocks
   (``failure_analysis``, ``optimization``, ``site_traits``) — the first
   is ``None`` when ``verdict == "correct"``.

Episodes are stored either in ``episode/pending/<task_id>.json``
(``committed=False``) or appended to ``episode/episodes.jsonl``
(``committed=True``). The schema is identical so the same loader can
read either file.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

EPISODE_SCHEMA_VERSION = 1
"""Bump this on any breaking change to the on-disk JSON shape."""

VALID_VERDICTS = ("correct", "wrong")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Episode(dict):
    """Plain-dict container; we use ``dict`` rather than ``TypedDict`` so
    the JSON round-trip is trivial and forward-compatible (extra keys
    survive without losing anything)."""

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "Episode":
        if not isinstance(raw, dict):
            raise TypeError(f"Episode payload must be dict, got {type(raw).__name__}")
        return cls(raw)

    @property
    def task_id(self) -> str:
        return str(self.get("task_id", ""))

    @property
    def domain(self) -> str:
        return str(self.get("domain", ""))

    @property
    def committed(self) -> bool:
        return bool(self.get("committed", False))

    @property
    def user_verdict(self) -> Optional[str]:
        v = self.get("user_verdict")
        if v in VALID_VERDICTS:
            return str(v)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def domain_of(url: str) -> str:
    """Return a sanitized domain (lowercase, no www., no port) suitable
    for use as a filesystem-safe key. Returns ``""`` if parsing fails."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# Tool-call log entries have shape: {action, action_input, success,
# summary, error_code, suggested_next_tools}. We compute counts and
# success rates per tool name without assuming exact keys.
def _tool_call_stats(tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    failures: Dict[str, int] = {}
    for entry in tool_calls or []:
        name = _coerce_str(entry.get("action") or entry.get("name") or "unknown")
        counts[name] = counts.get(name, 0) + 1
        if entry.get("success") is False:
            failures[name] = failures.get(name, 0) + 1
    out = {"total_calls": sum(counts.values()), "by_tool": counts}
    if failures:
        out["failures_by_tool"] = failures
    return out


def _count_code_lines(code: Optional[str]) -> int:
    if not code or not isinstance(code, str):
        return 0
    return len([ln for ln in code.splitlines() if ln.strip()])


# ---------------------------------------------------------------------------
# Draft context capture (Stage 2 LLM uses these for first-person retrospective)
# ---------------------------------------------------------------------------

# Caps to keep on-disk drafts manageable. Generous enough that a real run's
# code + tool log fits, but short of the absurd. The commit-time prompt
# builder may further truncate.
MAX_TOOL_CALLS_KEEP = 80
MAX_TOOL_INPUT_CHARS = 400
MAX_TOOL_SUMMARY_CHARS = 600
MAX_GENERATED_CODE_CHARS = 30000

_LARGE_INPUT_KEYS = {"html", "page_html", "raw_html", "screenshot", "screenshot_b64",
                     "image", "image_base64", "content", "body"}


def _shrink_action_input(action_input: Any) -> Any:
    """Drop / truncate large blobs from a tool's action_input.

    Keeps small scalar args verbatim (URLs, selectors, mode flags) — those
    are exactly what the retrospective LLM needs to see — but replaces
    HTML / screenshot blobs with a length tag so the draft stays small.
    """
    if not isinstance(action_input, dict):
        s = _coerce_str(action_input)
        return s if len(s) <= MAX_TOOL_INPUT_CHARS else s[:MAX_TOOL_INPUT_CHARS] + "…"
    out: Dict[str, Any] = {}
    for k, v in action_input.items():
        key = _coerce_str(k)
        if key.lower() in _LARGE_INPUT_KEYS:
            try:
                n = len(v) if hasattr(v, "__len__") else len(str(v))
            except Exception:
                n = -1
            out[key] = f"<{key} elided, len={n}>"
            continue
        if isinstance(v, str) and len(v) > MAX_TOOL_INPUT_CHARS:
            out[key] = v[:MAX_TOOL_INPUT_CHARS] + "…"
        elif isinstance(v, (list, tuple)) and len(v) > 12:
            out[key] = list(v[:12]) + [f"…+{len(v) - 12} more"]
        elif isinstance(v, dict):
            # one-level recursive shrink
            out[key] = _shrink_action_input(v)
        else:
            out[key] = v
    return out


def _build_tool_calls_excerpt(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim ``tool_calls_log`` for inclusion in the draft.

    Keeps the **last ``MAX_TOOL_CALLS_KEEP`` calls** in chronological order
    (the most recent are the most relevant to a retrospective), preserves
    action / success / error_code / a truncated summary, and elides large
    input blobs via :func:`_shrink_action_input`.
    """
    if not tool_calls:
        return []
    tail = list(tool_calls)[-MAX_TOOL_CALLS_KEEP:]
    out: List[Dict[str, Any]] = []
    for idx, entry in enumerate(tail):
        if not isinstance(entry, dict):
            continue
        summary = _coerce_str(entry.get("summary"))
        if len(summary) > MAX_TOOL_SUMMARY_CHARS:
            summary = summary[:MAX_TOOL_SUMMARY_CHARS] + "…"
        out.append({
            "i": idx,
            "action": _coerce_str(entry.get("action") or entry.get("name") or "unknown"),
            "input": _shrink_action_input(entry.get("action_input")),
            "success": bool(entry.get("success", True)),
            "error_code": entry.get("error_code"),
            "summary": summary,
        })
    return out


def _capture_initial_prompt(state: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot the initial human-readable task prompt from state.

    The planner builds the user message dynamically in
    ``planner_graph._build_initial_user_message`` — to keep this layer
    independent we just round-trip the inputs that go *into* it. The
    LLM at commit time can then reconstruct the same context as it had
    when the task started.
    """
    extra_req = _coerce_str(state.get("extra_requirements"))
    feedback_hint = _coerce_str(state.get("feedback_replay_hint"))
    site_hint = _coerce_str(state.get("site_memory_hint"))
    return {
        "url": _coerce_str(state.get("url")),
        "run_mode": _coerce_str(state.get("run_mode")),
        "start_date": _coerce_str(state.get("start_date")),
        "end_date": _coerce_str(state.get("end_date")),
        "extra_requirements": extra_req[:2000],
        "had_site_memory_hint": bool(site_hint),
        "had_feedback_replay_hint": bool(feedback_hint),
        # Keep the actual hint texts (capped) so the retrospective LLM
        # can see what guidance the planner started with.
        "site_memory_hint": site_hint[:3000] if site_hint else "",
        "feedback_replay_hint": feedback_hint[:3000] if feedback_hint else "",
    }


def _capture_generated_code(code: Optional[str]) -> Optional[str]:
    if not code or not isinstance(code, str):
        return None
    if len(code) <= MAX_GENERATED_CODE_CHARS:
        return code
    half = MAX_GENERATED_CODE_CHARS // 2
    return code[:half] + "\n\n# … (middle elided to fit draft) …\n\n" + code[-half:]


# ---------------------------------------------------------------------------
# Fact extraction (Stage 1, zero LLM)
# ---------------------------------------------------------------------------


def extract_facts_from_state(
    state: Dict[str, Any],
    *,
    ctx: Optional[Any] = None,
    duration_sec: Optional[float] = None,
    started_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the *facts* block of an Episode from the final AgentState.

    Pure function: no I/O, no LLM. Defensive against missing fields.

    Parameters
    ----------
    state : Dict
        The final ``AgentState`` returned by the planner graph.
    ctx : ToolContext, optional
        Used as a fallback source for fields the state may not mirror
        (e.g. ``page_html`` for fingerprinting).
    duration_sec : float, optional
        Wall-clock duration of the run. If omitted and ``started_at`` is
        provided we compute it from ``time.time() - started_at``.
    started_at : float, optional
        Epoch seconds when the run started.
    """
    state = state or {}

    if duration_sec is None and started_at is not None:
        try:
            duration_sec = max(0.0, float(time.time()) - float(started_at))
        except Exception:
            duration_sec = None

    url = _coerce_str(state.get("url"))
    domain = domain_of(url)

    tool_calls = list(state.get("tool_calls_log") or [])
    generated_code = state.get("generated_code") or ""

    # Snapshot the model alias used for this run so the commit-time
    # retrospective can default to "the same model that did the work".
    model_alias = ""
    try:
        cfg = getattr(ctx, "config", None) if ctx is not None else None
        for k in ("active_model_name", "qwen_model"):
            v = getattr(cfg, k, None) if cfg is not None else None
            if v:
                model_alias = _coerce_str(v)
                break
    except Exception:
        model_alias = ""

    return {
        "task_id": _coerce_str(state.get("task_id")) or uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "schema_version": EPISODE_SCHEMA_VERSION,
        "domain": domain,
        "url": url,
        "run_mode": _coerce_str(state.get("run_mode")),
        "iterations": _coerce_int(state.get("iterations")),
        "critic_rounds": _coerce_int(state.get("critic_rounds")),
        "duration_sec": round(float(duration_sec), 2) if duration_sec is not None else None,
        "code_size_lines": _count_code_lines(generated_code),
        "verified_selectors": state.get("verified_selectors") or {},
        "tool_call_stats": _tool_call_stats(tool_calls),
        # html_fingerprint left to caller (needs raw HTML, often on ctx)
        "html_fingerprint": None,
        # Pre-allocated empty containers for Stage 1/2 layers
        "auto_outcome": "unknown",
        "auto_findings": {
            "redundant_tool_calls": [],
            "suspected_failures": [],
            "redundant_code_blocks": [],
        },
        # ---- user feedback (filled by /feedback endpoint) ----
        "committed": False,
        "user_verdict": None,
        "user_suggestion": "",
        "committed_at": None,
        "rerun_of": _coerce_str(state.get("prev_task_id")) or None,
        # ---- lessons (filled by commit-time LLM) ----
        "lessons": None,
        # ---- retrospective context (Stage-2 LLM uses these) ----
        # Snapshotted at task end so the committing model has the same
        # information the running model had — full code, ordered tool log,
        # the original task brief — without us having to re-invoke any
        # graph internals at commit time.
        "task_brief": _capture_initial_prompt(state),
        "tool_calls_excerpt": _build_tool_calls_excerpt(tool_calls),
        "generated_code": _capture_generated_code(generated_code),
        "code_strategy": _coerce_str(state.get("code_strategy"))[:1500] or None,
        "model_alias": model_alias,
    }


def new_draft_episode(
    state: Dict[str, Any],
    *,
    ctx: Optional[Any] = None,
    duration_sec: Optional[float] = None,
    started_at: Optional[float] = None,
    auto_outcome: str = "unknown",
) -> Episode:
    """Create a new draft Episode (committed=False, user_verdict=None).

    Convenience over :func:`extract_facts_from_state` that wraps the result
    in the :class:`Episode` container and validates ``auto_outcome``.
    """
    facts = extract_facts_from_state(
        state, ctx=ctx, duration_sec=duration_sec, started_at=started_at,
    )
    if auto_outcome not in ("success", "partial", "failure", "unknown"):
        auto_outcome = "unknown"
    facts["auto_outcome"] = auto_outcome
    return Episode(facts)


# ---------------------------------------------------------------------------
# Validation (used by API layer before commit)
# ---------------------------------------------------------------------------


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def is_valid_task_id(task_id: str) -> bool:
    """Reject path-traversal / weird IDs before they touch the filesystem."""
    if not isinstance(task_id, str):
        return False
    return bool(_TASK_ID_RE.match(task_id))


__all__ = [
    "EPISODE_SCHEMA_VERSION",
    "VALID_VERDICTS",
    "MAX_GENERATED_CODE_CHARS",
    "MAX_TOOL_CALLS_KEEP",
    "MAX_TOOL_INPUT_CHARS",
    "MAX_TOOL_SUMMARY_CHARS",
    "Episode",
    "domain_of",
    "extract_facts_from_state",
    "is_valid_task_id",
    "new_draft_episode",
]
