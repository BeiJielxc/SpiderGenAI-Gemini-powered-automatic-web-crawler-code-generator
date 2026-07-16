"""Evidence contracts, deterministic gates and rollback attribution."""

from __future__ import annotations

import pytest


def test_candidate_ids_are_stable_and_payload_sensitive():
    from agents.evidence import make_candidate_id

    first = make_candidate_id("selector", {"title": "a", "container": "li"})
    reordered = make_candidate_id("selector", {"container": "li", "title": "a"})
    changed = make_candidate_id("selector", {"container": "div", "title": "a"})
    assert first == reordered
    assert first != changed


def test_acquisition_gate_requires_replayable_evidence():
    from agents.gates import acquisition_gate

    failed = acquisition_gate({"acquisition_route": "dom"})
    assert failed["validation_reports"][0]["passed"] is False
    assert failed["validation_reports"][0]["rollback_target"] == "selector"

    passed = acquisition_gate({
        "verified_selectors": {
            "list": {"container": "ul.items > li", "title_link": "a.title"}
        }
    })
    report = passed["validation_reports"][0]
    evidence = passed["stage_evidence"]["acquisition"]
    assert report["passed"] is True
    assert evidence["selected_candidate_id"]
    assert evidence["candidates"][0]["status"] == "verified"


def test_telemetry_and_raw_xhr_are_not_verified_business_apis():
    from agents.gates import acquisition_gate

    output = acquisition_gate({
        "acquisition_route": "api",
        "network_requests": {"api_requests": [
            {"url": "https://www.google-analytics.com/g/collect", "method": "POST"},
            {"url": "https://example.test/wp-admin/admin-ajax.php", "method": "POST"},
        ]},
    })
    evidence = output["stage_evidence"]["acquisition"]
    assert output["validation_reports"][0]["passed"] is False
    assert len(evidence["candidates"]) == 1
    assert evidence["candidates"][0]["status"] == "proposed"


def test_observable_dom_can_proceed_provisionally_to_runtime_validation():
    from agents.gates import acquisition_gate

    output = acquisition_gate({
        "acquisition_route": "dom",
        "page_html_len": 12000,
        "page_structure": {"lists": [{"count": 5}], "links": {}},
    })
    report = output["validation_reports"][0]
    evidence = output["stage_evidence"]["acquisition"]

    assert report["passed"] is True
    assert evidence["status"] == "proposed"
    assert evidence["candidates"][-1]["kind"] == "dom_structure_fallback"
    assert "selector_unverified_runtime_required" in evidence["risk_flags"]


def test_site_profile_rejects_evidence_from_wrong_page():
    from agents.gates import site_profile_gate

    output = site_profile_gate({
        "url": "https://example.test/news/",
        "page_info": {"url": "https://other.test/", "title": "Other"},
        "page_html_len": 5000,
        "page_structure": {"lists": [{"count": 4}]},
    })
    assert output["validation_reports"][0]["passed"] is False


def test_code_gate_surfaces_quota_error_without_retry_target():
    from agents.gates import code_gate

    output = code_gate({
        "generated_code": None,
        "last_error": "429 RESOURCE_EXHAUSTED: quota exceeded",
    })
    report = output["validation_reports"][0]
    assert report["failure_type"] == "llm_quota_exceeded"
    assert report["rollback_target"] == "exhausted"
    assert "RESOURCE_EXHAUSTED" in report["summary"]


def test_date_gate_rejects_invalid_range():
    from agents.gates import date_scope_gate

    output = date_scope_gate({
        "start_date": "2026-07-20",
        "end_date": "2026-07-01",
        "verified_selectors": {"list": {"date": ".date"}},
    })
    assert output["validation_reports"][0]["passed"] is False
    assert output["validation_reports"][0]["rollback_target"] == "date_scope"


def test_date_text_fallback_is_provisional_until_runtime():
    from agents.gates import date_scope_gate

    output = date_scope_gate({
        "start_date": "2026-07-01",
        "end_date": "2026-07-20",
        "verified_selectors": {"list": {}},
    })
    assert output["validation_reports"][0]["passed"] is True
    evidence = output["stage_evidence"]["date_scope"]
    assert evidence["status"] == "proposed"
    assert "runtime_text_date_fallback" in evidence["risk_flags"]


@pytest.mark.asyncio
async def test_known_failure_uses_deterministic_rollback_without_llm():
    from agents.supervisor import attribution_node

    class FailingLLM:
        async def ainvoke(self, _messages):
            raise AssertionError("known causes must not call the LLM")

    output = await attribution_node({
        "validation_reports": [{
            "passed": False,
            "failure_type": "selector_mismatch",
            "summary": "candidate returned zero rows",
        }],
        "rollback_count": 0,
    }, llm=FailingLLM())
    assert output["rollback_target"] == "selector"
    assert output["repair_history"][0]["failure_type"] == "selector_mismatch"
