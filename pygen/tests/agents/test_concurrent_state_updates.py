"""Regression tests for LangGraph concurrent tool-call state writes."""

from __future__ import annotations


def test_single_tool_call_guard_keeps_only_first_call():
    from langchain_core.messages import AIMessage

    from agents.specialists import single_tool_call_guard

    message = AIMessage(
        id="model-turn-1",
        content="",
        tool_calls=[
            {"name": "open_page", "args": {}, "id": "call-1", "type": "tool_call"},
            {"name": "get_page_info", "args": {}, "id": "call-2", "type": "tool_call"},
        ],
    )
    update = single_tool_call_guard({"messages": [message]})
    guarded = update["messages"][0]
    assert guarded.id == message.id
    assert [call["id"] for call in guarded.tool_calls] == ["call-1"]


def test_agent_state_reducers_accept_parallel_tool_mirrors():
    from langgraph.graph import END, START, StateGraph

    from agents.state import AgentState

    def first(_state):
        return {"generated_code": None, "page_info": {"title": "A"}}

    def second(_state):
        return {"generated_code": None, "page_info": {"title": "B"}}

    graph = StateGraph(AgentState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge(START, "second")
    graph.add_edge("first", END)
    graph.add_edge("second", END)
    result = graph.compile().invoke({"messages": [], "enhanced_analysis": {}})
    assert result["generated_code"] is None
    assert result["page_info"]["title"] in {"A", "B"}


def test_tool_log_reducer_deduplicates_nested_graph_results():
    from agents.state import merge_tool_logs

    first = {"tool_call_id": "call-1", "action": "open_page"}
    second = {"tool_call_id": "call-2", "action": "get_page_info"}
    assert merge_tool_logs([first], [first, second]) == [first, second]


def test_validation_and_repair_reducers_deduplicate_nested_results():
    from agents.state import merge_repair_history, merge_validation_reports

    report = {"gate": "code_gate", "created_at": "t1", "summary": "empty"}
    repair = {
        "attempt": 1, "failure_type": "empty_code",
        "rollback_target": "codegen", "reason": "empty",
    }
    assert merge_validation_reports([report], [report]) == [report]
    assert merge_repair_history([repair], [repair]) == [repair]


async def _noop_async(*_args, **_kwargs):
    return None


def test_codegen_specialist_is_required_node_not_outer_react_agent(monkeypatch):
    import agents.specialists as specialists
    from agents.tools_lc import run_required_codegen

    monkeypatch.setattr(specialists, "create_react_agent", lambda **_kwargs: object())
    agents = specialists.build_specialist_agents(llm=object())
    assert agents["codegen"] is run_required_codegen
    assert "codegen" not in specialists.SPECIALIST_PROMPTS

    from agents.tools_lc import SPECIALIST_TOOL_NAMES
    assert "codegen" not in SPECIALIST_TOOL_NAMES
