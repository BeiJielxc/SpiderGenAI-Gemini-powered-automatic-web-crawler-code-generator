"""Tests for ``pygen.agents.summarize_node`` — the Stage-1 Summary Agent.

The node is *zero LLM*: it should always return auto_findings, write a
draft episode (when a MemoryStore is supplied), expose the path back to
the state, never raise on bad inputs, and respect the kill-switch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

PYGEN_DIR = Path(__file__).resolve().parent.parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from agents.summarize_node import (  # noqa: E402
    MEMORY_STORE_CONFIG_KEY,
    summarize_node,
)
from agents.codegen_graph import PYGEN_CONFIG_KEY  # noqa: E402
from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY  # noqa: E402
from agents.critic_graph import LOG_CALLBACK_CONFIG_KEY  # noqa: E402
from memory import MemoryStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _Ctx:
    """Minimal stand-in for ``ToolContext`` (only ``page_html`` is read)."""

    def __init__(self, page_html: str = ""):
        self.page_html = page_html


def _make_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "task_id": "tid_summary_01",
        "url": "https://www.example.com/news",
        "run_mode": "news_sentiment",
        "iterations": 5,
        "critic_rounds": 0,
        "generated_code": "print('hello')\n",
        "verified_selectors": {"list": {"container": "ul.news"}},
        "tool_calls_log": [
            {"action": "verify_selector", "success": True, "action_input": {"selector": "ul.news"}},
        ],
        "critic_verdict": {"passed": True},
        "started_at": None,
        "prev_task_id": None,
    }
    state.update(overrides)
    return state


def _config(*, store=None, ctx=None, log=None) -> Dict[str, Any]:
    configurable: Dict[str, Any] = {}
    if store is not None:
        configurable[MEMORY_STORE_CONFIG_KEY] = store
    if ctx is not None:
        configurable[TOOL_CONTEXT_CONFIG_KEY] = ctx
    if log is not None:
        configurable[LOG_CALLBACK_CONFIG_KEY] = log
    return {"configurable": configurable}


# ---------------------------------------------------------------------------
# Behavior: with MemoryStore (the happy path)
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", max_episodes=10)


def test_summarize_node_writes_draft_when_store_provided(store):
    state = _make_state()
    ctx = _Ctx(page_html="<html><body><ul><li><a>x</a></li></ul></body></html>")
    out = summarize_node(state, _config(store=store, ctx=ctx))

    assert "auto_findings" in out
    assert "summary_draft_path" in out and out["summary_draft_path"]
    assert "html_fingerprint" in out and out["html_fingerprint"].startswith("sha256:")

    # Draft is on disk and round-trips
    draft = store.read_draft(state["task_id"])
    assert draft is not None
    assert draft["task_id"] == state["task_id"]
    assert draft["domain"] == "example.com"
    assert draft["committed"] is False
    assert draft["html_fingerprint"] == out["html_fingerprint"]
    assert isinstance(draft["auto_findings"], dict)


def test_summarize_node_classifies_auto_outcome_success(store):
    state = _make_state(critic_verdict={"passed": True}, generated_code="x = 1")
    out = summarize_node(state, _config(store=store, ctx=_Ctx()))
    assert out["summary_draft_path"]
    draft = store.read_draft(state["task_id"])
    assert draft["auto_outcome"] == "success"


def test_summarize_node_classifies_auto_outcome_partial_when_suspected(store):
    """Critic says passed but auto_findings flags a suspected failure."""
    code_with_smell = """
    items = page.locator('div.item').all()
    for item in items:
        link = item.locator('a').first.get_attribute('href')
        url = urljoin(base, link or '')
    """
    state = _make_state(
        generated_code=code_with_smell,
        critic_verdict={"passed": True},
        tool_calls_log=[],
    )
    out = summarize_node(state, _config(store=store, ctx=_Ctx()))
    draft = store.read_draft(state["task_id"])
    assert draft["auto_outcome"] == "partial"
    assert draft["auto_findings"]["suspected_failures"], (
        "expected suspected_failures to fire on Sec-Zambia smell"
    )


def test_summarize_node_classifies_failure_when_no_code(store):
    state = _make_state(generated_code="", critic_verdict={})
    summarize_node(state, _config(store=store, ctx=_Ctx()))
    draft = store.read_draft(state["task_id"])
    assert draft["auto_outcome"] == "failure"


def test_summarize_node_records_rerun_of(store):
    state = _make_state(prev_task_id="prev_xyz")
    summarize_node(state, _config(store=store, ctx=_Ctx()))
    draft = store.read_draft(state["task_id"])
    assert draft["rerun_of"] == "prev_xyz"


# ---------------------------------------------------------------------------
# Behavior: without a store (memory disabled / not wired)
# ---------------------------------------------------------------------------


def test_summarize_node_returns_findings_without_store():
    """No store → no draft, but auto_findings should still surface."""
    state = _make_state()
    out = summarize_node(state, _config(store=None, ctx=_Ctx()))
    assert "auto_findings" in out
    assert "summary_draft_path" not in out


# ---------------------------------------------------------------------------
# Behavior: kill switch
# ---------------------------------------------------------------------------


class _DisabledConfig:
    memory_enabled = False


def test_summarize_node_respects_memory_disabled_flag(store):
    state = _make_state()
    cfg = _config(store=store, ctx=_Ctx())
    cfg["configurable"][PYGEN_CONFIG_KEY] = _DisabledConfig()
    out = summarize_node(state, cfg)
    assert out == {}
    # No draft should have been written
    assert store.read_draft(state["task_id"]) is None


# ---------------------------------------------------------------------------
# Behavior: hard inputs / robustness
# ---------------------------------------------------------------------------


def test_summarize_node_never_raises_on_garbage_state(store):
    """Defense in depth: bad state shouldn't take down the run."""
    out = summarize_node({}, _config(store=store, ctx=_Ctx()))
    assert isinstance(out, dict)
    # auto_findings should be present (zero-token scan handles empty input)
    assert "auto_findings" in out


def test_summarize_node_handles_no_config():
    """Calling with config=None should silently return an empty dict (no crash)."""
    out = summarize_node(_make_state(), {"configurable": {}})
    # No store/ctx wired → still safe, still returns something
    assert isinstance(out, dict)
