"""Deterministic stage gates and evidence extraction.

Specialists may propose conclusions, but only these gates can advance the main graph.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .evidence import (
    EvidenceAssertion,
    EvidenceCandidate,
    StageEvidence,
    ValidationReport,
    make_candidate_id,
    merge_stage_evidence,
)
from .state import AgentState


def _artifact_refs(state: AgentState, actions: Iterable[str]) -> List[str]:
    wanted = set(actions)
    refs: List[str] = []
    for entry in state.get("tool_calls_log") or []:
        if entry.get("action") not in wanted:
            continue
        artifacts = entry.get("artifacts") or {}
        if isinstance(artifacts, dict):
            for value in artifacts.values():
                if isinstance(value, dict) and value.get("artifact_id"):
                    refs.append(str(value["artifact_id"]))
    return list(dict.fromkeys(refs))


def _result(evidence: StageEvidence, report: ValidationReport, state: AgentState) -> Dict[str, Any]:
    return {
        "stage_evidence": merge_stage_evidence(state.get("stage_evidence"), evidence),
        "validation_reports": [report.to_dict()],
        "last_error": None if report.passed else report.summary,
    }


def site_profile_gate(state: AgentState) -> Dict[str, Any]:
    page_info = state.get("page_info") or {}
    structure = state.get("page_structure") or {}
    html_len = int(state.get("page_html_len") or 0)
    accessible = bool(page_info.get("url") or page_info.get("title"))
    observable = bool(html_len > 100 or structure)
    current_url = str(page_info.get("url") or "").strip()
    target_url = str(state.get("url") or "").strip()
    if target_url:
        current = urlparse(current_url)
        target = urlparse(target_url)
        current_path = (current.path or "/").rstrip("/") or "/"
        target_path = (target.path or "/").rstrip("/") or "/"
        target_match = current.hostname == target.hostname and current_path == target_path
    else:
        target_match = True
    assertions = [
        EvidenceAssertion("page_accessible", True, accessible, accessible),
        EvidenceAssertion("page_observable", True, {"html_len": html_len, "has_structure": bool(structure)}, observable),
        EvidenceAssertion(
            "requested_page_restored",
            True,
            {"target": target_url, "current": current_url},
            target_match,
        ),
    ]
    passed = accessible and observable and target_match
    evidence = StageEvidence(
        stage="site_profile",
        status="verified" if passed else "rejected",
        confidence=0.9 if passed else 0.25,
        assertions=assertions,
        artifact_refs=_artifact_refs(state, {"get_page_html", "analyze_page", "enhanced_page_analysis"}),
        risk_flags=[] if passed else ["site_not_observable"],
        recommended_next_stage="acquisition_router" if passed else "site_profiler",
    )
    report = ValidationReport(
        gate="site_profile_gate",
        passed=passed,
        failure_type=None if passed else "site_unreachable",
        summary="Site profile evidence accepted." if passed else "The page is not accessible or observable.",
        assertions=assertions,
        rollback_target=None if passed else "site_profiler",
    )
    return _result(evidence, report, state)


def _api_candidates(state: AgentState) -> List[EvidenceCandidate]:
    network = state.get("network_requests") or {}
    raw: List[Any] = []
    for key in ("api_requests", "intercepted_apis", "requests", "candidate_endpoints"):
        value = network.get(key) if isinstance(network, dict) else None
        if isinstance(value, list):
            raw.extend(value)
    enhanced = state.get("enhanced_analysis") or {}
    captured_data_api = enhanced.get("captured_data_api") if isinstance(enhanced, dict) else None
    verified_urls = set()
    if isinstance(captured_data_api, dict):
        data_apis = captured_data_api.get("dataApis") or []
        if isinstance(captured_data_api.get("bestApi"), dict):
            data_apis = [captured_data_api["bestApi"]] + list(data_apis)
        for item in data_apis:
            if isinstance(item, dict) and item.get("url"):
                verified_urls.add(str(item["url"]))
                raw.append(item)
    for key in ("api_candidates", "candidate_endpoints"):
        value = enhanced.get(key) if isinstance(enhanced, dict) else None
        if isinstance(value, list):
            raw.extend(value)
    out: List[EvidenceCandidate] = []
    seen = set()
    for item in raw:
        payload = item if isinstance(item, dict) else {"url": str(item)}
        url = str(payload.get("url") or payload.get("request_url") or "").strip()
        if not url or url in seen:
            continue
        host = (urlparse(url).hostname or "").lower()
        if any(marker in host for marker in (
            "google-analytics.com", "googletagmanager.com", "doubleclick.net",
            "facebook.com", "clarity.ms", "hotjar.com",
        )):
            continue
        seen.add(url)
        cid = make_candidate_id("api", {"url": url, "method": payload.get("method")})
        out.append(EvidenceCandidate(
            candidate_id=cid,
            kind="api_endpoint",
            value={"url": url, "method": payload.get("method", "GET")},
            status="verified" if url in verified_urls else "proposed",
            confidence=float(payload.get("confidence") or (0.85 if url in verified_urls else 0.4)),
            provenance={"source": "captured_data_api" if url in verified_urls else "captured_network"},
        ))
        if len(out) >= 3:
            break
    return out


def _selector_candidates(state: AgentState) -> Tuple[List[EvidenceCandidate], Dict[str, Any]]:
    ledger = state.get("verified_selectors") or {}
    list_slots = ledger.get("list") if isinstance(ledger, dict) else {}
    list_slots = list_slots if isinstance(list_slots, dict) else {}
    selectors: List[str] = []
    for key in ("container", "title_link", "date", "next_page"):
        value = list_slots.get(key)
        if isinstance(value, str) and value.strip():
            selectors.append(value.strip())
    candidates: List[EvidenceCandidate] = []
    if selectors:
        cid = make_candidate_id("selector", list_slots)
        candidates.append(EvidenceCandidate(
            candidate_id=cid,
            kind="selector_bundle",
            value=dict(list_slots),
            status="proposed",
            confidence=0.65,
            provenance={"source": "verified_selector_ledger"},
        ))
    for alt in list_slots.get("container_alternatives") or []:
        if not isinstance(alt, str):
            continue
        payload = {"container": alt, "title_link": list_slots.get("title_link")}
        candidates.append(EvidenceCandidate(
            candidate_id=make_candidate_id("selector", payload),
            kind="selector_bundle",
            value=payload,
            status="proposed",
            confidence=0.45,
            provenance={"source": "selector_alternative"},
        ))
        if len(candidates) >= 3:
            break
    return candidates, list_slots


def acquisition_gate(state: AgentState) -> Dict[str, Any]:
    api = _api_candidates(state)
    selectors, slots = _selector_candidates(state)
    api_ok = any(item.status == "verified" for item in api)
    selector_ok = bool(slots.get("container") and (slots.get("title_link") or slots.get("title")))
    structure = state.get("page_structure") or {}
    lists = structure.get("lists") if isinstance(structure, dict) else []
    links = structure.get("links") if isinstance(structure, dict) else {}
    list_count = len(lists) if isinstance(lists, list) else 0
    link_count = 0
    if isinstance(links, dict):
        link_count = sum(len(value) for value in links.values() if isinstance(value, list))
    elif isinstance(links, list):
        link_count = len(links)
    html_len = int(state.get("page_html_len") or 0)
    dom_fallback_ok = html_len > 1000 and (list_count > 0 or link_count >= 3)
    passed = api_ok or selector_ok or dom_fallback_ok
    assertions = [
        EvidenceAssertion("api_candidate_available", True, len(api), api_ok),
        EvidenceAssertion(
            "selector_bundle_has_container_and_title",
            True,
            {"container": bool(slots.get("container")), "title": bool(slots.get("title_link") or slots.get("title"))},
            selector_ok,
        ),
        EvidenceAssertion(
            "observable_dom_available_for_runtime_validation",
            True,
            {"html_len": html_len, "lists": list_count, "links": link_count},
            dom_fallback_ok,
        ),
    ]
    candidates = api + selectors
    dom_candidate = None
    if dom_fallback_ok:
        dom_payload = {"html_len": html_len, "list_count": list_count, "link_count": link_count}
        dom_candidate = EvidenceCandidate(
            candidate_id=make_candidate_id("dom", dom_payload),
            kind="dom_structure_fallback",
            value=dom_payload,
            status="proposed",
            confidence=0.4,
            provenance={"source": "site_profile_page_structure"},
            risk_flags=["selector_unverified_runtime_required"],
        )
        candidates.append(dom_candidate)
    selected_candidate = next((item for item in candidates if item.status == "verified"), None)
    if selected_candidate is None and selector_ok:
        selected_candidate = next((item for item in selectors), None)
    if selected_candidate is None and dom_candidate is not None:
        selected_candidate = dom_candidate
    selected = selected_candidate.candidate_id if selected_candidate else None
    for item in candidates:
        if item.candidate_id == selected and (api_ok or selector_ok):
            item.status = "verified"
    strongly_verified = api_ok or selector_ok
    summary = (
        "Acquisition evidence accepted."
        if strongly_verified
        else "Observable DOM accepted provisionally; Critic and runtime validation are required."
        if dom_fallback_ok
        else "No usable API, selector bundle, or observable DOM was found."
    )
    evidence = StageEvidence(
        stage="acquisition",
        status="verified" if strongly_verified else ("proposed" if passed else "rejected"),
        confidence=0.8 if (api_ok and selector_ok) else (0.68 if strongly_verified else (0.4 if passed else 0.2)),
        selected_candidate_id=selected,
        candidates=candidates,
        assertions=assertions,
        artifact_refs=_artifact_refs(state, {"capture_api_and_infer_params", "extract_list_and_pagination", "verify_selector"}),
        risk_flags=([] if strongly_verified else ["selector_unverified_runtime_required"] if passed
                    else ["no_replayable_api_selector_or_dom_evidence"]),
        recommended_next_stage="date_scope" if passed else ("api_discovery" if state.get("acquisition_route") == "api" else "selector"),
    )
    report = ValidationReport(
        gate="acquisition_gate",
        passed=passed,
        failure_type=None if passed else "acquisition_evidence_missing",
        summary=summary,
        assertions=assertions,
        rollback_target=None if passed else ("api_discovery" if state.get("acquisition_route") == "api" else "selector"),
    )
    return _result(evidence, report, state)


def date_scope_gate(state: AgentState) -> Dict[str, Any]:
    start = (state.get("start_date") or "").strip()
    end = (state.get("end_date") or "").strip()
    date_required = bool(start or end)
    date_api = state.get("date_api_result") or {}
    ledger = state.get("verified_selectors") or {}
    slots = ledger.get("list") if isinstance(ledger, dict) else {}
    has_date_source = bool(date_api or (isinstance(slots, dict) and slots.get("date")))
    # A date can still be parsed from item text in generated code. That path is
    # provisional and must be proven by the final runtime + final-output gates.
    range_valid = not (start and end and start > end)
    passed = range_valid
    assertions = [
        EvidenceAssertion("date_range_well_formed", True, {"start": start, "end": end}, range_valid),
        EvidenceAssertion(
            "date_strategy_declared",
            True,
            {"verified_source": has_date_source, "runtime_text_fallback": date_required and not has_date_source},
            range_valid,
        ),
    ]
    payload = {"api": bool(date_api), "selector": slots.get("date") if isinstance(slots, dict) else None}
    candidate = EvidenceCandidate(
        candidate_id=make_candidate_id("date", payload),
        kind="date_strategy",
        value=payload,
        status=("verified" if passed and (has_date_source or not date_required)
                else "proposed" if passed else "rejected"),
        confidence=0.75 if has_date_source else 0.35,
        provenance={"source": "date_api_or_selector"},
        risk_flags=[] if has_date_source else ["runtime_text_date_fallback"],
    )
    evidence = StageEvidence(
        stage="date_scope",
        status=candidate.status,
        confidence=candidate.confidence,
        selected_candidate_id=candidate.candidate_id,
        candidates=[candidate],
        assertions=assertions,
        risk_flags=list(candidate.risk_flags),
        recommended_next_stage="codegen" if passed else "date_scope",
    )
    report = ValidationReport(
        gate="date_scope_gate",
        passed=passed,
        failure_type=None if passed else "date_strategy_unverified",
        summary=(
            "Date strategy accepted."
            if has_date_source or not date_required
            else "Date text fallback accepted provisionally; runtime proof is required."
        ) if passed else "The date range is invalid.",
        assertions=assertions,
        rollback_target=None if passed else "date_scope",
    )
    return _result(evidence, report, state)


def code_gate(state: AgentState) -> Dict[str, Any]:
    code = state.get("generated_code") or ""
    passed = bool(code.strip())
    codegen_error = str(state.get("last_error") or "")
    error_lower = codegen_error.lower()
    quota_failure = any(marker in error_lower for marker in (
        "resource_exhausted", "quota", "rate limit", "rate_limit", "429",
    ))
    failure_type = None if passed else ("llm_quota_exceeded" if quota_failure else "empty_code")
    rollback_target = None if passed else ("exhausted" if quota_failure else "codegen")
    assertions = [EvidenceAssertion("generated_code_non_empty", True, len(code), passed)]
    candidate = EvidenceCandidate(
        candidate_id=make_candidate_id("code", code),
        kind="crawler_code",
        value={"chars": len(code), "strategy": state.get("code_strategy")},
        status="verified" if passed else "rejected",
        confidence=0.8 if passed else 0.0,
        provenance={"source": "codegen_specialist"},
    )
    evidence = StageEvidence(
        stage="codegen",
        status="verified" if passed else "rejected",
        confidence=candidate.confidence,
        selected_candidate_id=candidate.candidate_id,
        candidates=[candidate],
        assertions=assertions,
        recommended_next_stage="critic" if passed else "codegen",
    )
    report = ValidationReport(
        gate="code_gate",
        passed=passed,
        failure_type=failure_type,
        summary=(
            "Code generation gate passed."
            if passed else codegen_error or "Codegen produced no runnable script."
        ),
        assertions=assertions,
        rollback_target=rollback_target,
    )
    return _result(evidence, report, state)


def critic_output_gate(state: AgentState) -> Dict[str, Any]:
    verdict = state.get("critic_verdict") or {}
    passed = bool(verdict.get("passed"))
    details = verdict.get("details") or {}
    cause = details.get("primary_cause") or (None if passed else "unknown")
    assertion = EvidenceAssertion("critic_runtime_passed", True, passed, passed)
    report = ValidationReport(
        gate="critic_output_gate",
        passed=passed,
        failure_type=cause,
        summary=str(verdict.get("summary") or ("Critic accepted crawler." if passed else "Critic rejected crawler.")),
        assertions=[assertion],
        metrics={"confidence": verdict.get("confidence", 0.0)},
    )
    evidence = StageEvidence(
        stage="critic",
        status="verified" if passed else "rejected",
        confidence=float(verdict.get("confidence") or 0.0),
        assertions=[assertion],
        risk_flags=[] if passed else [str(cause)],
        recommended_next_stage="runtime_execution" if passed else "attribution_critic",
    )
    return _result(evidence, report, state)


def last_gate_passed(state: AgentState) -> str:
    reports = state.get("validation_reports") or []
    return "pass" if reports and reports[-1].get("passed") else "fail"


__all__ = [
    "acquisition_gate",
    "code_gate",
    "critic_output_gate",
    "date_scope_gate",
    "last_gate_passed",
    "site_profile_gate",
]
