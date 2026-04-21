"""Unit tests for the ``verified_selectors`` ledger helper module.

Covers all six public surface areas:

  1. ``bootstrap()`` returns the canonical empty shape.
  2. ``merge_from_extract_list`` promotes container/title/date/next_page on
     positive evidence and is silent on partial/empty input.
  3. ``merge_from_probe_detail`` promotes content/title and stores
     content_alternatives.
  4. ``merge_from_verify_selector`` classifies descriptions, only promotes on
     positive matches, and always records ad-hoc verifications.
  5. Source-rank promotion: verify_selector evidence beats extract_list
     beats probe_detail; older slots are NOT silently downgraded.
  6. ``render_for_prompt`` returns ``""`` for an empty ledger and a
     well-formed "MUST USE" block when slots are populated.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``import verified_selectors`` work whether tests run from repo root
# or from inside the ``pygen`` directory.
_PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(_PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(_PYGEN_DIR))

import verified_selectors as vs  # noqa: E402


# ---------------------------------------------------------------------------
# bootstrap / _ensure
# ---------------------------------------------------------------------------


def test_bootstrap_shape():
    led = vs.bootstrap()
    assert led == {
        "list": {},
        "detail": {},
        "_provenance": {},
        "_ad_hoc_verifications": [],
    }


def test_merge_handles_none_inputs_gracefully():
    led = vs.merge_from_extract_list(None, None)
    assert led == vs.bootstrap()
    led = vs.merge_from_probe_detail(None, {})
    assert led == vs.bootstrap()
    led = vs.merge_from_verify_selector(None, None)
    assert led == vs.bootstrap()


def test_merge_does_not_mutate_input():
    """Pure-function contract: callers must not see in-place writes."""
    original = vs.bootstrap()
    snapshot = {
        "list": dict(original["list"]),
        "detail": dict(original["detail"]),
    }
    _ = vs.merge_from_verify_selector(
        original,
        {
            "selector": "h1.title",
            "description": "title link",
            "totalMatches": 5,
            "visibleMatches": 5,
        },
    )
    assert original["list"] == snapshot["list"]
    assert original["detail"] == snapshot["detail"]


# ---------------------------------------------------------------------------
# merge_from_extract_list
# ---------------------------------------------------------------------------


def _extract_list_payload():
    return {
        "baseUrl": "https://example.com/news/",
        "bestCandidate": {
            "selector": "div.elementor-element-35ac47c",
            "bareSelector": ".elementor-element-35ac47c",
            "candidateSelectors": [
                {"selector": ".elementor-element-35ac47c", "label": "bare"},
                {"selector": "main .elementor-element-35ac47c", "label": "scoped"},
            ],
            "titleSelector": ".elementor-heading-title a",
            "dateSelector": ".elementor-post-info__item--type-date",
            "score": 92,
            "itemCount": 6,
            "hasLink": True,
            "hasDate": True,
        },
        "pagination": {
            "next": {
                "selector": "a.next.page-numbers",
                "url": "https://example.com/news/page/2/",
            }
        },
    }


def test_extract_list_promotes_full_payload():
    led = vs.merge_from_extract_list(None, _extract_list_payload())
    assert led["list"]["container"] == "div.elementor-element-35ac47c"
    assert led["list"]["title_link"] == ".elementor-heading-title a"
    assert led["list"]["date"] == ".elementor-post-info__item--type-date"
    assert led["list"]["next_page"] == "a.next.page-numbers"
    alts = led["list"]["container_alternatives"]
    # bareSelector + scoped, no duplicates with primary
    assert ".elementor-element-35ac47c" in alts
    assert "main .elementor-element-35ac47c" in alts
    assert "div.elementor-element-35ac47c" not in alts

    prov = led["_provenance"]["list.container"]
    assert prov["source"] == "extract_list_and_pagination"
    assert prov["score"] == 92
    assert prov["item_count"] == 6


def test_extract_list_skips_promotion_when_zero_items():
    payload = _extract_list_payload()
    payload["bestCandidate"]["itemCount"] = 0
    led = vs.merge_from_extract_list(None, payload)
    assert led["list"].get("container") is None
    assert led["list"].get("title_link") is None
    # pagination still recorded because pagination block exists independently
    assert led["list"].get("next_page") == "a.next.page-numbers"


def test_extract_list_with_empty_pagination():
    payload = _extract_list_payload()
    payload["pagination"] = {}
    led = vs.merge_from_extract_list(None, payload)
    assert "next_page" not in led["list"]


# ---------------------------------------------------------------------------
# merge_from_probe_detail
# ---------------------------------------------------------------------------


def _probe_detail_payload():
    return {
        "url": "https://example.com/news/foo/",
        "contentSelector": "div.elementor-widget-theme-post-content",
        "contentTagName": "div.elementor-widget-theme-post-content",
        "contentTextLength": 2400,
        "sampleContentHtml": "<p>...</p>",
        "contentCandidates": [
            {"selector": "div.elementor-widget-theme-post-content"},
            {"selector": "article.post"},
            {"selector": ".entry-content"},
        ],
        "titleSelector": "h1.elementor-heading-title",
    }


def test_probe_detail_promotes_content_and_alternatives():
    led = vs.merge_from_probe_detail(None, _probe_detail_payload())
    assert led["detail"]["content"] == "div.elementor-widget-theme-post-content"
    assert led["detail"]["title"] == "h1.elementor-heading-title"
    alts = led["detail"]["content_alternatives"]
    assert "article.post" in alts
    assert ".entry-content" in alts
    assert "div.elementor-widget-theme-post-content" not in alts


def test_probe_detail_skips_when_no_content_selector():
    payload = _probe_detail_payload()
    payload["contentSelector"] = ""
    led = vs.merge_from_probe_detail(None, payload)
    assert "content" not in led["detail"]
    # title still promoted
    assert led["detail"]["title"] == "h1.elementor-heading-title"


# ---------------------------------------------------------------------------
# merge_from_verify_selector
# ---------------------------------------------------------------------------


def test_verify_selector_promotes_on_title_link_description():
    payload = {
        "selector": ".elementor-heading-title a",
        "description": "Check title and link",
        "totalMatches": 6,
        "visibleMatches": 6,
    }
    led = vs.merge_from_verify_selector(None, payload)
    assert led["list"]["title_link"] == ".elementor-heading-title a"
    prov = led["_provenance"]["list.title_link"]
    assert prov["source"] == "verify_selector"
    assert prov["total"] == 6
    assert prov["visible"] == 6
    # always logged in ad_hoc list as well
    assert led["_ad_hoc_verifications"][-1]["selector"] == ".elementor-heading-title a"


def test_verify_selector_does_not_promote_on_zero_matches():
    payload = {
        "selector": ".does-not-exist",
        "description": "title link",
        "totalMatches": 0,
        "visibleMatches": 0,
    }
    led = vs.merge_from_verify_selector(None, payload)
    assert "title_link" not in led["list"]
    # but we still record the failed verification for debugging
    assert led["_ad_hoc_verifications"][-1]["selector"] == ".does-not-exist"


def test_verify_selector_with_unknown_description_only_logs_ad_hoc():
    payload = {
        "selector": ".weird-thing",
        "description": "inspecting random element",
        "totalMatches": 3,
        "visibleMatches": 3,
    }
    led = vs.merge_from_verify_selector(None, payload)
    assert led["list"] == {}
    assert led["detail"] == {}
    assert led["_ad_hoc_verifications"][-1]["selector"] == ".weird-thing"


def test_verify_selector_classifies_common_descriptions():
    cases = [
        ("Check item selector",        ("list", "container")),
        ("body container candidate",   ("detail", "content")),
        ("next page link",             ("list", "next_page")),
        ("publish date",               ("list", "date")),
        ("Check read more link",       ("list", "title_link")),
        ("article body",               ("detail", "content")),
    ]
    for desc, (section, key) in cases:
        led = vs.merge_from_verify_selector(
            None,
            {
                "selector": "X",
                "description": desc,
                "totalMatches": 1,
                "visibleMatches": 1,
            },
        )
        assert led[section].get(key) == "X", f"{desc!r} should map to {section}.{key}"


def test_verify_selector_caps_ad_hoc_history_to_25():
    led = None
    for i in range(40):
        led = vs.merge_from_verify_selector(
            led,
            {
                "selector": f".sel-{i}",
                "description": "noop",
                "totalMatches": 1,
                "visibleMatches": 1,
            },
        )
    assert len(led["_ad_hoc_verifications"]) == 25
    # newest should be retained
    assert led["_ad_hoc_verifications"][-1]["selector"] == ".sel-39"


# ---------------------------------------------------------------------------
# Source-rank semantics
# ---------------------------------------------------------------------------


def test_verify_selector_upgrades_extract_list_value():
    led = vs.merge_from_extract_list(None, _extract_list_payload())
    assert led["list"]["title_link"] == ".elementor-heading-title a"

    led = vs.merge_from_verify_selector(
        led,
        {
            "selector": ".better-title-link",
            "description": "title link",
            "totalMatches": 6,
            "visibleMatches": 6,
        },
    )
    assert led["list"]["title_link"] == ".better-title-link"
    assert led["_provenance"]["list.title_link"]["source"] == "verify_selector"


def test_extract_list_does_not_downgrade_verify_selector_value():
    """A weaker source must NOT clobber a stronger source's selector."""
    led = vs.merge_from_verify_selector(
        None,
        {
            "selector": ".verified-title",
            "description": "title link",
            "totalMatches": 6,
            "visibleMatches": 6,
        },
    )
    led = vs.merge_from_extract_list(led, _extract_list_payload())
    assert led["list"]["title_link"] == ".verified-title"
    assert led["_provenance"]["list.title_link"]["source"] == "verify_selector"


def test_probe_detail_does_not_downgrade_verify_selector_value():
    led = vs.merge_from_verify_selector(
        None,
        {
            "selector": ".verified-content",
            "description": "article body",
            "totalMatches": 1,
            "visibleMatches": 1,
        },
    )
    led = vs.merge_from_probe_detail(led, _probe_detail_payload())
    assert led["detail"]["content"] == ".verified-content"
    assert led["_provenance"]["detail.content"]["source"] == "verify_selector"


# ---------------------------------------------------------------------------
# render_for_prompt
# ---------------------------------------------------------------------------


def test_render_empty_ledger_returns_empty_string():
    assert vs.render_for_prompt(None) == ""
    assert vs.render_for_prompt(vs.bootstrap()) == ""


def test_render_full_ledger_includes_must_use_and_selectors():
    led = vs.merge_from_extract_list(None, _extract_list_payload())
    led = vs.merge_from_probe_detail(led, _probe_detail_payload())
    led = vs.merge_from_verify_selector(
        led,
        {
            "selector": ".verified-title",
            "description": "title link",
            "totalMatches": 6,
            "visibleMatches": 6,
        },
    )
    out = vs.render_for_prompt(led)
    # imperative header present
    assert "强约束" in out
    assert "必须" in out
    # actual selectors rendered
    assert "div.elementor-element-35ac47c" in out
    assert ".verified-title" in out
    assert "div.elementor-widget-theme-post-content" in out
    # provenance hints rendered
    assert "verify_selector" in out
    assert "extract_list_and_pagination" in out
    # alternatives rendered as bullets
    assert "container alternatives" in out
    # ends with a single newline
    assert out.endswith("\n")


def test_render_only_list_section_when_detail_empty():
    led = vs.merge_from_extract_list(None, _extract_list_payload())
    out = vs.render_for_prompt(led)
    assert "List page" in out
    assert "Detail page" not in out


def test_render_skips_empty_lists():
    led = vs.bootstrap()
    led["list"]["container_alternatives"] = []  # empty list must not render
    led["list"]["container"] = "div.foo"
    led["_provenance"]["list.container"] = {"source": "verify_selector", "total": 3, "visible": 3}
    out = vs.render_for_prompt(led)
    assert "div.foo" in out
    # no orphan bullet for the empty alternatives list
    assert "container alternatives" not in out
