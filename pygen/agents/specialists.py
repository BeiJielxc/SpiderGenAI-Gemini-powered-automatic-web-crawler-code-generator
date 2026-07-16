"""Specialist agent factories for the evidence-driven orchestration graph."""

from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent

from .state import AgentState
from .tools_lc import run_required_codegen, select_tools_for_specialist


SPECIALIST_PROMPTS: Dict[str, str] = {
    "site_profiler": """You are the Site Profiler specialist. Work only on live-site access,
page type, target business region, rendering mode, network shape and anti-bot risk. Open the
task URL, collect page information, HTML/structure and network evidence. Do not generate code.
Do not claim success from one weak signal. Finish when downstream specialists have enough
observable context, and state remaining risks succinctly.""",
    "api_discovery": """You are the API Discovery specialist. Inspect captured XHR/fetch
requests and identify up to three replayable list-data API candidates. Verify response shape,
field mapping, pagination behavior and date parameters by perturbing inputs when possible.
Keep alternatives and risks. Do not generate crawler code and do not invent parameters.""",
    "selector": """You are the Selector specialist. Find and live-verify list container,
title link, date, detail content and pagination selectors. Reject navigation, footer, sidebar
and recommendation regions. Verify samples, link behavior and pagination change. Keep backup
candidates. Do not generate crawler code.""",
    "date_scope": """You are the Date and Scope specialist. Determine whether filtering
belongs in API parameters, DOM extraction or generated code. Inspect raw date samples,
normalization and inclusive boundaries. Never approve a strategy that silently turns a
non-empty candidate set into zero records. Do not generate final crawler code.""",
}


def single_tool_call_guard(state: AgentState) -> Dict[str, Any]:
    """Serialize model-requested tools so browser/context mutations stay ordered.

    LangGraph v2 fans multiple tool calls out through ``Send``. The tools share
    one live browser and ``ToolContext``, so running them in parallel is both
    unsafe and capable of producing concurrent state writes. Replacing the
    latest AIMessage with the same message id preserves one call and lets the
    model request the remaining work on subsequent ReAct turns.
    """
    messages = state.get("messages") or []
    if not messages:
        return {}
    latest = messages[-1]
    if not isinstance(latest, AIMessage) or len(latest.tool_calls or []) <= 1:
        return {}
    guarded = latest.model_copy(update={"tool_calls": list(latest.tool_calls[:1])})
    return {"messages": [guarded]}


def build_specialist_agents(*, llm, has_executor: bool = True) -> Dict[str, Any]:
    agents: Dict[str, Any] = {}
    for name, prompt in SPECIALIST_PROMPTS.items():
        agents[name] = create_react_agent(
            model=llm,
            tools=select_tools_for_specialist(name, has_executor=has_executor),
            state_schema=AgentState,
            prompt=prompt,
            post_model_hook=single_tool_call_guard,
            name=name,
        )
    agents["codegen"] = run_required_codegen
    return agents


__all__ = ["SPECIALIST_PROMPTS", "build_specialist_agents", "single_tool_call_guard"]
