"""Supervisor scaffold — placeholder for future multi-agent routing.

Today the supervisor is a trivial pass-through that routes every task to
the single ``planner`` worker. The scaffold exists so that when we later
want to introduce specialist agents (date-api-specialist,
menu-tree-specialist, selector-specialist, …) we only have to:

1. Build each specialist as its own ``create_react_agent`` with a subset
   of ``agents.tools_lc`` tools.
2. Register it in the ``AGENT_REGISTRY`` below.
3. Replace ``_dummy_router`` with an LLM-driven (or rule-based) router
   that picks the appropriate specialist for the current task / state.

The state schema already uses ``AgentState`` which is shared across all
agents, so hand-offs happen naturally through the ``messages`` channel
plus mirrored task-context fields.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from langgraph.graph import END, StateGraph

from .state import AgentState


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


AGENT_REGISTRY: Dict[str, Callable[..., Any]] = {}


def register_agent(name: str, factory: Callable[..., Any]) -> None:
    """Register a compiled-graph factory under ``name``.

    ``factory`` should accept keyword arguments (``llm``, ``tools``,
    ``max_iterations``, …) and return a compiled LangGraph.
    """
    AGENT_REGISTRY[name] = factory


# ---------------------------------------------------------------------------
# Router (stub)
# ---------------------------------------------------------------------------


def _dummy_router(state: AgentState) -> str:
    """Current single-worker router: everything goes to ``planner``."""
    return "planner"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_supervisor_graph(
    *,
    planner_graph,
    extra_workers: Optional[Dict[str, Any]] = None,
):
    """Build a supervisor graph that currently delegates everything to the
    passed-in ``planner_graph``.

    ``extra_workers`` is reserved for future specialists; when we add
    them, update ``_dummy_router`` to return their names and wire them in
    as additional nodes.
    """
    g = StateGraph(AgentState)
    g.add_node("planner", planner_graph)

    workers = extra_workers or {}
    for name, worker in workers.items():
        if name == "planner":
            continue
        g.add_node(name, worker)

    g.set_conditional_entry_point(
        _dummy_router,
        {name: name for name in {"planner", *workers.keys()}},
    )

    g.add_edge("planner", END)
    for name in workers:
        g.add_edge(name, END)

    return g.compile()


__all__ = [
    "AGENT_REGISTRY",
    "register_agent",
    "build_supervisor_graph",
]
