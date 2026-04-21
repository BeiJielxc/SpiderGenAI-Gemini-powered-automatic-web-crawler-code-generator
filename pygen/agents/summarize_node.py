"""Stage-1 Summary Agent — runs at the END of the planner graph.

Responsibilities (zero LLM calls)
---------------------------------
1. Extract facts from the final ``AgentState`` (``extract_facts_from_state``).
2. Compute the list-page HTML fingerprint from ``ctx.page_html``.
3. Run heuristic ``auto_findings`` scans (redundant tool calls /
   suspected silent failures / duplicate code blocks).
4. Persist a draft Episode to ``episode/pending/<task_id>.json``.
5. Mirror ``auto_findings`` and ``summary_draft_path`` back into the
   ``AgentState`` so the API layer / frontend can surface them.

The LLM-based ``lessons`` block is deliberately deferred to the user
feedback flow (see :mod:`pygen.memory.commit`). The user's plain-English
suggestion ("只爬到一堆图标") is a *required* input for translating
business-language complaints into technical root causes — calling the
LLM here would just have it guess from the model's own evidence.

This node is wired in by :func:`pygen.agents.planner_graph.build_planner_graph`
when ``memory.summary_agent.enable_auto_findings`` is on. It must
*never raise*: any exception is logged and the run still returns its
PlannerResult to the caller.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from langchain_core.runnables import RunnableConfig

try:
    from memory import (  # type: ignore
        MemoryStore,
        compute_list_page_fingerprint,
        new_draft_episode,
        run_auto_findings,
    )
except ImportError:  # pragma: no cover - package-style fallback
    from ..memory import (
        MemoryStore,
        compute_list_page_fingerprint,
        new_draft_episode,
        run_auto_findings,
    )

from .state import AgentState
from .critic_graph import (
    CRITIC_CONFIG_KEY,
    EXECUTOR_SESSION_CONFIG_KEY,
    LOG_CALLBACK_CONFIG_KEY,
)
from .codegen_graph import PYGEN_CONFIG_KEY
from .tools_lc import TOOL_CONTEXT_CONFIG_KEY


# Configurable key under which we expose the MemoryStore to the node.
# Defaulted in the runner so callers don't have to wire it manually.
MEMORY_STORE_CONFIG_KEY = "memory_store"

# Configurable key for the optional ``step_callback(step:int, label:str)``.
# api.py wires this to ``_update_step`` so the side-bar progress UI can
# advance to "🧠 Agent 正在自我复盘" the moment this node actually starts
# executing (not before, not after). Without this, the summary phase runs
# silently inside planner_graph and the user only sees "task done".
STEP_CALLBACK_CONFIG_KEY = "step_callback"
# Position 12 = "🧠 Agent 正在自我复盘 (Stage-1)" in frontend/ExecutionView.tsx
# STEPS_TEMPLATE. We sit between "📊 正在验证爬取结果" (11) and "🎉 任务完成" (13)
# because summarize_node is the LAST node of planner_graph — it runs AFTER
# critic + code execution + result verification, not before. Keep this index
# in sync with the frontend template, otherwise the UI will highlight the
# wrong row.
SUMMARIZE_STEP_INDEX = 12
SUMMARIZE_STEP_LABEL = "🧠 Agent 正在自我复盘 (Stage-1)"


# ---------------------------------------------------------------------------
# Node body
# ---------------------------------------------------------------------------


def summarize_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """LangGraph node that writes a Stage-1 draft episode and returns
    state updates so subsequent reads can see the auto_findings.

    Always returns a dict (possibly empty). Never raises.
    """
    configurable = (config or {}).get("configurable") or {}
    log: Callable[[str], None] = configurable.get(LOG_CALLBACK_CONFIG_KEY) or (lambda _m: None)
    step_cb: Optional[Callable[[int, str], None]] = configurable.get(STEP_CALLBACK_CONFIG_KEY)
    pygen_config = configurable.get(PYGEN_CONFIG_KEY)
    store: Optional[MemoryStore] = configurable.get(MEMORY_STORE_CONFIG_KEY)
    ctx = configurable.get(TOOL_CONTEXT_CONFIG_KEY)

    # Honor the kill switch even if a store was wired in.
    if pygen_config is not None and not _flag(pygen_config, "memory_enabled", True):
        log("[SUMMARIZE] memory disabled (memory_enabled=false) — skipping self-review")
        return {}

    # Advance the side-bar UI as soon as we know we're going to run; the
    # callback is best-effort, never raises.
    if step_cb is not None:
        try:
            step_cb(SUMMARIZE_STEP_INDEX, SUMMARIZE_STEP_LABEL)
        except Exception as exc:
            log(f"[SUMMARIZE] step_callback raised (ignored): {exc}")

    log("[SUMMARIZE] 开始自我复盘 (Stage-1: 零 LLM 启发式扫描 + draft 落盘)")

    if store is None:
        # Memory disabled — just compute auto_findings into state for the
        # frontend to display, but skip persistence.
        try:
            findings = run_auto_findings(state, ctx=ctx)
            log(
                "[SUMMARIZE] auto_findings(no-store): "
                f"redundant_tool_calls={len(findings.get('redundant_tool_calls') or [])} "
                f"suspected_failures={len(findings.get('suspected_failures') or [])} "
                f"redundant_code_blocks={len(findings.get('redundant_code_blocks') or [])}"
            )
            return {"auto_findings": findings}
        except Exception as exc:
            log(f"[SUMMARIZE] auto_findings failed (no store): {exc}")
            return {}

    try:
        started_at = state.get("started_at") if isinstance(state, dict) else None

        # ---- Heuristic auto_findings (zero LLM) ----
        try:
            findings = run_auto_findings(state, ctx=ctx)
        except Exception as exc:
            log(f"[SUMMARIZE] auto_findings raised: {exc}")
            findings = {
                "redundant_tool_calls": [],
                "suspected_failures": [],
                "redundant_code_blocks": [],
            }
        log(
            "[SUMMARIZE] auto_findings: "
            f"redundant_tool_calls={len(findings.get('redundant_tool_calls') or [])} "
            f"suspected_failures={len(findings.get('suspected_failures') or [])} "
            f"redundant_code_blocks={len(findings.get('redundant_code_blocks') or [])}"
        )

        # ---- HTML fingerprint of the list page (best-effort) ----
        fingerprint = None
        try:
            page_html = getattr(ctx, "page_html", None) if ctx is not None else None
            fingerprint = compute_list_page_fingerprint(page_html)
        except Exception as exc:
            log(f"[SUMMARIZE] fingerprint failed: {exc}")
        log(f"[SUMMARIZE] 列表页指纹: {fingerprint or 'n/a (无 page_html)'}")

        # ---- Decide auto_outcome (display-only; doesn't touch profile) ----
        critic = state.get("critic_verdict") or {}
        if critic and critic.get("passed"):
            if findings.get("suspected_failures"):
                auto_outcome = "partial"
            else:
                auto_outcome = "success"
        elif state.get("generated_code"):
            auto_outcome = "partial" if not critic else "failure"
        else:
            auto_outcome = "failure"
        log(
            f"[SUMMARIZE] auto_outcome={auto_outcome} "
            f"(critic_passed={bool(critic.get('passed')) if critic else None}, "
            f"has_code={bool(state.get('generated_code'))})"
        )

        # ---- Build the draft Episode and write to disk ----
        draft = new_draft_episode(
            state,
            ctx=ctx,
            started_at=started_at,
            auto_outcome=auto_outcome,
        )
        draft["html_fingerprint"] = fingerprint
        draft["auto_findings"] = findings
        if state.get("prev_task_id"):
            draft["rerun_of"] = str(state.get("prev_task_id"))

        path = store.write_draft(draft)
        if path is not None:
            log(
                f"[SUMMARIZE] draft 已落盘: {path.name} "
                f"(等待用户评价后触发 Stage-2 LLM 复盘)"
            )
        else:
            log("[SUMMARIZE] draft episode write returned None (see prior errors)")

        return {
            "auto_findings": findings,
            "summary_draft_path": str(path) if path else None,
            "html_fingerprint": fingerprint,
        }
    except Exception as exc:  # pragma: no cover - defensive
        import traceback
        log(f"[SUMMARIZE] node failed (non-fatal): {exc}\n{traceback.format_exc()}")
        return {}


def _flag(config: Any, attr: str, default: bool) -> bool:
    if config is None:
        return default
    v = getattr(config, attr, None)
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(v)


__all__ = [
    "MEMORY_STORE_CONFIG_KEY",
    "STEP_CALLBACK_CONFIG_KEY",
    "SUMMARIZE_STEP_INDEX",
    "SUMMARIZE_STEP_LABEL",
    "summarize_node",
]
