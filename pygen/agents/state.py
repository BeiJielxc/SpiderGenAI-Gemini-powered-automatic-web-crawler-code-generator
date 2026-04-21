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


class AgentState(TypedDict, total=False):
    """State shared by the planner, critic and codegen subgraphs."""

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
    page_info: Optional[Dict[str, Any]]
    page_html_len: Optional[int]  # only length; full HTML stays on ToolContext
    page_structure: Optional[Dict[str, Any]]
    network_requests: Optional[Dict[str, Any]]
    menu_tree: Optional[Dict[str, Any]]
    verified_mapping: Optional[Dict[str, Any]]
    # Structured ledger of selectors proven to work on the live page. Filled
    # by extract_list_and_pagination / probe_detail_page / verify_selector
    # tool hooks; consumed by codegen build_prompt_node as a hard constraint.
    # See pygen.verified_selectors for the schema.
    verified_selectors: Optional[Dict[str, Any]]
    enhanced_analysis: Dict[str, Any]
    date_api_result: Optional[Dict[str, Any]]
    screenshots_count: int

    # ---- generation outputs ----
    generated_code: Optional[str]
    code_strategy: Optional[str]

    # ---- critic subgraph state ----
    critic_rounds: int
    critic_verdict: Optional[Dict[str, Any]]
    critic_repaired_code: Optional[str]
    critic_evidence: Annotated[List[Dict[str, Any]], operator.add]

    # ---- planner/supervisor control ----
    iterations: int
    # Use operator.add so each tool wrapper can append a single-element list
    # and LangGraph concatenates across steps (mirrors legacy PlannerResult.tool_calls).
    tool_calls_log: Annotated[List[Dict[str, Any]], operator.add]
    strategy_summary: str
    cancelled: bool
    finish_requested: bool
    last_error: Optional[str]

    # ---- persistent memory bridge (Stage-2 feedback flow) ----
    # Read-only at the planner level; written by runner.run_agent before
    # ainvoke() based on prior episodes. The planner subgraph renders
    # them into the user prompt with priority feedback > site > task.
    site_memory_hint: Optional[str]
    feedback_replay_hint: Optional[str]
    prev_task_id: Optional[str]
    # Wall-clock start time (epoch seconds) — used by summarize_node to
    # compute duration_sec without re-instrumenting tool wrappers.
    started_at: Optional[float]
    # Stage-1 outputs from summarize_node
    auto_findings: Optional[Dict[str, Any]]
    summary_draft_path: Optional[str]
    html_fingerprint: Optional[str]


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
    )


__all__ = ["AgentState", "initial_state"]
