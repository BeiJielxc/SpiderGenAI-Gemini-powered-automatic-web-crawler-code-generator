"""Supervisor scaffolding + runner contract tests."""

from __future__ import annotations

import pytest


def test_register_agent_roundtrip():
    from agents.supervisor import AGENT_REGISTRY, register_agent

    def factory():
        return "hello"

    register_agent("test-worker", factory)
    assert AGENT_REGISTRY["test-worker"] is factory


def test_acquisition_routes_support_api_dom_and_hybrid():
    from agents.supervisor import acquisition_route, after_api_route

    assert acquisition_route({"acquisition_route": "api"}) == "api"
    assert acquisition_route({"acquisition_route": "dom"}) == "dom"
    assert acquisition_route({"acquisition_route": "hybrid"}) == "api"
    assert after_api_route({"acquisition_route": "hybrid"}) == "dom"
    assert after_api_route({"acquisition_route": "api"}) == "dom"
    assert after_api_route({
        "acquisition_route": "api",
        "enhanced_analysis": {"captured_data_api": {"bestApi": {"url": "https://x/api"}}},
    }) == "gate"


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


@pytest.mark.asyncio
async def test_golden_replay_returns_before_model_or_graph_initialization():
    from agents.runner import run_agent

    class ExplodingConfig:
        def __getattr__(self, name):
            raise AssertionError(f"golden replay touched config.{name}")

    logs = []
    result = await run_agent(
        browser=None,
        config=ExplodingConfig(),
        llm_agent=None,
        url="https://example.com/news",
        run_mode="news_sentiment",
        start_date="2026-01-01",
        end_date="2026-01-31",
        task_id="task-1",
        reusable_script_code="print('golden')\n",
        task_signature="a" * 64,
        golden_code_path="output/golden_crawlers/active/example.py",
        log_callback=logs.append,
    )

    assert result.success is True
    assert result.script_code == "print('golden')\n"
    assert result.iterations == 0
    assert result.tool_calls == []
    assert result.final_state["execution_source"] == "golden_replay"
    assert any("跳过 LLM" in line for line in logs)
