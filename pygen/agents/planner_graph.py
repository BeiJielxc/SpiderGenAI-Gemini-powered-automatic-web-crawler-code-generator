"""Main ReAct planner graph — replaces ``pygen.planner.AgentPlanner``.

Structure:

```
  START --> react_agent (create_react_agent)
     ^              |
     |              v
     |          _need_critic?
     |          /         \\
     |         /           \\
     |   "react"        "critic"
     |      |               |
     |      +--- END (no code & no tool_calls: give up)
     |                      |
     |                      v
     |                 critic_subgraph
     |                      |
     |                      v
     |                 _critic_passed?
     |                 /           \\
     |            "react"           "done"
     |              |                  \\
     +--------------+                   v
                                       END
```

The outer graph wraps LangGraph's prebuilt ``create_react_agent`` and
adds a Critic gate: when the ReAct loop stops producing tool_calls AND
a ``generated_code`` exists in state, we run the critic subgraph. If the
critic fails we inject a feedback message and re-enter the ReAct loop.

The ReAct agent itself (from ``create_react_agent``) does all the
iterate-until-no-tool-calls heavy lifting, giving us native function
calling + automatic tool_calls/ToolMessage plumbing for free.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import create_react_agent

try:
    from prompts import load as load_prompt
except ImportError:  # pragma: no cover
    from ..prompts import load as load_prompt  # type: ignore

from .critic_graph import build_critic_graph
from .state import AgentState
from .summarize_node import summarize_node


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

MAX_PLANNER_ITERATIONS_CONFIG_KEY = "max_iterations"
ENABLE_CRITIC_CONFIG_KEY = "enable_critic"


RUN_MODE_HINTS: Dict[str, str] = {
    "news_sentiment": "Focus on news articles and sentiment signals.",
    "enterprise_report": "Focus on enterprise disclosure / report documents.",
}


def _render_system_prompt(max_iterations: int) -> str:
    """Load the LangGraph-native planner prompt."""
    try:
        return load_prompt(
            "planner/system.md",
            max_iterations=max_iterations,
        )
    except Exception:
        # Defensive fallback — never let prompt-loading errors take down the graph
        return (
            "You are a web crawler code-generation agent. Use the tools to "
            "explore the site and generate code. Budget: "
            f"{max_iterations} tool calls."
        )


def _build_initial_user_message(state: AgentState) -> HumanMessage:
    """Concatenate persistent-memory hints + the task block.

    Priority order (top of prompt = highest priority):

    1. ``feedback_replay_hint``  — last-run errors + user suggestion
       (rerun path; **always wins** when present).
    2. ``site_memory_hint``      — past site profile (hint, must
       re-verify before reuse).
    3. The standard Task / Parameters block.
    """
    run_mode = state.get("run_mode", "")
    run_mode_hint = RUN_MODE_HINTS.get(run_mode, "")
    parts: list = []

    # ---- (1) highest priority: feedback replay (rerun) ----
    feedback_hint = (state.get("feedback_replay_hint") or "").strip()
    if feedback_hint:
        parts.append(feedback_hint)
        parts.append("")

    # ---- (2) site memory hint (still re-verify) ----
    site_hint = (state.get("site_memory_hint") or "").strip()
    if site_hint:
        parts.append(site_hint)
        parts.append("")

    # ---- (3) standard task block ----
    parts.extend([
        "## Task",
        f"Generate a crawler script for: {state.get('url', '')}",
        "",
        "## Parameters",
        f"- Run mode: {run_mode}",
        f"- Date range: {state.get('start_date','')} ~ {state.get('end_date','')}",
    ])
    if run_mode_hint:
        parts.append(f"- Mode hint: {run_mode_hint}")
    extra = state.get("extra_requirements") or ""
    if extra.strip():
        parts.append(f"- Task objective (highest priority): {extra}")
    parts.append("")
    parts.append("Start by opening and analyzing the page.")
    return HumanMessage(content="\n".join(parts))


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _last_ai_message(state: AgentState) -> Optional[AIMessage]:
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _need_critic(state: AgentState) -> str:
    """Decide whether to run the critic after the ReAct agent settles."""
    if state.get("cancelled"):
        return "end"
    # If the last AI message still has pending tool_calls, ``create_react_agent``
    # should have kept looping — treat as "back to react" defensively.
    last = _last_ai_message(state)
    if last is not None and getattr(last, "tool_calls", None):
        return "react"
    code = state.get("generated_code") or ""
    configurable = None
    if isinstance(state.get("_critic_disabled"), bool):
        configurable = {"enable_critic": not state["_critic_disabled"]}
    # We only run critic if we have code AND critic is enabled for this run.
    if code.strip() and state.get("critic_verdict") is None:
        return "critic"
    return "end"


def _critic_passed(state: AgentState) -> str:
    if state.get("cancelled"):
        return "end"
    verdict = state.get("critic_verdict") or {}
    if verdict.get("passed"):
        return "end"
    # Fallback: if we've already spent the full iteration budget, stop looping.
    iterations = int(state.get("iterations", 0))
    # Budget is enforced inside react via recursion_limit; we also stop here
    # to avoid repeated critic rounds after a hopeless retry.
    if iterations >= 0 and int(state.get("critic_rounds", 0)) >= 3:
        return "end"
    return "react"


# ---------------------------------------------------------------------------
# Post-critic feedback injection
# ---------------------------------------------------------------------------


def critic_feedback_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """After the critic fails, inject a HumanMessage summarizing the issues
    so the ReAct agent has fresh context when it re-enters the loop."""
    verdict = state.get("critic_verdict") or {}
    if verdict.get("passed"):
        return {}
    summary = verdict.get("summary") or "Critic failed, please retry."
    details = verdict.get("details") or {}
    cause = details.get("primary_cause", "unknown")
    recs = (verdict.get("recommendations") or [])[:5]
    rec_text = "\n".join(f"- {r}" for r in recs) if recs else "- (no specific recommendations)"
    msg = (
        f"[CRITIC FEEDBACK]\n"
        f"The generated crawler code did not pass the critic.\n"
        f"Primary cause: {cause}\n"
        f"Summary: {summary}\n"
        f"Recommendations:\n{rec_text}\n\n"
        "Please adjust your strategy and call the appropriate tools to "
        "regenerate or repair the crawler code."
    )
    # Clear the provisional critic_verdict so the next round will route to critic again
    return {
        "messages": [HumanMessage(content=msg)],
        "critic_verdict": None,
        "critic_rounds": 0,  # reset per attempt; MAX_PLANNER_ITERATIONS bounds the outer loop
    }


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_planner_graph(
    *,
    llm,
    tools: list,
    max_iterations: int = 20,
    enable_critic: bool = True,
    checkpointer=None,
):
    """Compile the planner graph.

    Parameters
    ----------
    llm : BaseChatModel
        LangChain chat model. Typically from ``agents.llm.build_chat_model``.
    tools : list
        LangChain tool objects (from ``agents.tools_lc.select_tools_for_context``).
    max_iterations : int
        Upper bound on the ReAct tool-call budget. Surfaced into the
        system prompt and used for ``recursion_limit``.
    enable_critic : bool
        Whether to run the critic subgraph as a post-ReAct gate.
    """
    system_prompt = _render_system_prompt(max_iterations=max_iterations)

    react_agent = create_react_agent(
        model=llm,
        tools=tools,
        state_schema=AgentState,
        prompt=system_prompt,
    )

    g = StateGraph(AgentState)
    g.add_node("react", react_agent)

    # Stage-1 Summary Agent — always added (cheap, zero-LLM, idempotent).
    # Routes ``end`` from react / critic into ``summarize`` so we capture a
    # draft Episode + auto_findings before exiting the planner subgraph.
    g.add_node("summarize", summarize_node)

    if enable_critic:
        g.add_node("critic", build_critic_graph())
        g.add_node("critic_feedback", critic_feedback_node)

        g.set_entry_point("react")
        g.add_conditional_edges(
            "react",
            _need_critic,
            {"critic": "critic", "react": "react", "end": "summarize"},
        )
        g.add_conditional_edges(
            "critic",
            _critic_passed,
            {"react": "critic_feedback", "end": "summarize"},
        )
        g.add_edge("critic_feedback", "react")
    else:
        g.set_entry_point("react")
        g.add_edge("react", "summarize")

    g.add_edge("summarize", END)

    if checkpointer is not None:
        return g.compile(checkpointer=checkpointer)
    return g.compile()


def build_initial_state_messages(state: AgentState) -> AgentState:
    """Seed ``state.messages`` with the initial user task description.

    This is factored out so the runner / tests can call it before invoking
    the compiled graph without having to construct the full task
    description themselves.
    """
    if not state.get("messages"):
        state["messages"] = [_build_initial_user_message(state)]
    return state


__all__ = [
    "build_planner_graph",
    "build_initial_state_messages",
    "critic_feedback_node",
    "RUN_MODE_HINTS",
]
