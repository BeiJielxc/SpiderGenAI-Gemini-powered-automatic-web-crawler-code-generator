"""Runtime-grounded failure attribution tests."""

from __future__ import annotations


def test_date_filter_zero_is_attributed_to_date_scope():
    from memory.runtime_finalize import _runtime_attribution

    decision = _runtime_attribution({}, {
        "success": False,
        "record_count": 0,
        "stage_counts": {"runtime_records": 12, "final_records": 0},
        "exit_code": 0,
        "schema_valid": True,
    })
    assert decision["failure_type"] == "date_filter_too_strict"
    assert decision["rollback_target"] == "date_scope"


def test_empty_api_candidate_output_is_attributed_to_api_discovery():
    from memory.runtime_finalize import _runtime_attribution

    state = {"stage_evidence": {"acquisition": {
        "selected_candidate_id": "api-1",
        "candidates": [{"candidate_id": "api-1", "kind": "api_endpoint"}],
    }}}
    decision = _runtime_attribution(state, {
        "success": False,
        "record_count": 0,
        "stage_counts": {"runtime_records": 0, "final_records": 0},
        "exit_code": 0,
        "schema_valid": False,
    })
    assert decision["failure_type"] == "api_schema_mismatch"
    assert decision["rollback_target"] == "api_discovery"


def test_external_output_path_is_attributed_to_codegen_before_api():
    from memory.runtime_finalize import _runtime_attribution

    state = {"stage_evidence": {"acquisition": {
        "selected_candidate_id": "api-1",
        "candidates": [{"candidate_id": "api-1", "kind": "api_endpoint"}],
    }}}
    decision = _runtime_attribution(state, {
        "success": False,
        "record_count": 0,
        "stage_counts": {"runtime_records": 0, "final_records": 0},
        "exit_code": 0,
        "schema_valid": False,
        "error": "Crawler produced no task-owned JSON output. The script may have written to an absolute/shared path.",
    })
    assert decision["failure_type"] == "output_path_escape"
    assert decision["rollback_target"] == "codegen"


def test_missing_generated_code_is_not_attributed_to_selected_api():
    from memory.runtime_finalize import _runtime_attribution

    state = {"stage_evidence": {"acquisition": {
        "selected_candidate_id": "api-1",
        "candidates": [{"candidate_id": "api-1", "kind": "api_endpoint"}],
    }}}
    decision = _runtime_attribution(state, {
        "success": False,
        "record_count": 0,
        "error": "Agent did not produce any crawler code.",
    })
    assert decision["failure_type"] == "empty_code"
    assert decision["rollback_target"] == "codegen"


def test_planner_acquisition_failure_is_attributed_before_schema_fallback():
    from memory.runtime_finalize import _runtime_attribution

    decision = _runtime_attribution({}, {
        "success": False,
        "record_count": 0,
        "schema_valid": False,
        "error": "No usable API or selector bundle was verified.",
    })
    assert decision["failure_type"] == "acquisition_evidence_missing"
    assert decision["rollback_target"] == "selector"


def test_dom_fallback_zero_output_is_attributed_to_selector():
    from memory.runtime_finalize import _runtime_attribution

    state = {"stage_evidence": {"acquisition": {
        "selected_candidate_id": "dom-1",
        "candidates": [{"candidate_id": "dom-1", "kind": "dom_structure_fallback"}],
    }}}
    decision = _runtime_attribution(state, {
        "success": False,
        "record_count": 0,
        "stage_counts": {"runtime_records": 0, "final_records": 0},
        "exit_code": 0,
        "schema_valid": False,
    })
    assert decision["failure_type"] == "selector_mismatch"
    assert decision["rollback_target"] == "selector"
