"""Runner — the thin entry point that ``api.py`` calls to run the
LangGraph-based agent.

Responsibilities:

1. Build or accept the runtime singletons (``ArtifactStore``,
   ``Critic``, ``ExecutorSession``) and a shared ``ToolContext``.
2. Construct the LLM (through ``agents.llm.build_chat_model``).
3. Compile the evidence-driven specialist supervisor and ``ainvoke``
   it with the proper ``RunnableConfig['configurable']`` so every node
   can reach the runtime handles.
4. Translate the graph's final ``AgentState`` back into a legacy
   ``PlannerResult``-compatible object so ``api.py`` / downstream code
   needs no changes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage

try:
    from artifact_store import ArtifactStore
    from critic_runtime import Critic
    from executor_session import ExecutorSession
    from tools import ToolContext
except ImportError:  # pragma: no cover
    from ..artifact_store import ArtifactStore  # type: ignore
    from ..critic_runtime import Critic  # type: ignore
    from ..executor_session import ExecutorSession  # type: ignore
    from ..tools import ToolContext  # type: ignore

try:
    from memory import (  # type: ignore
        MemoryStore,
        compute_list_page_fingerprint,
        find_recent_task_id_for_domain,
        render_feedback_replay_hint,
        render_site_memory_hint,
        walk_rerun_chain,
    )
    from memory.episode import domain_of  # type: ignore
    from memory.site_profile import apply_time_decay  # type: ignore
except ImportError:  # pragma: no cover
    from ..memory import (
        MemoryStore,
        compute_list_page_fingerprint,
        find_recent_task_id_for_domain,
        render_feedback_replay_hint,
        render_site_memory_hint,
        walk_rerun_chain,
    )
    from ..memory.episode import domain_of
    from ..memory.site_profile import apply_time_decay

from .result import PlannerResult

from .critic_graph import finalize_verdict_from_state
from .llm import build_chat_model
from .planner_graph import build_initial_state_messages
from .rerun_validate import (
    pre_validate_rerun_selectors,
    render_pre_validation_hint,
)
from .state import AgentState, initial_state
from .summarize_node import MEMORY_STORE_CONFIG_KEY, STEP_CALLBACK_CONFIG_KEY
from .supervisor import build_supervisor_graph
from .tools_lc import (
    CANCEL_CHECK_CONFIG_KEY,
    CODEGEN_GRAPH_CONFIG_KEY,
    TOOL_CONTEXT_CONFIG_KEY,
)
from .codegen_graph import LLM_AGENT_CONFIG_KEY, PYGEN_CONFIG_KEY
from .critic_graph import (
    CRITIC_CONFIG_KEY,
    EXECUTOR_SESSION_CONFIG_KEY,
    LOG_CALLBACK_CONFIG_KEY,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_agent(
    *,
    browser,
    config,
    llm_agent,
    url: str,
    run_mode: str,
    start_date: str,
    end_date: str,
    extra_requirements: str = "",
    task_id: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
    attachments: Optional[List[Any]] = None,
    max_iterations: int = 20,
    cancel_check: Optional[Callable[[], bool]] = None,
    artifact_store: Optional[ArtifactStore] = None,
    critic: Optional[Critic] = None,
    executor_session: Optional[ExecutorSession] = None,
    enable_critic: bool = True,
    enable_codegen_graph: bool = True,
    memory_store: Optional[MemoryStore] = None,
    prev_task_id: Optional[str] = None,
    step_callback: Optional[Callable[[int, str], None]] = None,
    reusable_script_code: Optional[str] = None,
    task_signature: Optional[str] = None,
    golden_code_path: Optional[str] = None,
) -> PlannerResult:
    """Run the LangGraph-based agent and return a ``PlannerResult``.

    Single entry point used by ``api.py`` for autonomous crawler
    generation.
    """
    log = log_callback or (lambda _msg: None)
    _cancel = cancel_check or (lambda: False)

    # An active golden crawler is already user-approved executable code.  This
    # branch must stay ahead of model construction, memory lookup and graph
    # compilation so an exact-signature replay never depends on an LLM.
    if isinstance(reusable_script_code, str) and reusable_script_code.strip():
        result = PlannerResult()
        result.success = True
        result.script_code = reusable_script_code
        result.strategy_summary = "golden_replay"
        result.final_state = {
            "url": url,
            "run_mode": run_mode,
            "start_date": start_date,
            "end_date": end_date,
            "extra_requirements": extra_requirements,
            "task_id": task_id,
            "prev_task_id": prev_task_id,
            "generated_code": reusable_script_code,
            "code_strategy": "golden_replay",
            "started_at": time.time(),
            "task_signature": task_signature,
            "execution_source": "golden_replay",
            "golden_code_path": golden_code_path,
            "golden_status": "active",
            "stage_evidence": {},
            "validation_reports": [],
            "repair_history": [],
        }
        log("[GOLDEN] 已加载人工确认的黄金爬虫，跳过 LLM 与专家图")
        return result

    owns_executor = False
    owns_artifact_store = False

    try:
        # --- Default artifact store ---------------------------------------
        if artifact_store is None:
            artifact_root = Path(__file__).parent.parent / "output" / "artifacts"
            per_task_subdir = bool(getattr(config, "artifacts_per_task_subdir", True))
            artifact_store = ArtifactStore(
                artifact_root,
                per_task_subdir=per_task_subdir,
            )
            owns_artifact_store = True

            # TTL-based cleanup runs once per agent invocation. Cheap mtime
            # walk; failures are swallowed inside cleanup_expired().
            ttl_days = int(getattr(config, "artifacts_ttl_days", 7) or 0)
            if ttl_days > 0:
                try:
                    removed = artifact_store.cleanup_expired(ttl_days * 86400)
                    if removed:
                        log(f"[ARTIFACTS] TTL cleanup removed {removed} files (>{ttl_days}d old)")
                except Exception as exc:
                    log(f"[ARTIFACTS] TTL cleanup failed (non-fatal): {exc}")

        # --- Default memory store + read path (site profile + rerun hint) ----
        site_memory_hint = None
        feedback_replay_hint = None
        owns_memory_store = False
        if memory_store is None and getattr(config, "memory_enabled", True):
            try:
                memory_store = MemoryStore(
                    config.memory_root,
                    log_callback=log,
                    max_episodes=getattr(config, "memory_max_keep", 1000),
                )
                owns_memory_store = True
                # Run the pending-draft GC opportunistically (cheap mtime walk)
                gc_days = int(getattr(config, "memory_pending_gc_days", 7))
                if gc_days > 0:
                    try:
                        memory_store.gc_pending(older_than_days=gc_days)
                    except Exception as exc:
                        log(f"[MEMORY] gc_pending failed (non-fatal): {exc}")
            except Exception as exc:
                log(f"[MEMORY] failed to initialise MemoryStore (memory disabled for this run): {exc}")
                memory_store = None

        if memory_store is not None and getattr(config, "memory_inject_into_planner", True):
            try:
                domain = domain_of(url)
                if domain:
                    profile = memory_store.lookup_site(domain)
                    if profile is not None:
                        # Decay confidence by elapsed time before deciding to inject.
                        profile = apply_time_decay(
                            profile,
                            decay_per_30d=float(getattr(config, "memory_confidence_decay_per_30d", 0.1)),
                        )
                        rendered = render_site_memory_hint(
                            profile,
                            min_confidence=float(getattr(config, "memory_min_inject_confidence", 0.3)),
                            blacklist_min_losses=int(getattr(config, "memory_blacklist_min_losses", 2)),
                            blacklist_max_winrate=float(getattr(config, "memory_blacklist_max_winrate", 0.2)),
                            blacklist_require_consecutive=bool(
                                getattr(config, "memory_blacklist_require_consecutive", True)
                            ),
                        )
                        if rendered:
                            site_memory_hint = rendered
                            log(f"[MEMORY] site profile injected (domain={domain}, confidence={profile.get('confidence', 0):.2f})")
            except Exception as exc:
                log(f"[MEMORY] site-hint injection failed (non-fatal): {exc}")

        # Hole-2.A — robust feedback-replay lookup with domain guard +
        # domain-based auto-fallback. Even when the caller forgot to pass
        # ``prev_task_id`` (typical batch / fresh-form path) we still try
        # to inherit the most recent same-site episode. And when the caller
        # passed an id that points at a *different* domain (batch queue
        # smuggling cross-site lineage) we silently discard it instead of
        # poisoning the prompt.
        if memory_store is not None:
            try:
                current_domain = domain_of(url) or ""
            except Exception:
                current_domain = ""
            try:
                max_hops = int(getattr(config, "memory_feedback_replay_hops", 3))
            except Exception:
                max_hops = 3
            domain_fallback_enabled = bool(
                getattr(config, "memory_feedback_replay_domain_fallback", True)
            )
            try:
                fallback_age_days = float(
                    getattr(config, "memory_feedback_replay_fallback_age_days", 14.0)
                )
            except (TypeError, ValueError):
                fallback_age_days = 14.0

            effective_prev_task_id: Optional[str] = prev_task_id or None
            replay_source = "explicit_prev_task_id"

            # ---- Domain-guard the explicit prev_task_id (Hole 2.A.guard) ----
            if effective_prev_task_id and current_domain:
                try:
                    head = walk_rerun_chain(
                        memory_store,
                        effective_prev_task_id,
                        max_hops=1,
                    )
                    if head:
                        head_domain = str(head[0].get("domain") or "").strip().lower()
                        if head_domain and head_domain != current_domain:
                            log(
                                f"[MEMORY] prev_task_id={effective_prev_task_id[:8]} points at "
                                f"domain={head_domain!r} (current={current_domain!r}) — "
                                f"discarding to avoid cross-domain prompt pollution"
                            )
                            effective_prev_task_id = None
                            replay_source = "discarded_cross_domain"
                except Exception as exc:
                    log(f"[MEMORY] prev_task_id domain check failed (non-fatal): {exc}")

            # ---- Domain-fallback when nothing usable was passed (Hole 2.A.fallback) ----
            if not effective_prev_task_id and current_domain and domain_fallback_enabled:
                try:
                    candidate = find_recent_task_id_for_domain(
                        memory_store,
                        current_domain,
                        max_age_days=fallback_age_days,
                        log=log,
                    )
                    if candidate:
                        effective_prev_task_id = candidate
                        replay_source = "domain_fallback"
                        log(
                            f"[MEMORY] no prev_task_id from caller; auto-using "
                            f"most-recent same-domain task_id={candidate[:8]} "
                            f"(domain={current_domain})"
                        )
                except Exception as exc:
                    log(f"[MEMORY] domain-fallback lookup failed (non-fatal): {exc}")

            # ---- Walk the chain (with domain guard) and render -----------
            if effective_prev_task_id:
                try:
                    chain = walk_rerun_chain(
                        memory_store,
                        effective_prev_task_id,
                        max_hops=max_hops,
                        expected_domain=current_domain or None,
                    )
                    if chain:
                        rendered = render_feedback_replay_hint(chain)
                        if rendered:
                            feedback_replay_hint = rendered
                            chain_ids = ", ".join(
                                (ep.get("task_id") or "?")[:8] for ep in chain
                            )
                            log(
                                f"[MEMORY] feedback_replay_hint injected "
                                f"(source={replay_source}, hops={len(chain)}, "
                                f"max_hops={max_hops}, chain=[{chain_ids}])"
                            )

                        # ---- Module B: rerun pre-validation ------------
                        # Use the cached HTML (Module A) to pre-check the
                        # previous run's selector guesses BEFORE the LLM
                        # starts. The result is prepended to
                        # ``feedback_replay_hint`` so the planner sees a
                        # concrete "✅ try these / ❌ skip these" list at
                        # the very top of its prompt.
                        if bool(getattr(config, "rerun_pre_validate_enabled", True)):
                            try:
                                from page_cache import get_default_page_cache
                                cache = get_default_page_cache()
                                if cache is not None:
                                    max_sel = int(
                                        getattr(config, "rerun_pre_validate_max_selectors", 5)
                                    )
                                    report = pre_validate_rerun_selectors(
                                        url=url,
                                        chain=chain,
                                        page_cache=cache,
                                        max_selectors=max_sel,
                                        log=log,
                                    )
                                    pre_block = render_pre_validation_hint(report)
                                    if pre_block:
                                        feedback_replay_hint = (
                                            (pre_block + "\n\n" + (feedback_replay_hint or "")).strip()
                                        )
                                        log(
                                            "[MEMORY] rerun pre-validation block injected "
                                            f"(✅ {len(report.pre_validated)} / "
                                            f"❌ {len(report.disproved)} / "
                                            f"⏭ {len(report.skipped)})"
                                        )
                            except Exception as exc:
                                log(
                                    f"[MEMORY] rerun pre-validation failed "
                                    f"(non-fatal): {exc}"
                                )
                    else:
                        log(
                            f"[MEMORY] no replayable episode found for "
                            f"task_id={effective_prev_task_id[:8]} "
                            f"(source={replay_source}; skipping replay hint)"
                        )
                except Exception as exc:
                    log(f"[MEMORY] feedback-hint injection failed (non-fatal): {exc}")
            elif prev_task_id and not current_domain:
                log(
                    "[MEMORY] prev_task_id provided but current URL has no parseable "
                    "domain — skipping replay hint"
                )

        # --- Default critic ----------------------------------------------
        if critic is None:
            critic = Critic(llm_agent=llm_agent, artifact_store=artifact_store, max_retries=3)

        # --- Default executor session ------------------------------------
        if executor_session is None:
            sandbox_root = Path(__file__).parent.parent / "sandbox_sessions" / (task_id or "default")
            executor_session = ExecutorSession(
                workdir=sandbox_root,
                auto_start=getattr(config, "sandbox_auto_start", True),
                persistent=getattr(config, "sandbox_persistent_session", True),
                backend=getattr(config, "sandbox_backend", "docker"),
                docker_image=getattr(config, "sandbox_docker_image", None),
                docker_auto_pull=getattr(config, "sandbox_docker_auto_pull", True),
                docker_disable_network=getattr(config, "sandbox_docker_disable_network", False),
                docker_mount_workdir=getattr(config, "sandbox_docker_mount_workdir", True),
            )
            owns_executor = True

        # --- Build shared ToolContext ------------------------------------
        ctx = ToolContext(
            browser=browser,
            config=config,
            llm_agent=llm_agent,
            url=url,
            run_mode=run_mode,
            start_date=start_date,
            end_date=end_date,
            extra_requirements=extra_requirements,
            task_id=task_id,
            log_callback=log,
            attachments=attachments,
            artifact_store=artifact_store,
            executor_session=executor_session,
            critic=critic,
        )

        # --- Build LLM + evidence-driven specialist graph ---------------
        chat_model = build_chat_model(config, temperature=0.2)
        graph = build_supervisor_graph(
            llm=chat_model,
            has_executor=executor_session is not None,
        )

        # --- Initial state ----------------------------------------------
        state = initial_state(
            url=url,
            run_mode=run_mode,
            start_date=start_date,
            end_date=end_date,
            extra_requirements=extra_requirements,
            task_id=task_id,
            site_memory_hint=site_memory_hint,
            feedback_replay_hint=feedback_replay_hint,
            prev_task_id=prev_task_id,
            started_at=time.time(),
        )
        state = build_initial_state_messages(state)

        # --- RunnableConfig --------------------------------------------
        runnable_config = {
            "configurable": {
                TOOL_CONTEXT_CONFIG_KEY: ctx,
                CANCEL_CHECK_CONFIG_KEY: _cancel,
                CRITIC_CONFIG_KEY: critic,
                EXECUTOR_SESSION_CONFIG_KEY: executor_session,
                LOG_CALLBACK_CONFIG_KEY: log,
                LLM_AGENT_CONFIG_KEY: llm_agent,
                PYGEN_CONFIG_KEY: config,
                CODEGEN_GRAPH_CONFIG_KEY: enable_codegen_graph,
                MEMORY_STORE_CONFIG_KEY: memory_store,
                STEP_CALLBACK_CONFIG_KEY: step_callback,
            },
            # recursion_limit bounds the supervisor-level step count. Each
            # ReAct iteration can internally produce 2 steps (AIMessage +
            # ToolMessage) so we multiply by 3 for generous headroom, plus
            # a few for critic/feedback transitions.
            "recursion_limit": max(120, max_iterations * 8 + 40),
        }

        log(f"[LANGGRAPH] Starting agent (model={getattr(config, 'qwen_model', '?')}, "
            f"max_iter={max_iterations}, architecture=evidence-specialists)")

        try:
            final_state = await graph.ainvoke(state, config=runnable_config)
        except asyncio.CancelledError:
            log("[LANGGRAPH] Task was cancelled by runtime")
            return _build_result_from_state(
                state, ctx, error="Task cancelled by runtime"
            )
        except Exception as exc:
            import traceback
            log(f"[LANGGRAPH] Graph invocation failed: {exc}")
            log(traceback.format_exc())
            return _build_result_from_state(
                state, ctx, error=f"Graph invocation failed: {exc}"
            )

        if not final_state.get("html_fingerprint") and getattr(ctx, "page_html", None):
            final_state["html_fingerprint"] = compute_list_page_fingerprint(ctx.page_html)

        return _build_result_from_state(final_state, ctx, error=None)

    finally:
        if owns_executor and executor_session is not None:
            try:
                await executor_session.close(force=True)
            except Exception:
                pass
        # ArtifactStore / MemoryStore have no close() today; leave to GC.
        _ = owns_artifact_store
        _ = owns_memory_store


# ---------------------------------------------------------------------------
# State -> PlannerResult translation
# ---------------------------------------------------------------------------


def _build_result_from_state(
    state: AgentState,
    ctx: ToolContext,
    error: Optional[str],
) -> PlannerResult:
    """Adapt the graph's final state to the exact shape the legacy code expects."""
    result = PlannerResult()
    result.tool_calls = list(state.get("tool_calls_log") or [])
    result.iterations = int(state.get("iterations", len(result.tool_calls)))
    if not result.iterations and result.tool_calls:
        result.iterations = len(result.tool_calls)
    result.enhanced_analysis = dict(state.get("enhanced_analysis") or ctx.enhanced_analysis or {})
    result.verified_mapping = state.get("verified_mapping") or ctx.verified_mapping
    result.verified_selectors = state.get("verified_selectors") or ctx.verified_selectors
    result.auto_findings = state.get("auto_findings")
    result.summary_draft_path = state.get("summary_draft_path")
    result.html_fingerprint = state.get("html_fingerprint")
    result.stage_evidence = dict(state.get("stage_evidence") or {})
    result.validation_reports = list(state.get("validation_reports") or [])
    result.attribution_decision = state.get("attribution_decision")
    result.repair_history = list(state.get("repair_history") or [])
    result.final_state = dict(state)

    # Prefer the (possibly critic-repaired) generated_code on state; fall
    # back to ctx for the rare pathological case where the mirror was
    # skipped.
    script_code = state.get("generated_code") or ctx.generated_code or ""
    result.script_code = script_code or None
    result.strategy_summary = state.get("code_strategy") or ctx.code_generation_strategy or ""

    critic_raw = state.get("critic_verdict") or {}
    if error:
        result.success = False
        result.error = error
    elif not script_code.strip():
        result.success = False
        result.error = state.get("last_error") or "Agent did not produce any crawler code."
    elif critic_raw and not critic_raw.get("passed", False):
        # Match legacy behavior: if critic rejects, report the summary.
        verdict = finalize_verdict_from_state(state)
        result.success = False
        result.error = verdict.summary or "Critic rejected the generated code."
    else:
        result.success = True
        result.error = None

    # Attach a short strategy summary if the model produced a final
    # AI message (matches the behavior of legacy's free-text summary).
    if not result.strategy_summary:
        last_ai: Optional[AIMessage] = None
        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, AIMessage):
                last_ai = msg
                break
        if last_ai and isinstance(last_ai.content, str):
            result.strategy_summary = last_ai.content[:400]

    return result


__all__ = ["run_agent"]
