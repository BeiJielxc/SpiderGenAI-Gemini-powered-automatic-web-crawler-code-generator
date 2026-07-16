"""Write Stage-1 memory only after the real task-isolated runtime gate."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .auto_findings import run_auto_findings
from .episode import new_draft_episode
from .store import MemoryStore


def _runtime_attribution(state: Dict[str, Any], runtime_report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if bool(runtime_report.get("success")) and int(runtime_report.get("record_count") or 0) > 0:
        return state.get("attribution_decision")

    counts = runtime_report.get("stage_counts") or {}
    runtime_count = int(counts.get("runtime_records") or 0)
    final_count = int(counts.get("final_records") or runtime_report.get("record_count") or 0)
    error_text = str(runtime_report.get("error") or "").lower()
    if (
        "no usable api or selector bundle" in error_text
        or "no usable api, selector bundle" in error_text
        or "acquisition_evidence_missing" in error_text
    ):
        target, failure_type, confidence = "selector", "acquisition_evidence_missing", 0.99
        reason = "The acquisition stage ended without replayable API, selector, or DOM evidence."
    elif "page is not accessible or observable" in error_text or "site_unreachable" in error_text:
        target, failure_type, confidence = "site_profiler", "site_unreachable", 0.99
        reason = "The requested page could not be restored and observed by the Site Profile stage."
    elif "date range" in error_text and ("invalid" in error_text or "well-formed" in error_text):
        target, failure_type, confidence = "date_scope", "date_strategy_unverified", 0.95
        reason = "The Date and Scope stage rejected the requested date range or strategy."
    elif "did not produce any crawler code" in error_text or "empty code" in error_text:
        target, failure_type, confidence = "codegen", "empty_code", 0.99
        reason = "The Codegen stage completed without producing a crawler script."
    elif "resource_exhausted" in error_text or "quota" in error_text or "429" in error_text:
        target, failure_type, confidence = "codegen", "llm_quota_exceeded", 0.99
        reason = "The code-generation model reported a quota or rate-limit failure."
    elif "no task-owned json output" in error_text or "absolute/shared path" in error_text:
        target, failure_type, confidence = "codegen", "output_path_escape", 0.99
        reason = (
            "The crawler completed but wrote JSON outside the task-owned runtime "
            "directory; the acquisition evidence is not at fault."
        )
    elif runtime_count > 0 and final_count == 0:
        target, failure_type, confidence = "date_scope", "date_filter_too_strict", 0.95
        reason = "Runtime produced records, but normalization/date filtering removed all of them."
    elif runtime_report.get("timed_out"):
        target, failure_type, confidence = "codegen", "timeout", 0.9
        reason = "The generated crawler exceeded the runtime timeout."
    elif int(runtime_report.get("exit_code") or 0) != 0:
        target, failure_type, confidence = "codegen", "runtime_exception", 0.9
        reason = "The generated crawler exited with a non-zero status."
    elif not runtime_report.get("schema_valid"):
        acquisition = (state.get("stage_evidence") or {}).get("acquisition") or {}
        selected_id = acquisition.get("selected_candidate_id")
        selected = next(
            (item for item in acquisition.get("candidates") or []
             if item.get("candidate_id") == selected_id),
            {},
        )
        kind = selected.get("kind")
        if kind == "api_endpoint":
            target, failure_type = "api_discovery", "api_schema_mismatch"
        elif kind in {"selector_bundle", "dom_structure_fallback"}:
            target, failure_type = "selector", "selector_mismatch"
        else:
            target, failure_type = "codegen", "schema_mismatch"
        confidence = 0.72
        reason = "The selected acquisition candidate produced no output matching the required schema."
    else:
        target, failure_type, confidence = "codegen", "zero_usable_records", 0.55
        reason = "Execution completed but no usable records survived the final output gate."

    return {
        "failure_type": failure_type,
        "suspected_stages": [{
            "stage": target,
            "probability": confidence,
            "reason": reason,
        }],
        "rollback_target": target,
        "retry_candidate_id": None,
        "confidence": confidence,
        "reason": reason,
        "applies_to": "next_rerun",
    }


def finalize_runtime_episode(
    *,
    planner_result: Any,
    runtime_report: Dict[str, Any],
    final_output: Dict[str, Any],
    config: Any,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    log = log_callback or (lambda _message: None)
    if not getattr(config, "memory_enabled", True):
        return {"auto_findings": None, "summary_draft_path": None, "html_fingerprint": None}

    state = dict(getattr(planner_result, "final_state", {}) or {})
    state["runtime_report"] = dict(runtime_report or {})
    state["final_output"] = dict(final_output or {})
    state["generated_code"] = getattr(planner_result, "script_code", None) or state.get("generated_code")
    state["stage_evidence"] = getattr(planner_result, "stage_evidence", {}) or state.get("stage_evidence", {})
    state["validation_reports"] = getattr(planner_result, "validation_reports", []) or state.get("validation_reports", [])
    state["repair_history"] = getattr(planner_result, "repair_history", []) or state.get("repair_history", [])

    success = bool(runtime_report.get("success")) and int(runtime_report.get("record_count") or 0) > 0
    runtime_validation = {
        "gate": "final_runtime_output_gate",
        "passed": success,
        "failure_type": None,
        "summary": "Final runtime output accepted." if success else str(runtime_report.get("error") or "Final runtime output rejected."),
        "assertions": [{
            "check": "final_record_count_positive",
            "expected": True,
            "observed": int(runtime_report.get("record_count") or 0),
            "passed": success,
            "artifact_ref": None,
            "note": "",
        }],
        "metrics": dict(runtime_report.get("stage_counts") or {}),
        "rollback_target": None,
    }
    attribution = _runtime_attribution(state, runtime_report)
    if not success and attribution:
        runtime_validation["failure_type"] = attribution["failure_type"]
        runtime_validation["rollback_target"] = attribution["rollback_target"]
        state["attribution_decision"] = attribution
        state["repair_history"] = list(state.get("repair_history") or []) + [{
            "attempt": "next_rerun",
            "failure_type": attribution["failure_type"],
            "rollback_target": attribution["rollback_target"],
            "reason": attribution["reason"],
        }]
    state["validation_reports"] = list(state.get("validation_reports") or []) + [runtime_validation]
    auto_outcome = "success" if success else ("partial" if state.get("generated_code") else "failure")
    findings = run_auto_findings(state, ctx=None)
    if not success:
        findings.setdefault("suspected_failures", []).append({
            "kind": runtime_report.get("error") or "runtime_gate_failed",
            "detail": f"record_count={runtime_report.get('record_count', 0)}",
        })

    draft = new_draft_episode(
        state,
        started_at=state.get("started_at"),
        auto_outcome=auto_outcome,
    )
    draft["html_fingerprint"] = state.get("html_fingerprint")
    draft["auto_findings"] = findings
    draft["stage_evidence"] = state.get("stage_evidence") or {}
    draft["validation_reports"] = state.get("validation_reports") or []
    draft["repair_history"] = state.get("repair_history") or []
    draft["attribution_decision"] = state.get("attribution_decision")
    draft["runtime_report"] = runtime_report
    draft["final_output_summary"] = {
        "reports": len(final_output.get("reports") or []),
        "news_articles": len(final_output.get("newsArticles") or []),
    }

    store = MemoryStore(
        config.memory_root,
        log_callback=log,
        max_episodes=getattr(config, "memory_max_keep", 1000),
    )
    path = store.write_draft(draft)
    log(f"[SUMMARIZE] Runtime-grounded Stage-1 draft written: {path or 'n/a'}")
    return {
        "auto_findings": findings,
        "summary_draft_path": str(path) if path else None,
        "html_fingerprint": state.get("html_fingerprint"),
        "validation_reports": state.get("validation_reports") or [],
        "attribution_decision": state.get("attribution_decision"),
        "repair_history": state.get("repair_history") or [],
    }


__all__ = ["finalize_runtime_episode"]
