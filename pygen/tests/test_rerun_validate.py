"""Tests for the rerun pre-validation pipeline (Module B).

Covers:

* Selector extraction from freeform Chinese ``fix_direction`` text.
* ``collect_selectors_from_verified`` walking nested dicts.
* End-to-end ``pre_validate_rerun_selectors``:
    - cache hit + matching DOM → ``pre_validated``
    - cache hit + non-matching DOM → ``disproved``
    - cache miss → ``skipped`` with reason ``cache_miss``
    - cache drift → ``skipped`` with reason ``drift``
    - empty chain / no candidates → empty report
    - chain present but page_cache=None → empty report
* Render returns "" for empty reports, otherwise non-empty + contains
  the candidate selectors verbatim (so the planner can copy-paste).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from agents.rerun_validate import (  # noqa: E402
    PreValidationReport,
    collect_selectors_from_verified,
    extract_selectors_from_text,
    pre_validate_rerun_selectors,
    render_pre_validation_hint,
)
from page_cache import PageCache  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path) -> PageCache:
    return PageCache(tmp_path / "pc", ttl_sec=600)


def _ep(*, fix_direction: str = "", verified: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a minimal episode dict suitable for the rerun chain."""
    return {
        "task_id": "ep-1",
        "domain": "example.com",
        "lessons": {
            "failure_analysis": {
                "fix_direction": fix_direction,
                "root_cause_guess": "",
                "user_complaint_interpreted": "",
            },
        },
        "verified_selectors": verified or {},
    }


# ---------------------------------------------------------------------------
# Selector extraction — text
# ---------------------------------------------------------------------------


def test_extract_selectors_finds_class_in_chinese_text():
    """Real-world style: Chinese sentence with one CSS class."""
    text = "应该改用 .elementor-widget-theme-post-content 作为正文容器"
    sels = extract_selectors_from_text(text)
    assert ".elementor-widget-theme-post-content" in sels


def test_extract_selectors_finds_id_and_attr():
    text = "用 #main-list 容器，里面的 a[href*='/news/'] 是真正的标题链接"
    sels = extract_selectors_from_text(text)
    assert "#main-list" in sels
    assert "a[href*='/news/']" in sels


def test_extract_selectors_drops_bare_tags():
    """A naked ``div`` is not a useful selector — must be filtered."""
    text = "用 div 包住所有 li 元素"
    assert extract_selectors_from_text(text) == []


def test_extract_selectors_dedup_and_cap():
    """Each class mention is a STANDALONE selector (separated by commas
    so the regex doesn't merge them into one descendant path)."""
    text = ".aa, .aa, .bb, .cc, .dd, .ee, .ff"  # one dup
    sels = extract_selectors_from_text(text, max_selectors=4)
    assert len(sels) == 4
    assert sels[0] == ".aa"
    assert len(set(sels)) == len(sels)


def test_extract_selectors_empty_input():
    assert extract_selectors_from_text("") == []
    assert extract_selectors_from_text(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Selector extraction — verified_selectors ledger
# ---------------------------------------------------------------------------


def test_collect_from_verified_walks_nested_dicts():
    verified = {
        "list": {
            "container": ".articles",
            "title": ".articles h2 a",
            "_meta": "ignored: bare tag",
        },
        "detail": {
            "title": "h1.headline",
            "content": ".post-body",
        },
    }
    sels = collect_selectors_from_verified(verified)
    assert ".articles" in sels
    assert ".articles h2 a" in sels
    assert "h1.headline" in sels
    assert ".post-body" in sels
    # Sanity: the bare-tag string was filtered.
    assert "ignored: bare tag" not in sels


def test_collect_from_verified_handles_lists():
    verified = {"list": {"candidates": [".one", ".two", ".three"]}}
    assert collect_selectors_from_verified(verified) == [".one", ".two", ".three"]


def test_collect_from_verified_dedup():
    verified = {"a": ".dup", "b": ".dup", "c": ".other"}
    sels = collect_selectors_from_verified(verified)
    assert sels.count(".dup") == 1
    assert ".other" in sels


# ---------------------------------------------------------------------------
# Pre-validation: end-to-end
# ---------------------------------------------------------------------------


def test_prevalidate_marks_matching_selectors_as_pre_validated(cache):
    url = "https://example.com/news"
    cache.put_html(
        url,
        "<html><body>"
        "<div class='post-body'><p>real content</p></div>"
        "<div class='sidebar'>x</div>"
        "</body></html>",
    )
    chain = [_ep(fix_direction="改用 .post-body 作为正文")]
    report = pre_validate_rerun_selectors(
        url=url, chain=chain, page_cache=cache
    )
    assert any(r["selector"] == ".post-body" and r["count"] == 1 for r in report.pre_validated)
    assert report.disproved == []


def test_prevalidate_marks_missing_selectors_as_disproved(cache):
    url = "https://example.com/news"
    cache.put_html(
        url,
        "<html><body><div class='real-content'>x</div></body></html>",
    )
    chain = [_ep(fix_direction="试 .icon-list-text 看看")]
    report = pre_validate_rerun_selectors(
        url=url, chain=chain, page_cache=cache
    )
    assert any(r["selector"] == ".icon-list-text" and r["count"] == 0 for r in report.disproved)
    assert report.pre_validated == []


def test_prevalidate_cache_miss_skips_with_reason(cache):
    url = "https://example.com/never-cached"
    chain = [_ep(fix_direction="试 .target")]
    report = pre_validate_rerun_selectors(
        url=url, chain=chain, page_cache=cache
    )
    assert report.pre_validated == []
    assert report.disproved == []
    assert any(s["reason"] == "cache_miss" for s in report.skipped)


def test_prevalidate_drift_skips_with_reason(cache):
    url = "https://example.com/drifty"
    cache.put_html(url, "<html><body><div class='aa'></div></body></html>")
    cache.put_html(
        url,
        "<html><body><article><section><h1>x</h1></section></article></body></html>",
    )
    entry = cache.get(url)
    assert entry.last_drift is True

    chain = [_ep(fix_direction="试 .aa 看看")]
    report = pre_validate_rerun_selectors(
        url=url, chain=chain, page_cache=cache
    )
    assert report.pre_validated == []
    assert any(s["reason"] == "drift" for s in report.skipped)


def test_prevalidate_pulls_from_verified_when_text_empty(cache):
    """No selectors in fix_direction → fall back to verified_selectors."""
    url = "https://example.com/news"
    cache.put_html(
        url,
        "<html><body><h1 class='headline'>hi</h1></body></html>",
    )
    chain = [_ep(fix_direction="", verified={"detail": {"title": "h1.headline"}})]
    report = pre_validate_rerun_selectors(
        url=url, chain=chain, page_cache=cache
    )
    assert any(r["selector"] == "h1.headline" for r in report.pre_validated)


def test_prevalidate_empty_chain_returns_empty(cache):
    report = pre_validate_rerun_selectors(
        url="https://example.com/x", chain=[], page_cache=cache
    )
    assert report.empty


def test_prevalidate_no_cache_returns_empty(cache):
    chain = [_ep(fix_direction="试 .target")]
    report = pre_validate_rerun_selectors(
        url="https://example.com/x", chain=chain, page_cache=None
    )
    assert report.empty


def test_prevalidate_no_extractable_selectors_returns_empty(cache):
    cache.put_html("https://example.com/x", "<html><body>x</body></html>")
    chain = [_ep(fix_direction="this site is just hard to scrape")]
    report = pre_validate_rerun_selectors(
        url="https://example.com/x", chain=chain, page_cache=cache
    )
    assert report.empty


def test_prevalidate_respects_max_selectors_cap(cache):
    cache.put_html("https://example.com/x", "<html/>")
    fix = " ".join(f".sel{i}" for i in range(10))
    chain = [_ep(fix_direction=fix)]
    report = pre_validate_rerun_selectors(
        url="https://example.com/x", chain=chain, page_cache=cache, max_selectors=3
    )
    total = len(report.pre_validated) + len(report.disproved) + len(report.skipped)
    assert total <= 3


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_empty_report_returns_blank():
    assert render_pre_validation_hint(PreValidationReport()) == ""


def test_render_includes_all_three_buckets():
    rep = PreValidationReport(
        cache_url="https://example.com/x",
        cache_age_sec=42.0,
        pre_validated=[{"selector": ".good", "count": 5, "source": "fix_direction"}],
        disproved=[{"selector": ".bad", "count": 0, "source": "previous_run_used"}],
        skipped=[{"selector": ".meh", "reason": "drift"}],
    )
    out = render_pre_validation_hint(rep)
    assert "✅" in out and "❌" in out and "⏭" in out
    assert ".good" in out
    assert ".bad" in out
    assert ".meh" in out
    assert "命中 5" in out
    assert "drift" in out


def test_render_only_pre_validated_omits_other_sections():
    rep = PreValidationReport(
        cache_url="https://x.com",
        cache_age_sec=1.0,
        pre_validated=[{"selector": ".a", "count": 1, "source": "fix_direction"}],
    )
    out = render_pre_validation_hint(rep)
    assert "✅" in out
    assert "❌" not in out
    assert "⏭" not in out
