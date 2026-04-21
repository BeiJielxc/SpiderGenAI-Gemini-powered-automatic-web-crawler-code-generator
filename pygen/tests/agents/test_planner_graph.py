"""Tests for the planner graph routing around the ReAct agent + critic."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage


def test_need_critic_returns_end_when_cancelled():
    from agents.planner_graph import _need_critic

    assert _need_critic({"cancelled": True}) == "end"


def test_need_critic_returns_critic_when_code_present_and_no_pending_tool_calls():
    from agents.planner_graph import _need_critic

    state = {
        "cancelled": False,
        "generated_code": "print('x')",
        "critic_verdict": None,
        "messages": [AIMessage(content="done")],
    }
    assert _need_critic(state) == "critic"


def test_need_critic_returns_end_when_no_code():
    from agents.planner_graph import _need_critic

    state = {
        "cancelled": False,
        "generated_code": "",
        "critic_verdict": None,
        "messages": [AIMessage(content="stuck")],
    }
    assert _need_critic(state) == "end"


def test_critic_passed_routes_to_end_on_pass():
    from agents.planner_graph import _critic_passed

    assert _critic_passed({"critic_verdict": {"passed": True}}) == "end"


def test_critic_passed_loops_back_on_fail():
    from agents.planner_graph import _critic_passed

    state = {"critic_verdict": {"passed": False}, "critic_rounds": 1}
    assert _critic_passed(state) == "react"


def test_critic_passed_stops_after_rounds_exceeded():
    from agents.planner_graph import _critic_passed

    state = {"critic_verdict": {"passed": False}, "critic_rounds": 3}
    assert _critic_passed(state) == "end"


def test_initial_user_message_includes_task_objective():
    from agents.state import initial_state
    from agents.planner_graph import build_initial_state_messages

    state = initial_state(
        url="https://example.com",
        run_mode="enterprise_report",
        start_date="2024-01-01",
        end_date="2024-12-31",
        extra_requirements="look for quarterly reports",
    )
    state = build_initial_state_messages(state)
    assert state["messages"]
    first = state["messages"][0]
    assert isinstance(first, HumanMessage)
    assert "https://example.com" in first.content
    assert "quarterly reports" in first.content


def test_critic_feedback_node_resets_verdict_and_injects_message():
    from agents.planner_graph import critic_feedback_node

    state = {
        "critic_verdict": {
            "passed": False,
            "summary": "missing main guard",
            "details": {"primary_cause": "missing_main_guard"},
            "recommendations": ["add if __name__", "dump json"],
        },
        "messages": [],
    }
    out = critic_feedback_node(state, config={})
    assert out["critic_verdict"] is None
    assert out["critic_rounds"] == 0
    assert len(out["messages"]) == 1
    msg = out["messages"][0]
    assert isinstance(msg, HumanMessage)
    assert "CRITIC FEEDBACK" in msg.content
    assert "missing_main_guard" in msg.content


def test_build_planner_graph_compiles_without_network():
    """The planner graph must compile fully without any network calls."""
    from agents.llm import build_chat_model
    from agents.planner_graph import build_planner_graph
    from agents.tools_lc import select_tools_for_context

    class Cfg:
        qwen_model = "gpt-4o-mini"
        qwen_base_url = "https://api.openai.com/v1"

        @property
        def qwen_api_key(self):
            return "sk-test"

    model = build_chat_model(Cfg(), temperature=0)
    tools = select_tools_for_context(has_executor=True, has_critic=True)
    graph = build_planner_graph(llm=model, tools=tools, max_iterations=5)
    assert graph is not None
