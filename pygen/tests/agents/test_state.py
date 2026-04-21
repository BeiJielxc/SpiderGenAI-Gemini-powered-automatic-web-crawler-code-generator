"""Unit tests for AgentState reducer behavior."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages


def test_initial_state_has_expected_fields():
    from agents.state import initial_state

    s = initial_state(
        url="https://example.com",
        run_mode="enterprise_report",
        start_date="2024-01-01",
        end_date="2024-12-31",
        extra_requirements="foo",
        task_id="t-1",
    )
    assert s["url"] == "https://example.com"
    assert s["run_mode"] == "enterprise_report"
    assert s["iterations"] == 0
    assert s["critic_rounds"] == 0
    assert s["tool_calls_log"] == []
    assert s["enhanced_analysis"] == {}
    assert s["cancelled"] is False


def test_add_messages_reducer_appends_not_overwrites():
    base = [HumanMessage(content="hi")]
    delta = [AIMessage(content="there")]
    merged = add_messages(base, delta)
    assert len(merged) == 2
    assert merged[0].content == "hi"
    assert merged[1].content == "there"


def test_tool_calls_log_concatenation_via_operator_add():
    """We rely on ``operator.add`` so partial updates append correctly."""
    import operator

    base = [{"action": "open_page"}]
    delta = [{"action": "extract_list_and_pagination"}]
    merged = operator.add(base, delta)
    assert [m["action"] for m in merged] == ["open_page", "extract_list_and_pagination"]


def test_critic_evidence_concatenation():
    import operator

    merged = operator.add([{"step": "round_summary"}], [{"step": "llm_repair"}])
    assert [m["step"] for m in merged] == ["round_summary", "llm_repair"]
