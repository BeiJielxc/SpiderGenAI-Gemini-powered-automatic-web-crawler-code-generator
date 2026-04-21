"""Critic subgraph — replaces the hand-written 3-round loop in
``critic_runtime.Critic.evaluate_generated_code_async``.

Design:

* The underlying **algorithms** stay in ``pygen.critic_runtime.Critic``:
  ``StaticCodeValidator``, ``FailureClassifier``, ``_run_rule_round``,
  ``_llm_repair_once``, ``_llm_adjudicate_cause``, etc. These are
  battle-tested pieces we do NOT want to rewrite.

* This module replaces ONLY the top-level orchestration (the imperative
  "round1 -> maybe_repair -> round2 -> maybe_repair -> round3" cascade)
  with an explicit ``StateGraph``:

  ```
     diagnose -> decide -> END (passed)
                         -> repair -> diagnose (failed, rounds<max)
                         -> END (failed, rounds>=max)
  ```

* ``diagnose`` internally covers the 4 legacy stages (static_validate ->
  runtime_execute -> classify_failure -> quality_assessment) via
  ``Critic._run_rule_round``; splitting further would require re-plumbing
  the cause-ranking / minimal-experiment coupling and is not worth the
  risk at this stage. The LangGraph loop boundary is what matters for
  extensibility / checkpointing / multi-agent hand-off.

* The ``Critic`` instance + runtime handles are pulled from
  ``RunnableConfig['configurable']`` so the subgraph keeps zero
  non-serializable state.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

try:
    from critic_runtime import Critic, CriticVerdict
    from failure_classifier import FailureType
except ImportError:  # pragma: no cover - package style fallback
    from ..critic_runtime import Critic, CriticVerdict  # type: ignore
    from ..failure_classifier import FailureType  # type: ignore

from .state import AgentState


# ---------------------------------------------------------------------------
# Configurable keys expected in RunnableConfig['configurable']
# ---------------------------------------------------------------------------


CRITIC_CONFIG_KEY = "critic"
EXECUTOR_SESSION_CONFIG_KEY = "executor_session"
LOG_CALLBACK_CONFIG_KEY = "log_callback"


def _get_configurable(config: RunnableConfig) -> Dict[str, Any]:
    return (config or {}).get("configurable") or {}


def _get_critic(config: RunnableConfig) -> Optional[Critic]:
    return _get_configurable(config).get(CRITIC_CONFIG_KEY)


def _get_executor_session(config: RunnableConfig):
    return _get_configurable(config).get(EXECUTOR_SESSION_CONFIG_KEY)


def _log(config: RunnableConfig, msg: str) -> None:
    cb = _get_configurable(config).get(LOG_CALLBACK_CONFIG_KEY)
    if callable(cb):
        try:
            cb(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def diagnose_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Run one diagnosis round: static check + lightweight runtime +
    failure classification + minimal experiment + quality assessment.

    Wraps ``Critic._run_rule_round`` so all the legacy coupling (cause
    ranking, excluded-cause list for round 3, minimal-experiment probing)
    is preserved verbatim.
    """
    critic = _get_critic(config)
    code = state.get("generated_code") or ""

    if not critic:
        return {
            "critic_verdict": {
                "passed": False,
                "summary": "Critic not configured in runtime context.",
                "confidence": 1.0,
                "issues": [
                    {"severity": "error", "code": "critic_unavailable",
                     "message": "Critic missing from RunnableConfig['configurable']."}
                ],
                "recommendations": [],
                "details": {"stopped_reason": "critic_unavailable"},
            },
            "critic_rounds": state.get("critic_rounds", 0) + 1,
        }

    if not code.strip():
        return {
            "critic_verdict": {
                "passed": False,
                "summary": "Generated code is empty.",
                "confidence": 1.0,
                "issues": [
                    {"severity": "error", "code": "empty_code",
                     "message": "Generated code is empty."}
                ],
                "recommendations": ["Regenerate crawler code before validation."],
                "details": {"stopped_reason": "empty_code"},
            },
            "critic_rounds": state.get("critic_rounds", 0) + 1,
        }

    round_index = int(state.get("critic_rounds", 0)) + 1

    # Round 3 excludes previously seen primary causes to force classifier diversity
    # (matches legacy `excluded = set(_unique_keep_order(used_causes))` behavior).
    excluded: set[str] = set()
    if round_index >= 3:
        for rd in (state.get("critic_evidence") or []):
            if rd.get("step") == "round_summary":
                cause = rd.get("primary_cause")
                if cause:
                    excluded.add(str(cause))

    executor_session = _get_executor_session(config)
    enhanced_analysis = state.get("enhanced_analysis") or {}

    _log(config, f"[CRITIC] Diagnose round {round_index} starting (excluded_causes={list(excluded)})")

    try:
        round_result = await critic._run_rule_round(  # type: ignore[attr-defined]
            code=code,
            run_mode=state.get("run_mode", ""),
            objective=state.get("extra_requirements", ""),
            min_items=1,
            target_url=state.get("url", ""),
            executor_session=executor_session,
            round_index=round_index,
            excluded_primary_causes=excluded,
            run_minimal_experiment=(round_index <= 2),
            enhanced_analysis=enhanced_analysis,
        )
    except Exception as exc:
        _log(config, f"[CRITIC] Diagnose round {round_index} crashed: {exc}")
        return {
            "critic_verdict": {
                "passed": False,
                "summary": f"Critic diagnose crashed: {exc}",
                "confidence": 0.0,
                "issues": [{"severity": "error", "code": "critic_crash", "message": str(exc)}],
                "recommendations": [],
                "details": {"stopped_reason": "diagnose_exception"},
            },
            "critic_rounds": round_index,
        }

    summary_payload = round_result.get("summary_payload") or {}
    # Stamp the round summary into critic_evidence for future rounds to read
    evidence_additions = [
        {"step": "round_summary", **summary_payload}
    ] + list(round_result.get("evidence") or [])

    passed = bool(round_result.get("passed"))
    msg = "PASS" if passed else "FAIL"
    _log(
        config,
        f"[CRITIC] Round {round_index} {msg} "
        f"| records={summary_payload.get('record_count', '?')} "
        f"| cause={summary_payload.get('primary_cause', '?')}",
    )

    # Terminal verdict gets filled in when we decide in the router; here we only
    # write a provisional verdict that captures current state in case the graph
    # ends right after this round.
    provisional = {
        "passed": passed,
        "summary": f"Critic round {round_index} {msg.lower()}.",
        "confidence": float(round_result.get("confidence", 0.5)),
        "issues": [i.__dict__ if hasattr(i, "__dict__") else i for i in (round_result.get("issues") or [])],
        "recommendations": list(round_result.get("recommendations") or []),
        "details": {
            "final_round": round_index,
            "primary_cause": summary_payload.get("primary_cause"),
            "backup_cause": summary_payload.get("backup_cause"),
            "evidence": evidence_additions,
        },
    }

    return {
        "critic_verdict": provisional,
        "critic_rounds": round_index,
        "critic_evidence": evidence_additions,  # appended via operator.add reducer
    }


async def repair_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Ask the LLM to produce a repaired version of the crawler code."""
    critic = _get_critic(config)
    verdict = state.get("critic_verdict") or {}
    details = verdict.get("details") or {}
    round_index = int(state.get("critic_rounds", 1))
    code = state.get("generated_code") or ""

    if not critic or not getattr(critic, "llm_agent", None) or not code.strip():
        _log(config, f"[CRITIC] Repair skipped at round {round_index} (no llm or no code)")
        return {}

    primary = details.get("primary_cause") or FailureType.UNKNOWN.value
    backup = details.get("backup_cause") or FailureType.UNKNOWN.value
    evidence = state.get("critic_evidence") or []
    strategy_hint = (
        "Apply one focused repair for primary cause."
        if round_index <= 2
        else "Final fallback repair, one targeted fix only."
    )

    _log(
        config,
        f"[CRITIC] Repair round {round_index} | primary={primary} | backup={backup}",
    )

    try:
        repaired = await critic._llm_repair_once(  # type: ignore[attr-defined]
            code=code,
            run_mode=state.get("run_mode", ""),
            objective=state.get("extra_requirements", ""),
            primary_cause=primary,
            backup_cause=backup,
            evidence=evidence,
            round_index=round_index,
            strategy_hint=strategy_hint,
        )
    except Exception as exc:
        _log(config, f"[CRITIC] Repair round {round_index} crashed: {exc}")
        return {
            "critic_evidence": [
                {
                    "step": "llm_repair",
                    "round": round_index,
                    "result": {"changed": False, "error": str(exc)},
                }
            ]
        }

    changed = bool(repaired and repaired.strip() and repaired != code)
    evidence_delta = [
        {
            "step": "llm_repair",
            "round": round_index,
            "result": {"changed": changed},
        }
    ]

    updates: Dict[str, Any] = {"critic_evidence": evidence_delta}
    if changed:
        updates["generated_code"] = repaired
        updates["critic_repaired_code"] = repaired
        _log(config, f"[CRITIC] Repair produced new code ({len(repaired)} chars)")
    else:
        _log(config, "[CRITIC] Repair produced no usable change")

    return updates


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


MAX_CRITIC_ROUNDS = 3


def critic_router(state: AgentState) -> str:
    """Decide what happens after diagnose completes."""
    verdict = state.get("critic_verdict") or {}
    if verdict.get("passed"):
        return "passed"
    rounds = int(state.get("critic_rounds", 0))
    if rounds >= MAX_CRITIC_ROUNDS:
        return "exhausted"
    return "repair"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_critic_graph():
    """Compile the critic subgraph.

    Returned graph takes the full ``AgentState`` as its state schema and
    exposes these external effects:

    * writes ``critic_verdict`` (final pass/fail verdict on exit)
    * may mutate ``generated_code`` if repair produced a new version
    * appends to ``critic_evidence`` and increments ``critic_rounds``
    """
    g = StateGraph(AgentState)
    g.add_node("diagnose", diagnose_node)
    g.add_node("repair", repair_node)

    g.set_entry_point("diagnose")
    g.add_conditional_edges(
        "diagnose",
        critic_router,
        {
            "passed": END,
            "exhausted": END,
            "repair": "repair",
        },
    )
    g.add_edge("repair", "diagnose")

    return g.compile()


def finalize_verdict_from_state(state: AgentState) -> CriticVerdict:
    """Convert the critic subgraph's final ``AgentState`` back into the
    legacy ``CriticVerdict`` dataclass so api.py / downstream code that
    still speaks the old shape keeps working unchanged."""
    from critic_runtime import CriticIssue  # local import to avoid cycle

    raw = state.get("critic_verdict") or {}
    issues = [
        CriticIssue(
            severity=str(i.get("severity", "error")),
            code=str(i.get("code", "")),
            message=str(i.get("message", "")),
        )
        for i in (raw.get("issues") or [])
    ]
    return CriticVerdict(
        passed=bool(raw.get("passed", False)),
        summary=str(raw.get("summary", "")),
        confidence=float(raw.get("confidence", 0.0)),
        issues=issues,
        recommendations=list(raw.get("recommendations") or []),
        details=dict(raw.get("details") or {}),
    )


__all__ = [
    "build_critic_graph",
    "diagnose_node",
    "repair_node",
    "critic_router",
    "finalize_verdict_from_state",
    "MAX_CRITIC_ROUNDS",
]
