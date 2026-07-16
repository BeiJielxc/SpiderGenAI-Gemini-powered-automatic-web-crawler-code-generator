"""AgentState — the single source of truth flowing through the LangGraph.

Design notes:
- ``messages`` uses LangGraph's ``add_messages`` reducer so tool nodes can
  append ToolMessage / AIMessage without overwriting history.
- Non-serializable runtime handles (browser, executor_session, llm_agent,
  artifact_store, log_callback, cancel_check) are NOT stored here. They are
  passed via ``RunnableConfig['configurable']`` so that checkpoints remain
  JSON-serializable.
- All fields are intentionally ``Optional`` / have ``total=False`` behavior
  so partial ``Command(update=...)`` writes from tool wrappers work without
  forcing callers to provide every key.
"""

from __future__ import annotations

import operator
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langgraph.managed import RemainingSteps
from typing_extensions import Annotated


def latest_value(_current: Any, incoming: Any) -> Any:
    """Allow concurrent tool mirrors while keeping LangGraph's stable write order."""
    return incoming


def merge_mapping(current: Any, incoming: Any) -> Dict[str, Any]:
    """Merge independent top-level analysis keys emitted by concurrent tools."""
    merged = dict(current or {})
    merged.update(dict(incoming or {}))
    return merged


def merge_tool_logs(
    current: Optional[List[Dict[str, Any]]],
    incoming: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Merge nested-graph tool logs without re-appending the same call."""
    merged: List[Dict[str, Any]] = []
    seen_call_ids = set()
    seen_legacy_objects = set()
    for entry in list(current or []) + list(incoming or []):
        call_id = entry.get("tool_call_id") if isinstance(entry, dict) else None
        if call_id:
            if call_id in seen_call_ids:
                continue
            seen_call_ids.add(call_id)
        else:
            # Compatibility for old/manual state updates without call ids. An
            # identical dict returned by a nested subgraph should appear once.
            legacy_key = repr(entry)
            if legacy_key in seen_legacy_objects:
                continue
            seen_legacy_objects.add(legacy_key)
        merged.append(entry)
    return merged


def _merge_unique_dicts(
    current: Optional[List[Dict[str, Any]]],
    incoming: Optional[List[Dict[str, Any]]],
    key_fields: tuple[str, ...],
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for entry in list(current or []) + list(incoming or []):
        key = tuple(repr(entry.get(field)) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def merge_validation_reports(current, incoming) -> List[Dict[str, Any]]:
    return _merge_unique_dicts(current, incoming, ("gate", "created_at", "summary"))


def merge_repair_history(current, incoming) -> List[Dict[str, Any]]:
    return _merge_unique_dicts(
        current, incoming, ("attempt", "failure_type", "rollback_target", "reason")
    )


class AgentState(TypedDict, total=False):
    """State shared by the supervisor, specialists and critic subgraphs."""

    # ---- chat history (merged via add_messages reducer) ----
    messages: Annotated[List[AnyMessage], add_messages]

    # ---- required by langgraph.prebuilt.create_react_agent (internal budget) ----
    remaining_steps: RemainingSteps

    # ---- task parameters (immutable for a run) ----
    url: str
    run_mode: str
    start_date: str
    end_date: str
    extra_requirements: str
    task_id: str

    # ---- collected context (tool-populated, serializable) ----
    page_info: Annotated[Optional[Dict[str, Any]], latest_value]
    page_html_len: Annotated[Optional[int], latest_value]  # full HTML stays on ToolContext
    page_structure: Annotated[Optional[Dict[str, Any]], latest_value]
    network_requests: Annotated[Optional[Dict[str, Any]], latest_value]
    menu_tree: Annotated[Optional[Dict[str, Any]], latest_value]
    verified_mapping: Annotated[Optional[Dict[str, Any]], latest_value]
    # Structured ledger of selectors proven to work on the live page. Filled
    # by extract_list_and_pagination / probe_detail_page / verify_selector
    # tool hooks; consumed by codegen build_prompt_node as a hard constraint.
    # See pygen.verified_selectors for the schema.
    verified_selectors: Annotated[Optional[Dict[str, Any]], latest_value]
    enhanced_analysis: Annotated[Dict[str, Any], merge_mapping]
    date_api_result: Annotated[Optional[Dict[str, Any]], latest_value]
    screenshots_count: Annotated[int, latest_value]

    # ---- generation outputs ----
    generated_code: Annotated[Optional[str], latest_value]
    code_strategy: Annotated[Optional[str], latest_value]

    # ---- critic subgraph state ----
    critic_rounds: int
    critic_verdict: Optional[Dict[str, Any]]
    critic_repaired_code: Optional[str]
    critic_evidence: Annotated[List[Dict[str, Any]], operator.add]

    # ---- supervisor control ----
    iterations: int
    # Use operator.add so each tool wrapper can append a single-element list
    # and LangGraph concatenates across steps (mirrors legacy PlannerResult.tool_calls).
    tool_calls_log: Annotated[List[Dict[str, Any]], merge_tool_logs]
    strategy_summary: str
    cancelled: bool
    finish_requested: bool
    last_error: Optional[str]

    # ---- persistent memory bridge (Stage-2 feedback flow) ----
    # Written by runner.run_agent before ainvoke() based on prior episodes.
    # Initial messages expose them to the specialist graph with priority
    # feedback > site > task.
    site_memory_hint: Optional[str]
    feedback_replay_hint: Optional[str]
    prev_task_id: Optional[str]
    # Wall-clock start time (epoch seconds), consumed by runtime finalization.
    started_at: Optional[float]
    # Stage-1 outputs from runtime-grounded finalization
    auto_findings: Optional[Dict[str, Any]]
    summary_draft_path: Optional[str]
    html_fingerprint: Optional[str]

    # ---- evidence-driven specialist orchestration ----
    stage_evidence: Dict[str, Dict[str, Any]]
    validation_reports: Annotated[List[Dict[str, Any]], merge_validation_reports]
    router_decision: Optional[Dict[str, Any]]
    acquisition_route: Optional[str]
    attribution_decision: Optional[Dict[str, Any]]
    rollback_target: Optional[str]
    rollback_count: int
    repair_history: Annotated[List[Dict[str, Any]], merge_repair_history]
    runtime_report: Optional[Dict[str, Any]]
    final_output: Optional[Dict[str, Any]]


def initial_state(
    *,
    url: str,
    run_mode: str,
    start_date: str,
    end_date: str,
    extra_requirements: str = "",
    task_id: str = "",
    site_memory_hint: Optional[str] = None,
    feedback_replay_hint: Optional[str] = None,
    prev_task_id: Optional[str] = None,
    started_at: Optional[float] = None,
) -> AgentState:
    """Factory producing a well-formed starting state."""
    return AgentState(
        messages=[],
        url=url,
        run_mode=run_mode,
        start_date=start_date,
        end_date=end_date,
        extra_requirements=extra_requirements,
        task_id=task_id,
        page_info=None,
        page_html_len=None,
        page_structure=None,
        network_requests=None,
        menu_tree=None,
        verified_mapping=None,
        verified_selectors=None,
        enhanced_analysis={},
        date_api_result=None,
        screenshots_count=0,
        generated_code=None,
        code_strategy=None,
        critic_rounds=0,
        critic_verdict=None,
        critic_repaired_code=None,
        critic_evidence=[],
        iterations=0,
        tool_calls_log=[],
        strategy_summary="",
        cancelled=False,
        finish_requested=False,
        last_error=None,
        site_memory_hint=site_memory_hint,
        feedback_replay_hint=feedback_replay_hint,
        prev_task_id=prev_task_id,
        started_at=started_at,
        auto_findings=None,
        summary_draft_path=None,
        html_fingerprint=None,
        stage_evidence={},
        validation_reports=[],
        router_decision=None,
        acquisition_route=None,
        attribution_decision=None,
        rollback_target=None,
        rollback_count=0,
        repair_history=[],
        runtime_report=None,
        final_output=None,
    )


__all__ = [
    "AgentState", "initial_state", "latest_value", "merge_mapping",
    "merge_tool_logs", "merge_validation_reports", "merge_repair_history",
]
