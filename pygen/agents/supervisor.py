"""Evidence-driven specialist supervisor.

The main stages are fixed.  Acquisition and rollback routes are dynamic, while
specialist conclusions are accepted only by deterministic gates.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Any, Callable, Dict, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from .critic_graph import build_critic_graph
from .evidence import AttributionDecision
from .gates import (
    acquisition_gate,
    code_gate,
    critic_output_gate,
    date_scope_gate,
    last_gate_passed,
    site_profile_gate,
)
from .specialists import build_specialist_agents
from .state import AgentState
from .tools_lc import (
    run_required_api_discovery,
    run_required_date_scope,
    run_required_selector,
    run_required_site_profile,
)


AGENT_REGISTRY: Dict[str, Callable[..., Any]] = {}
MAX_ROLLBACKS = 5


def register_agent(name: str, factory: Callable[..., Any]) -> None:
    AGENT_REGISTRY[name] = factory


def _json_object(text: Any) -> Optional[Dict[str, Any]]:
    raw = text if isinstance(text, str) else str(text or "")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1]).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


async def acquisition_router_node(state: AgentState, *, llm) -> Dict[str, Any]:
    network = state.get("network_requests") or {}
    structure = state.get("page_structure") or {}
    prompt = {
        "url": state.get("url"),
        "objective": state.get("extra_requirements"),
        "network_keys": list(network.keys()) if isinstance(network, dict) else [],
        "api_request_count": len(network.get("api_requests") or []) if isinstance(network, dict) else 0,
        "page_structure_keys": list(structure.keys())[:30] if isinstance(structure, dict) else [],
        "date_range": [state.get("start_date"), state.get("end_date")],
    }
    messages = [
        SystemMessage(content=(
            "Route web-data acquisition. Return JSON only: "
            '{"route":"api|dom|hybrid","confidence":0.0,"reason":"..."}. '
            "Use hybrid when an API exists but replayability or field coverage is uncertain."
        )),
        HumanMessage(content=json.dumps(prompt, ensure_ascii=False, default=str)),
    ]
    decision: Dict[str, Any] = {}
    try:
        response = await llm.ainvoke(messages)
        decision = _json_object(getattr(response, "content", response)) or {}
    except Exception as exc:
        decision = {"route": "hybrid", "confidence": 0.25, "reason": f"router_error: {exc}"}
    route = str(decision.get("route") or "hybrid").strip().lower()
    if route not in {"api", "dom", "hybrid"}:
        route = "hybrid"
    # A captured API is a useful signal, but uncertainty still goes hybrid.
    if route == "api" and not (isinstance(network, dict) and network.get("api_requests")):
        route = "hybrid"
        decision["reason"] = "No captured API evidence; expanded route to hybrid."
    decision["route"] = route
    return {"acquisition_route": route, "router_decision": decision}


def acquisition_route(state: AgentState) -> str:
    route = state.get("acquisition_route") or "hybrid"
    return "api" if route in {"api", "hybrid"} else "dom"


def after_api_route(state: AgentState) -> str:
    enhanced = state.get("enhanced_analysis") or {}
    has_verified_data_api = bool(
        isinstance(enhanced, dict) and enhanced.get("captured_data_api")
    )
    # Raw XHR capture is not enough proof to skip DOM discovery. This also
    # prevents analytics/telemetry requests from being treated as business APIs.
    if state.get("acquisition_route") == "hybrid" or not has_verified_data_api:
        return "dom"
    return "gate"


def _latest_failure(state: AgentState) -> Dict[str, Any]:
    for report in reversed(state.get("validation_reports") or []):
        if not report.get("passed"):
            return report
    return {}


_CAUSE_TO_STAGE = {
    "site_unreachable": "site_profiler",
    "blocked_by_waf": "site_profiler",
    "network_error": "site_profiler",
    "rate_limited": "site_profiler",
    "acquisition_evidence_missing": "selector",
    "api_param_invalid": "api_discovery",
    "api_schema_mismatch": "api_discovery",
    "selector_mismatch": "selector",
    "pagination_date_lost": "selector",
    "pagination_not_advancing": "selector",
    "date_strategy_unverified": "date_scope",
    "date_extraction_failed": "date_scope",
    "date_filter_too_strict": "date_scope",
    "empty_code": "codegen",
    "schema_mismatch": "codegen",
    "null_pointer": "codegen",
    "hardcoded_index": "codegen",
    "timeout": "codegen",
}


async def attribution_node(state: AgentState, *, llm) -> Dict[str, Any]:
    failure = _latest_failure(state)
    failure_type = str(failure.get("failure_type") or "unknown")
    target = failure.get("rollback_target") or _CAUSE_TO_STAGE.get(failure_type)
    reason = str(failure.get("summary") or "No deterministic attribution available.")
    confidence = 0.9 if target else 0.0

    if not target:
        compact = {
            "failure": failure,
            "stage_evidence": state.get("stage_evidence") or {},
            "recent_tools": (state.get("tool_calls_log") or [])[-12:],
        }
        try:
            response = await llm.ainvoke([
                SystemMessage(content=(
                    "Attribute crawler failure to exactly one rollback target from "
                    "site_profiler, api_discovery, selector, date_scope, codegen. "
                    "Return JSON only with target, confidence and reason."
                )),
                HumanMessage(content=json.dumps(compact, ensure_ascii=False, default=str)[:18000]),
            ])
            parsed = _json_object(getattr(response, "content", response)) or {}
            candidate = str(parsed.get("target") or "")
            if candidate in {"site_profiler", "api_discovery", "selector", "date_scope", "codegen"}:
                target = candidate
                confidence = float(parsed.get("confidence") or 0.5)
                reason = str(parsed.get("reason") or reason)
        except Exception as exc:
            reason = f"Attribution LLM failed: {exc}"

    target = str(target or "codegen")
    count = int(state.get("rollback_count") or 0) + 1
    if count > MAX_ROLLBACKS:
        target = "exhausted"
        reason = f"Rollback budget exhausted after {MAX_ROLLBACKS} attempts. Last cause: {failure_type}"
    decision = AttributionDecision(
        failure_type=failure_type,
        suspected_stages=[{"stage": target, "probability": confidence, "reason": reason}],
        rollback_target=target,
        confidence=confidence,
        reason=reason,
    )
    updates: Dict[str, Any] = {
        "attribution_decision": decision.to_dict(),
        "rollback_target": target,
        "rollback_count": count,
        "repair_history": [{
            "attempt": count,
            "failure_type": failure_type,
            "rollback_target": target,
            "reason": reason,
        }],
    }
    if target != "exhausted":
        updates.update({
            "generated_code": None,
            "critic_verdict": None,
            "critic_rounds": 0,
        })
    return updates


def rollback_route(state: AgentState) -> str:
    target = state.get("rollback_target") or "exhausted"
    return target if target in {
        "site_profiler", "api_discovery", "selector", "date_scope", "codegen", "exhausted"
    } else "exhausted"


def build_supervisor_graph(
    *,
    llm=None,
    has_executor: bool = True,
    checkpointer=None,
    planner_graph=None,
    extra_workers: Optional[Dict[str, Any]] = None,
):
    """Build the production specialist graph.

    ``planner_graph`` remains an explicit compatibility escape hatch for one
    release; new callers should pass ``llm`` and use the specialist graph.
    """
    if llm is None:
        if planner_graph is None:
            raise ValueError("llm is required for the specialist supervisor")
        legacy = StateGraph(AgentState)
        legacy.add_node("planner", planner_graph)
        legacy.set_entry_point("planner")
        legacy.add_edge("planner", END)
        return legacy.compile(checkpointer=checkpointer) if checkpointer else legacy.compile()

    workers = build_specialist_agents(llm=llm, has_executor=has_executor)
    workers.update(extra_workers or {})

    graph = StateGraph(AgentState)
    for name, worker in workers.items():
        graph.add_node(name, worker)
    graph.add_node("site_profile_gate", site_profile_gate)
    graph.add_node("site_profile_required", run_required_site_profile)
    graph.add_node("acquisition_router", partial(acquisition_router_node, llm=llm))
    graph.add_node("api_discovery_required", run_required_api_discovery)
    graph.add_node("selector_required", run_required_selector)
    graph.add_node("acquisition_gate", acquisition_gate)
    graph.add_node("date_scope_required", run_required_date_scope)
    graph.add_node("date_scope_gate", date_scope_gate)
    graph.add_node("code_gate", code_gate)
    graph.add_node("critic", build_critic_graph())
    graph.add_node("critic_output_gate", critic_output_gate)
    graph.add_node("attribution_critic", partial(attribution_node, llm=llm))

    graph.set_entry_point("site_profiler")
    graph.add_edge("site_profiler", "site_profile_required")
    graph.add_edge("site_profile_required", "site_profile_gate")
    graph.add_conditional_edges(
        "site_profile_gate", last_gate_passed,
        {"pass": "acquisition_router", "fail": "attribution_critic"},
    )
    graph.add_conditional_edges(
        "acquisition_router", acquisition_route,
        {"api": "api_discovery", "dom": "selector"},
    )
    graph.add_conditional_edges(
        "api_discovery_required", after_api_route,
        {"dom": "selector", "gate": "acquisition_gate"},
    )
    graph.add_edge("api_discovery", "api_discovery_required")
    graph.add_edge("selector", "selector_required")
    graph.add_edge("selector_required", "acquisition_gate")
    graph.add_conditional_edges(
        "acquisition_gate", last_gate_passed,
        {"pass": "date_scope", "fail": "attribution_critic"},
    )
    graph.add_edge("date_scope", "date_scope_required")
    graph.add_edge("date_scope_required", "date_scope_gate")
    graph.add_conditional_edges(
        "date_scope_gate", last_gate_passed,
        {"pass": "codegen", "fail": "attribution_critic"},
    )
    graph.add_edge("codegen", "code_gate")
    graph.add_conditional_edges(
        "code_gate", last_gate_passed,
        {"pass": "critic", "fail": "attribution_critic"},
    )
    graph.add_edge("critic", "critic_output_gate")
    graph.add_conditional_edges(
        "critic_output_gate", last_gate_passed,
        {"pass": END, "fail": "attribution_critic"},
    )
    graph.add_conditional_edges(
        "attribution_critic", rollback_route,
        {
            "site_profiler": "site_profiler",
            "api_discovery": "api_discovery",
            "selector": "selector",
            "date_scope": "date_scope",
            "codegen": "codegen",
            "exhausted": END,
        },
    )
    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


__all__ = [
    "AGENT_REGISTRY",
    "MAX_ROLLBACKS",
    "acquisition_router_node",
    "attribution_node",
    "build_supervisor_graph",
    "register_agent",
]
