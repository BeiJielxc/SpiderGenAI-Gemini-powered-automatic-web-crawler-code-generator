"""Supervisor scaffolding + runner contract tests."""

from __future__ import annotations

import pytest


def test_register_agent_roundtrip():
    from agents.supervisor import AGENT_REGISTRY, register_agent

    def factory():
        return "hello"

    register_agent("test-worker", factory)
    assert AGENT_REGISTRY["test-worker"] is factory


def test_dummy_router_picks_planner():
    from agents.supervisor import _dummy_router

    assert _dummy_router({}) == "planner"


def test_result_translation_preserves_planner_result_shape():
    """runner._build_result_from_state must produce the same fields api.py reads."""
    from agents.result import PlannerResult
    from agents.runner import _build_result_from_state
    from tools import ToolContext

    class _B:
        pass

    ctx = ToolContext(
        browser=_B(),
        config=None,
        llm_agent=None,
        url="u",
        run_mode="m",
        start_date="s",
        end_date="e",
    )
    ctx.generated_code = "print('x')"
    ctx.code_generation_strategy = "strat"
    ctx.enhanced_analysis = {"list_extract": {"count": 3}}
    ctx.verified_mapping = {"menu_to_urls": {"a": "b"}}

    state = {
        "tool_calls_log": [{"action": "open_page"}, {"action": "generate_crawler_code"}],
        "iterations": 2,
        "generated_code": "print('x')",
        "code_strategy": "strat",
        "enhanced_analysis": {"list_extract": {"count": 3}},
        "verified_mapping": {"menu_to_urls": {"a": "b"}},
        "critic_verdict": {"passed": True},
        "messages": [],
    }

    result = _build_result_from_state(state, ctx, error=None)
    assert isinstance(result, PlannerResult)
    assert result.success is True
    assert result.script_code == "print('x')"
    assert result.iterations == 2
    assert len(result.tool_calls) == 2
    assert result.enhanced_analysis == {"list_extract": {"count": 3}}
    assert result.verified_mapping == {"menu_to_urls": {"a": "b"}}
    assert result.error is None


def test_result_translation_marks_failure_on_empty_code():
    from agents.runner import _build_result_from_state
    from tools import ToolContext

    class _B:
        pass

    ctx = ToolContext(
        browser=_B(), config=None, llm_agent=None,
        url="u", run_mode="m", start_date="s", end_date="e",
    )
    state = {
        "tool_calls_log": [{"action": "open_page"}],
        "iterations": 1,
        "generated_code": "",
        "critic_verdict": None,
        "messages": [],
    }
    result = _build_result_from_state(state, ctx, error=None)
    assert result.success is False
    assert "did not produce" in (result.error or "")


def test_result_translation_marks_failure_when_critic_rejects():
    from agents.runner import _build_result_from_state
    from tools import ToolContext

    class _B:
        pass

    ctx = ToolContext(
        browser=_B(), config=None, llm_agent=None,
        url="u", run_mode="m", start_date="s", end_date="e",
    )
    state = {
        "tool_calls_log": [],
        "iterations": 5,
        "generated_code": "print('x')",
        "critic_verdict": {"passed": False, "summary": "nope", "issues": [],
                           "recommendations": [], "details": {}},
        "messages": [],
    }
    result = _build_result_from_state(state, ctx, error=None)
    assert result.success is False
    assert "nope" in (result.error or "")
