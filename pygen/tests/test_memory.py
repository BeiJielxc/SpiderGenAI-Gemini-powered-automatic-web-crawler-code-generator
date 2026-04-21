"""Tests for the persistent memory subsystem (``pygen/memory``).

Covers:

* ``Episode`` fact extraction (state -> facts dict).
* ``SiteProfile.update_profile_from_episode``: anti-misleading rules
  (no-op without verdict), confidence decay, selector promotion,
  fingerprint drift, quarantine + recovery.
* ``MemoryStore`` round-trip: write_draft/read_draft/append_committed,
  ring-buffer trimming, JSON corruption self-healing, draft GC.
* ``run_auto_findings``: redundant tool calls + Sec-Zambia smell.
* Renderers: ``render_site_memory_hint`` (incl. quarantine + drift)
  and ``render_feedback_replay_hint``.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from memory.auto_findings import run_auto_findings  # noqa: E402
from memory.episode import (  # noqa: E402
    domain_of,
    extract_facts_from_state,
    is_valid_task_id,
    new_draft_episode,
)
from memory.fingerprint import compute_list_page_fingerprint  # noqa: E402
from memory.render import (  # noqa: E402
    find_recent_task_id_for_domain,
    render_feedback_replay_hint,
    render_site_memory_hint,
    should_inject_profile,
    walk_rerun_chain,
)
from memory.site_profile import (  # noqa: E402
    SiteProfile,
    apply_time_decay,
    update_profile_from_episode,
)
from memory.store import MemoryStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> MemoryStore:
    """Fresh, empty MemoryStore rooted at a tmpdir per test."""
    return MemoryStore(tmp_path / "memory", max_episodes=10)


@pytest.fixture
def sample_state():
    """A realistic AgentState-shaped dict for fact extraction tests."""
    return {
        "task_id": "abc12345",
        "url": "https://www.example.com/news/",
        "run_mode": "news_sentiment",
        "iterations": 7,
        "critic_rounds": 1,
        "generated_code": "line1\nline2\nline3\n",
        "verified_selectors": {
            "list": {
                "container": "ul.news-list",
                "title_link": "li.article a",
            }
        },
        "tool_calls_log": [
            {"action": "verify_selector", "success": True, "action_input": {"selector": "ul.news-list"}},
            {"action": "verify_selector", "success": True, "action_input": {"selector": "li.article a"}},
            {"action": "extract_list_and_pagination", "success": True, "action_input": {}},
        ],
    }


# ---------------------------------------------------------------------------
# episode.py
# ---------------------------------------------------------------------------


def test_domain_of_strips_www_and_lowercases():
    assert domain_of("https://WWW.Example.COM/foo") == "example.com"
    assert domain_of("http://sub.example.com/x") == "sub.example.com"
    assert domain_of("not a url") == ""


def test_is_valid_task_id_rejects_path_traversal():
    assert is_valid_task_id("abc123")
    assert is_valid_task_id("task_42-ok")
    assert not is_valid_task_id("../escape")
    assert not is_valid_task_id("with/slash")
    assert not is_valid_task_id("")
    assert not is_valid_task_id("a" * 100)  # too long


def test_extract_facts_includes_domain_and_tool_stats(sample_state):
    facts = extract_facts_from_state(sample_state, started_at=time.time() - 5)
    assert facts["task_id"] == "abc12345"
    assert facts["domain"] == "example.com"
    assert facts["iterations"] == 7
    assert facts["code_size_lines"] == 3
    assert facts["verified_selectors"]["list"]["container"] == "ul.news-list"
    assert facts["tool_call_stats"]["total_calls"] == 3
    assert facts["tool_call_stats"]["by_tool"]["verify_selector"] == 2
    # duration computed from started_at
    assert facts["duration_sec"] is not None and facts["duration_sec"] >= 0
    # default empty buckets
    assert facts["auto_findings"]["redundant_tool_calls"] == []
    assert facts["committed"] is False


def test_new_draft_episode_normalizes_invalid_outcome(sample_state):
    ep = new_draft_episode(sample_state, auto_outcome="bogus")
    assert ep["auto_outcome"] == "unknown"
    ep_ok = new_draft_episode(sample_state, auto_outcome="success")
    assert ep_ok["auto_outcome"] == "success"


# ---------------------------------------------------------------------------
# site_profile.py
# ---------------------------------------------------------------------------


def _ep(*, verdict, fingerprint=None, selectors=None, lessons=None):
    return {
        "domain": "example.com",
        "user_verdict": verdict,
        "html_fingerprint": fingerprint,
        "verified_selectors": selectors or {},
        "lessons": lessons,
    }


def test_profile_no_op_without_verdict():
    """Anti-misleading invariant #1: no verdict → never mutate."""
    initial = SiteProfile.empty("example.com")
    initial["confidence"] = 0.5
    out = update_profile_from_episode(initial, _ep(verdict=None))
    assert out is initial  # exact same object handed back


def test_profile_correct_increments_wins_and_confidence():
    out = update_profile_from_episode(None, _ep(verdict="correct"))
    assert out["domain"] == "example.com"
    assert out["wins"] == 1
    assert out["losses"] == 0
    assert out["consecutive_failures"] == 0
    assert out["confidence"] > 0.5  # bonus applied


def test_profile_wrong_penalizes_and_quarantines_after_threshold():
    p = update_profile_from_episode(None, _ep(verdict="wrong"))
    assert p["losses"] == 1
    assert p["consecutive_failures"] == 1
    assert p["confidence"] < 0.5
    assert p["quarantined"] is False
    p2 = update_profile_from_episode(p, _ep(verdict="wrong"))
    assert p2["consecutive_failures"] == 2
    assert p2["quarantined"] is True


def test_profile_correct_clears_quarantine():
    p = update_profile_from_episode(None, _ep(verdict="wrong"))
    p = update_profile_from_episode(p, _ep(verdict="wrong"))
    assert p["quarantined"] is True
    p3 = update_profile_from_episode(p, _ep(verdict="correct"))
    assert p3["quarantined"] is False
    assert p3["consecutive_failures"] == 0


def test_profile_selector_promotion_requires_min_wins():
    selectors = {"list": {"title_link": "a.story-link"}}
    p = None
    for _ in range(2):
        p = update_profile_from_episode(p, _ep(verdict="correct", selectors=selectors))
    # Not yet promoted (default promote_min_wins=3)
    assert p["stable_selectors"] == {}
    # 3rd win promotes
    p = update_profile_from_episode(p, _ep(verdict="correct", selectors=selectors))
    assert "list.title_link" in p["stable_selectors"]
    promoted = p["stable_selectors"]["list.title_link"]
    assert promoted["selector"] == "a.story-link"
    assert promoted["wins"] >= 3


def test_profile_drift_detected_on_new_fingerprint():
    p = update_profile_from_episode(None, _ep(verdict="correct", fingerprint="sha256:fp1"))
    assert p["has_drift"] is False
    # same fingerprint again — still no drift
    p = update_profile_from_episode(p, _ep(verdict="correct", fingerprint="sha256:fp1"))
    assert p["has_drift"] is False
    # new fingerprint — drift fires
    p = update_profile_from_episode(p, _ep(verdict="correct", fingerprint="sha256:fp2"))
    assert p["has_drift"] is True
    assert p["html_fingerprints"][0] == "sha256:fp2"


def test_profile_lessons_become_pitfalls_and_traits():
    lessons = {
        "site_traits": {"platform": "WordPress", "list_pattern": "ul/li"},
        "failure_analysis": {"fix_direction": "use article-row container"},
        "optimization": ["跳过 enhanced_page_analysis 这一步", "复用 verify_selector 结果"],
    }
    p = update_profile_from_episode(None, _ep(verdict="wrong", lessons=lessons))
    assert p["site_traits"]["platform"] == "WordPress"
    assert "use article-row container" in p["known_pitfalls"]
    assert any("跳过 enhanced_page_analysis" in s for s in p["known_pitfalls"])


# ---------------------------------------------------------------------------
# Module C — pitfall hygiene (selector advice filter + same-slot dedup)
# ---------------------------------------------------------------------------


def test_pitfall_drops_raw_selector_advice():
    """fix_direction containing a CSS class should NOT enter known_pitfalls.

    Concrete selector advice belongs in candidate_selectors / blacklist,
    not in the freeform pitfalls list (otherwise the planner sees two
    competing 'authoritative' suggestions next run).
    """
    lessons = {
        "failure_analysis": {
            "fix_direction": "改用 .elementor-widget-theme-post-content 作为正文容器",
        },
    }
    p = update_profile_from_episode(None, _ep(verdict="wrong", lessons=lessons))
    pitfalls = p.get("known_pitfalls") or []
    assert all(".elementor-widget-theme-post-content" not in s for s in pitfalls), (
        f"Selector-shaped advice leaked into pitfalls: {pitfalls}"
    )


def test_pitfall_keeps_abstract_advice():
    """Abstract experience (no selector) survives the filter."""
    lessons = {
        "failure_analysis": {
            "fix_direction": "this site needs Playwright (heavy JS rendering)",
        },
    }
    p = update_profile_from_episode(None, _ep(verdict="wrong", lessons=lessons))
    assert any("Playwright" in s for s in (p.get("known_pitfalls") or []))


def test_pitfall_same_slot_dedupes_replacing_old():
    """Two consecutive pitfalls about ``detail.content`` → only the
    newer one survives. Stops the 'two contradictory advice notes
    stacked at the top of the prompt' failure mode.
    """
    p1 = update_profile_from_episode(None, _ep(
        verdict="wrong",
        lessons={"failure_analysis": {
            "fix_direction": "detail.content 取到的是图标，应该改用文章正文容器"
        }},
    ))
    p2 = update_profile_from_episode(p1, _ep(
        verdict="wrong",
        lessons={"failure_analysis": {
            "fix_direction": "detail.content 选错了，要用主内容区"
        }},
    ))
    pitfalls = p2.get("known_pitfalls") or []
    # Old advice about detail.content gone, new one present.
    assert not any("应该改用文章正文容器" in s for s in pitfalls)
    assert any("要用主内容区" in s for s in pitfalls)


def test_pitfall_different_slots_both_kept():
    """Pitfalls about *different* slots should accumulate independently."""
    p1 = update_profile_from_episode(None, _ep(
        verdict="wrong",
        lessons={"failure_analysis": {
            "fix_direction": "list.title 抓错了"
        }},
    ))
    p2 = update_profile_from_episode(p1, _ep(
        verdict="wrong",
        lessons={"failure_analysis": {
            "fix_direction": "detail.content 抓到了图标"
        }},
    ))
    pitfalls = p2.get("known_pitfalls") or []
    assert any("list.title" in s for s in pitfalls)
    assert any("detail.content" in s for s in pitfalls)


def test_pitfall_optimization_tips_run_through_filter():
    """``optimization`` entries also get pitfall-merged (selector filter +
    same-slot dedup), since they share the same surface area."""
    lessons = {
        "optimization": [
            ".my-class > a 选择器太脆弱",   # selector-ish → dropped
            "跳过 enhanced_page_analysis 这一步",  # abstract → kept
        ],
    }
    p = update_profile_from_episode(None, _ep(verdict="wrong", lessons=lessons))
    pitfalls = p.get("known_pitfalls") or []
    assert all(".my-class > a" not in s for s in pitfalls)
    assert any("enhanced_page_analysis" in s for s in pitfalls)


# ---------------------------------------------------------------------------
# Slot-level verdicts (Stage-2 LLM precise error attribution)
# ---------------------------------------------------------------------------


def test_slot_verdicts_split_task_verdict_per_slot():
    """Stage-2 LLM marks detail.content=wrong but list.* + detail.title=correct.

    Real-world case: 标题对、正文是图标。Task verdict=wrong, but only
    detail.content's selector should accumulate a loss; the other 4 slots
    that worked correctly should accumulate wins (NOT inherit the
    task-level wrong).
    """
    selectors = {
        "list": {
            "container": ".list-card",
            "title": "a.headline",
            "title_link": "a.headline",
        },
        "detail": {
            "title": "h1.post-title",
            "content": ".elementor-section-wrap",  # ← the bad one
        },
    }
    lessons = {
        "slot_verdicts": {
            "list.container": "correct",
            "list.title": "correct",
            "list.title_link": "correct",
            "detail.title": "correct",
            "detail.content": "wrong",  # ← only this one
        },
    }
    p = update_profile_from_episode(
        None,
        _ep(verdict="wrong", selectors=selectors, lessons=lessons),
    )
    cands = p["candidate_selectors"]
    # The 4 healthy slots get +1 win each
    assert cands["list.container::.list-card"]["wins"] == 1
    assert cands["list.container::.list-card"]["losses"] == 0
    assert cands["list.title::a.headline"]["wins"] == 1
    assert cands["detail.title::h1.post-title"]["wins"] == 1
    # The bad one gets +1 loss
    assert cands["detail.content::.elementor-section-wrap"]["wins"] == 0
    assert cands["detail.content::.elementor-section-wrap"]["losses"] == 1
    assert cands["detail.content::.elementor-section-wrap"]["consecutive_losses"] == 1


def test_slot_verdicts_unknown_does_not_touch_counters():
    """LLM intentionally passes on a slot → no counter movement at all."""
    selectors = {"list": {"title_link": "a.x"}}
    lessons = {"slot_verdicts": {"list.title_link": "unknown"}}
    p = update_profile_from_episode(
        None,
        _ep(verdict="wrong", selectors=selectors, lessons=lessons),
    )
    # Entry is created (so it's visible), but wins/losses both stay 0.
    entry = p["candidate_selectors"]["list.title_link::a.x"]
    assert entry["wins"] == 0
    assert entry["losses"] == 0
    assert entry["consecutive_losses"] == 0
    assert entry["last_verdict"] is None


def test_slot_verdicts_missing_falls_back_to_task_level():
    """No slot_verdicts in lessons → legacy behaviour: push task verdict
    down to every verified slot. Backward compat for pre-Stage-2 episodes
    and for the rule-based fallback path (no LLM available)."""
    selectors = {"list": {"title_link": "a.x"}, "detail": {"content": ".y"}}
    p = update_profile_from_episode(None, _ep(verdict="wrong", selectors=selectors))
    cands = p["candidate_selectors"]
    assert cands["list.title_link::a.x"]["losses"] == 1
    assert cands["detail.content::.y"]["losses"] == 1


def test_slot_verdicts_consecutive_resets_on_correct():
    """A correct verdict for a slot resets its consecutive_losses streak."""
    selectors = {"detail": {"content": ".bad"}}
    # Two consecutive losses
    p = update_profile_from_episode(
        None,
        _ep(verdict="wrong", selectors=selectors, lessons={"slot_verdicts": {"detail.content": "wrong"}}),
    )
    p = update_profile_from_episode(
        p,
        _ep(verdict="wrong", selectors=selectors, lessons={"slot_verdicts": {"detail.content": "wrong"}}),
    )
    entry = p["candidate_selectors"]["detail.content::.bad"]
    assert entry["losses"] == 2
    assert entry["consecutive_losses"] == 2
    # One win in between → counter resets
    p = update_profile_from_episode(
        p,
        _ep(verdict="correct", selectors=selectors, lessons={"slot_verdicts": {"detail.content": "correct"}}),
    )
    entry = p["candidate_selectors"]["detail.content::.bad"]
    assert entry["losses"] == 2  # losses don't decrement
    assert entry["wins"] == 1
    assert entry["consecutive_losses"] == 0  # streak broken
    assert entry["last_verdict"] == "correct"


def test_slot_verdicts_unknown_value_is_ignored_in_sanitizer():
    """An invalid value in slot_verdicts (e.g. 'maybe') falls through to
    the task-level verdict via the no-effective-signal branch."""
    # We test the site_profile branch directly here; sanitizer-level coverage
    # lives next to the commit.py tests but exercising end-to-end is fine.
    selectors = {"list": {"title_link": "a.x"}}
    # Deliberately use a value the schema doesn't allow. The renderer code
    # in site_profile only acts on the closed enum, so this should fall
    # back to the task verdict (=wrong).
    lessons = {"slot_verdicts": {"list.title_link": "maybe"}}
    p = update_profile_from_episode(
        None,
        _ep(verdict="wrong", selectors=selectors, lessons=lessons),
    )
    entry = p["candidate_selectors"]["list.title_link::a.x"]
    assert entry["losses"] == 1


def test_apply_time_decay_reduces_confidence_over_time():
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.8
    # Force last_updated_at into the past
    past = datetime.now(timezone.utc) - timedelta(days=60)
    p["last_updated_at"] = past.isoformat()
    decayed = apply_time_decay(p, decay_per_30d=0.1)
    assert decayed["confidence"] < 0.8
    # ~60 days × 0.1/30d ≈ 0.2 drop
    assert decayed["confidence"] == pytest.approx(0.6, abs=0.05)


# ---------------------------------------------------------------------------
# fingerprint.py
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_text_changes():
    """Same skeleton + same text-length bucket → same fingerprint.

    The fingerprint quantizes text length into log-ish buckets (xs/s/m/l/xl),
    so minor wording changes within the same bucket do not perturb the hash.
    """
    html_a = """
    <html><body>
      <ul class="list">
        <li><a href="/a">Story</a></li>
        <li><a href="/b">Story</a></li>
      </ul>
    </body></html>
    """
    html_b = """
    <html><body>
      <ul class="list">
        <li><a href="/x">Story</a></li>
        <li><a href="/y">Story</a></li>
      </ul>
    </body></html>
    """
    fp_a = compute_list_page_fingerprint(html_a)
    fp_b = compute_list_page_fingerprint(html_b)
    assert fp_a == fp_b
    assert fp_a.startswith("sha256:")


def test_fingerprint_changes_on_structural_drift():
    html_a = "<html><body><ul class='list'><li><a>x</a></li></ul></body></html>"
    html_b = "<html><body><div class='cards'><article><a>x</a></article></div></body></html>"
    assert compute_list_page_fingerprint(html_a) != compute_list_page_fingerprint(html_b)


def test_fingerprint_empty_returns_marker():
    fp = compute_list_page_fingerprint("")
    assert fp.startswith("sha256:empty") or fp.startswith("sha256:")


# ---------------------------------------------------------------------------
# auto_findings.py
# ---------------------------------------------------------------------------


def test_auto_findings_detect_duplicate_tool_calls():
    state = {
        "tool_calls_log": [
            {"action": "verify_selector", "success": True, "action_input": {"selector": "a.x"}},
            {"action": "verify_selector", "success": True, "action_input": {"selector": "a.x"}},
        ],
    }
    out = run_auto_findings(state)
    assert any("verify_selector" in s for s in out["redundant_tool_calls"])


def test_auto_findings_flag_seczambia_smell():
    code = """
    items = page.locator('div.item').all()
    for item in items:
        link = item.locator('a').first.get_attribute('href')
        url = urljoin(base, link or '')
    """
    out = run_auto_findings({"generated_code": code, "tool_calls_log": []})
    assert any("回退到列表页地址" in s or "图标" in s for s in out["suspected_failures"])


def test_auto_findings_returns_stable_buckets_on_empty_state():
    out = run_auto_findings({})
    assert set(out.keys()) == {
        "redundant_tool_calls",
        "suspected_failures",
        "redundant_code_blocks",
    }
    assert all(out[k] == [] for k in out)


# ---------------------------------------------------------------------------
# render.py
# ---------------------------------------------------------------------------


def test_should_inject_profile_skips_low_confidence():
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.1
    assert should_inject_profile(p, min_confidence=0.3) is False


def test_render_site_memory_hint_emits_must_re_verify_block():
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.8
    p["wins"] = 5
    p["stable_selectors"] = {
        "list.title_link": {"selector": "a.story-link", "wins": 4, "losses": 0, "winrate": 1.0}
    }
    md = render_site_memory_hint(p)
    assert "[过往经验提示]" in md
    assert "must re-verify" in md or "verify_selector" in md
    assert "a.story-link" in md
    # Confidence visible to the planner
    assert "0.80" in md


def test_render_site_memory_hint_quarantine_blocks_selectors():
    p = SiteProfile.empty("bad.com")
    p["confidence"] = 0.9
    p["quarantined"] = True
    p["stable_selectors"] = {"list.title_link": {"selector": "a.x", "wins": 9, "losses": 1, "winrate": 0.9}}
    md = render_site_memory_hint(p)
    assert "WARNING" in md
    # Selectors must NOT leak into a quarantined hint
    assert "a.x" not in md


def test_render_site_memory_hint_drift_warning():
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["has_drift"] = True
    p["stable_selectors"] = {"list.title_link": {"selector": "a.s", "wins": 5, "losses": 0, "winrate": 1.0}}
    md = render_site_memory_hint(p)
    assert "DRIFT" in md
    assert "a.s" in md  # selectors still shown but with warning


def test_render_feedback_replay_hint_includes_user_suggestion_and_findings():
    prev = {
        "user_suggestion": "只爬到一堆图标！",
        "auto_findings": {
            "suspected_failures": ["urljoin 把所有 sourceUrl 写成了列表页"],
            "redundant_tool_calls": [],
            "redundant_code_blocks": [],
        },
    }
    md = render_feedback_replay_hint(prev)
    assert "[反馈回放]" in md
    assert "最高优先级" in md
    assert "只爬到一堆图标" in md
    assert "urljoin" in md


def test_render_feedback_replay_hint_returns_empty_for_empty_input():
    assert render_feedback_replay_hint(None) == ""
    assert render_feedback_replay_hint({}) == ""
    assert render_feedback_replay_hint({"user_suggestion": "", "auto_findings": {}}) == ""


# ---------------------------------------------------------------------------
# Failed-selector blacklist (render side)
# ---------------------------------------------------------------------------


def test_render_site_memory_hint_emits_blacklist_for_repeatedly_failed_selectors():
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["wins"] = 1
    p["losses"] = 5
    p["candidate_selectors"] = {
        "list.title_link": {"selector": ".bad-icon", "wins": 0, "losses": 4, "winrate": 0.0},
        # Below threshold (only 1 loss) — must NOT appear.
        "list.title": {"selector": ".meh", "wins": 0, "losses": 1, "winrate": 0.0},
        # Above winrate cap (0.5 > 0.2) — must NOT appear.
        "list.date": {"selector": ".flaky", "wins": 2, "losses": 2, "winrate": 0.5},
    }
    md = render_site_memory_hint(p)
    assert "历史上验证失败的选择器" in md
    assert ".bad-icon" in md
    assert ".meh" not in md  # filtered: losses < min_losses
    assert ".flaky" not in md  # filtered: winrate > cap


def test_render_site_memory_hint_blacklist_visible_under_quarantine():
    p = SiteProfile.empty("bad.com")
    p["confidence"] = 0.9
    p["quarantined"] = True
    # Positive selectors must NOT leak under quarantine, but blacklist still does.
    p["stable_selectors"] = {
        "list.title_link": {"selector": "a.x", "wins": 9, "losses": 1, "winrate": 0.9},
    }
    p["candidate_selectors"] = {
        "list.title": {"selector": ".trash", "wins": 0, "losses": 3, "winrate": 0.0},
    }
    md = render_site_memory_hint(p)
    assert "WARNING" in md
    assert "a.x" not in md           # positive selectors stay sealed
    assert "历史上验证失败的选择器" in md
    assert ".trash" in md            # blacklist promoted under quarantine


def test_should_inject_profile_triggers_on_blacklist_alone():
    """Profile with only failures (no stable / traits / pitfalls) still injects."""
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.1  # below default min_confidence — would normally skip
    p["candidate_selectors"] = {
        "list.title_link": {"selector": ".garbage", "wins": 0, "losses": 4, "winrate": 0.0},
    }
    assert should_inject_profile(p, min_confidence=0.3) is True
    md = render_site_memory_hint(p, min_confidence=0.3)
    assert ".garbage" in md


def test_should_inject_profile_blacklist_threshold_respected():
    """Below the loss threshold the blacklist is empty — no injection."""
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.1
    p["candidate_selectors"] = {
        "list.title_link": {"selector": ".meh", "wins": 0, "losses": 1, "winrate": 0.0},
    }
    assert should_inject_profile(p, min_confidence=0.3, blacklist_min_losses=2) is False


def test_render_site_memory_hint_blacklist_caps_to_max():
    from memory.site_profile import MAX_BLACKLIST_SELECTORS  # local import to avoid noise

    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["candidate_selectors"] = {
        f"list.slot_{i}": {"selector": f".x{i}", "wins": 0, "losses": 5 + i, "winrate": 0.0}
        for i in range(MAX_BLACKLIST_SELECTORS + 5)
    }
    md = render_site_memory_hint(p)
    blacklist_lines = [ln for ln in md.splitlines() if ln.startswith("- `list.slot_")]
    assert len(blacklist_lines) == MAX_BLACKLIST_SELECTORS
    # The highest-loss entries should win the cap; the very lowest losses are dropped.
    assert ".x0" not in md  # losses=5, lowest → trimmed when cap is small enough
    assert f".x{MAX_BLACKLIST_SELECTORS + 4}" in md  # losses=highest → kept


def test_blacklist_require_consecutive_excludes_broken_streaks():
    """A selector with losses=2 but a win sandwiched in (consecutive_losses=1)
    is NOT eligible for the blacklist when require_consecutive=True. This is
    the explicit Stage-2 LLM-misjudgment safety net — one stray wrong from
    the LLM mustn't ship a healthy selector to the blacklist if a real
    correct verdict has already broken the streak.
    """
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["candidate_selectors"] = {
        # Total losses = 2 → would normally pass the gate.
        # But consecutive_losses = 1 → fails the consecutive gate.
        "list.title_link": {
            "selector": ".healthy-but-misjudged-once",
            "wins": 1,
            "losses": 2,
            "consecutive_losses": 1,
            "winrate": 0.33,
        },
    }
    md_strict = render_site_memory_hint(
        p,
        blacklist_min_losses=2,
        blacklist_max_winrate=0.5,
        blacklist_require_consecutive=True,
    )
    # winrate exceeds 0.2 default but we passed 0.5 — gate must reject for
    # consecutive reason, not winrate. Hint may render other sections, but
    # this selector must NOT appear under the blacklist.
    assert ".healthy-but-misjudged-once" not in md_strict


def test_blacklist_require_consecutive_passes_unbroken_streak():
    """Same selector with consecutive_losses=2 (unbroken streak) DOES enter
    the blacklist. This is the genuine "二次失败 + 没有 win 救场" → 拉黑."""
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["candidate_selectors"] = {
        "list.title_link": {
            "selector": ".genuinely-broken",
            "wins": 0,
            "losses": 2,
            "consecutive_losses": 2,
            "winrate": 0.0,
        },
    }
    md = render_site_memory_hint(
        p,
        blacklist_min_losses=2,
        blacklist_max_winrate=0.2,
        blacklist_require_consecutive=True,
    )
    assert "历史上验证失败的选择器" in md
    assert ".genuinely-broken" in md


def test_blacklist_require_consecutive_off_keeps_legacy_behaviour():
    """When the gate is disabled the old "总数累计" rule applies even if
    the streak is broken. This is the off-switch users can flip from
    config.yaml if they decide LLM误判 is unrealistically rare for them."""
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["candidate_selectors"] = {
        "list.title_link": {
            "selector": ".sandwiched",
            "wins": 1,
            "losses": 2,
            "consecutive_losses": 1,
            "winrate": 0.33,
        },
    }
    md = render_site_memory_hint(
        p,
        blacklist_min_losses=2,
        blacklist_max_winrate=0.5,  # passes winrate gate
        blacklist_require_consecutive=False,  # ← legacy
    )
    assert ".sandwiched" in md


def test_blacklist_require_consecutive_back_compat_no_field():
    """Old profiles (created before consecutive_losses was tracked) must
    still work. The collector should treat ``consecutive_losses == losses``
    so legacy entries with raw losses≥N continue to enter the blacklist."""
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    p["candidate_selectors"] = {
        # Notice: NO `consecutive_losses` field at all (legacy profile).
        "list.title_link": {
            "selector": ".legacy-broken",
            "wins": 0,
            "losses": 3,
            "winrate": 0.0,
        },
    }
    md = render_site_memory_hint(
        p,
        blacklist_min_losses=2,
        blacklist_max_winrate=0.2,
        blacklist_require_consecutive=True,
    )
    assert ".legacy-broken" in md


# ---------------------------------------------------------------------------
# Multi-hop rerun chain
# ---------------------------------------------------------------------------


def test_walk_rerun_chain_walks_drafts_and_committed(store):
    """Mix of draft + committed episodes along a rerun chain."""
    # Oldest hop lives in committed log.
    store.append_committed_episode({
        "task_id": "tid-A",
        "user_suggestion": "第一次：图标爬错了",
        "user_verdict": "wrong",
        "auto_findings": {},
        "rerun_of": None,
    })
    # Middle hop: also committed.
    store.append_committed_episode({
        "task_id": "tid-B",
        "user_suggestion": "还是图标，没正文",
        "user_verdict": "wrong",
        "auto_findings": {},
        "rerun_of": "tid-A",
    })
    # Newest: still a draft (user hasn't evaluated yet).
    store.write_draft({
        "task_id": "tid-C",
        "user_suggestion": "依然不对",
        "user_verdict": None,
        "auto_findings": {"suspected_failures": ["还是 .icon"]},
        "rerun_of": "tid-B",
    })

    chain = walk_rerun_chain(store, "tid-C", max_hops=5)
    assert [ep["task_id"] for ep in chain] == ["tid-C", "tid-B", "tid-A"]


def test_walk_rerun_chain_respects_max_hops(store):
    store.append_committed_episode({"task_id": "t1", "rerun_of": None})
    store.append_committed_episode({"task_id": "t2", "rerun_of": "t1"})
    store.append_committed_episode({"task_id": "t3", "rerun_of": "t2"})
    store.append_committed_episode({"task_id": "t4", "rerun_of": "t3"})
    chain = walk_rerun_chain(store, "t4", max_hops=2)
    assert [ep["task_id"] for ep in chain] == ["t4", "t3"]


def test_walk_rerun_chain_handles_cycle(store):
    # Pathological: someone wrote a circular rerun_of pointer.
    store.append_committed_episode({"task_id": "x1", "rerun_of": "x2"})
    store.append_committed_episode({"task_id": "x2", "rerun_of": "x1"})
    chain = walk_rerun_chain(store, "x1", max_hops=10)
    assert [ep["task_id"] for ep in chain] == ["x1", "x2"]


def test_walk_rerun_chain_returns_empty_for_unknown_id(store):
    assert walk_rerun_chain(store, "does-not-exist", max_hops=3) == []
    assert walk_rerun_chain(store, None, max_hops=3) == []
    assert walk_rerun_chain(None, "any", max_hops=3) == []


def test_render_feedback_replay_hint_multi_hop_full_then_compact():
    chain = [
        {
            "task_id": "task-NEW-aaaaaaaaaa",
            "user_verdict": "wrong",
            "user_suggestion": "最新：图标还是没换",
            "auto_findings": {"suspected_failures": ["urljoin 错配"]},
        },
        {
            "task_id": "task-MID-bbbbbbbbbb",
            "user_verdict": "wrong",
            "user_suggestion": "上一次：选择器选到了图标",
            "lessons": {"failure_analysis": {"root_cause_guess": "误把 .icon 当 .title"}},
        },
        {
            "task_id": "task-OLD-cccccccccc",
            "user_verdict": "wrong",
            "user_suggestion": "更早：抓不到任何标题",
            "auto_findings": {},
        },
    ]
    md = render_feedback_replay_hint(chain)

    assert "[反馈回放]" in md
    assert "Hop 1/3" in md and "Hop 2/3" in md and "Hop 3/3" in md
    # latest hop full: shows auto_findings header
    assert "模型自检报告" in md
    assert "urljoin 错配" in md
    # older hops compact: shorter user_suggestion line, no findings header repeated
    assert "选择器选到了图标" in md
    assert "误把 .icon 当 .title" in md
    assert md.count("模型自检报告") == 1


def test_render_feedback_replay_hint_accepts_single_dict_legacy():
    md = render_feedback_replay_hint({
        "user_suggestion": "只爬到一堆图标！",
        "auto_findings": {"suspected_failures": ["urljoin 错"]},
    })
    assert "[反馈回放]" in md
    assert "Hop 1/1" in md
    assert "只爬到一堆图标" in md


def test_render_feedback_replay_hint_skips_empty_hops_in_chain():
    chain = [
        {"user_suggestion": "实质内容"},
        {"user_suggestion": "", "auto_findings": {}},  # empty → dropped
        {
            "lessons": {"failure_analysis": {"fix_direction": "走 detail 页"}},
        },
    ]
    md = render_feedback_replay_hint(chain)
    # Two non-empty hops should remain.
    assert "Hop 1/2" in md and "Hop 2/2" in md
    assert "走 detail 页" in md


# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------


def test_store_draft_round_trip(store):
    ep = {"task_id": "tid001", "url": "https://example.com", "domain": "example.com"}
    path = store.write_draft(ep)
    assert path is not None and path.exists()
    loaded = store.read_draft("tid001")
    assert loaded is not None
    assert loaded["task_id"] == "tid001"
    assert loaded["committed"] is False


def test_store_rejects_unsafe_task_id(store):
    assert store.write_draft({"task_id": "../escape"}) is None
    assert store.read_draft("../escape") is None


def test_store_delete_draft(store):
    store.write_draft({"task_id": "tid002"})
    assert store.delete_draft("tid002") is True
    assert store.read_draft("tid002") is None


def test_store_append_committed_and_iter(store):
    for i in range(3):
        ok = store.append_committed_episode({
            "task_id": f"t{i}",
            "domain": "example.com",
            "user_verdict": "correct",
        })
        assert ok is True
    rows = list(store.iter_committed_episodes())
    assert len(rows) == 3
    assert all(r["committed"] is True for r in rows)
    assert all(r.get("committed_at") for r in rows)


def test_store_ring_buffer_trims_old_rows(store):
    """The ring buffer kicks in only when len > max*1.1 (= 11 for max=10),
    and then trims back to ``max_episodes`` rows. We assert the property
    we actually care about: after enough writes, the file size stays
    bounded near the cap, never grows unbounded, and the most recent
    rows are preserved."""
    for i in range(15):
        store.append_committed_episode({
            "task_id": f"t{i}",
            "domain": "example.com",
            "user_verdict": "correct",
        })
    rows = list(store.iter_committed_episodes())
    assert len(rows) <= int(store.max_episodes * 1.1) + 1
    assert rows[-1]["task_id"] == "t14"  # most recent preserved
    # Oldest rows must be evicted: t0..t3 cannot all still be present
    surviving_ids = {r["task_id"] for r in rows}
    assert "t14" in surviving_ids
    assert "t0" not in surviving_ids


def test_store_site_round_trip(store):
    p = SiteProfile.empty("example.com")
    p["confidence"] = 0.7
    assert store.write_site(p) is True
    loaded = store.lookup_site("example.com")
    assert loaded is not None
    assert loaded["confidence"] == pytest.approx(0.7)
    # Markdown sidecar should also be written
    md_path = store.root / "site" / "example.com.md"
    assert md_path.exists()
    assert "example.com" in md_path.read_text(encoding="utf-8")


def test_store_quarantine_marker_written(store):
    p = SiteProfile.empty("bad.com")
    p["quarantined"] = True
    store.write_site(p)
    qpath = store.root / "site" / "_quarantine" / "bad.com.json"
    assert qpath.exists()


def test_store_corrupt_profile_self_heals(store):
    p = SiteProfile.empty("example.com")
    store.write_site(p)
    json_path = store.root / "site" / "example.com.json"
    json_path.write_text("not-json{{", encoding="utf-8")
    # Bust the in-memory cache so we actually re-read from disk
    store._site_cache.pop("example.com", None)
    assert store.lookup_site("example.com") is None
    # Backup should now exist
    bak_files = list((store.root / "site").glob("example.com.json.bak.*"))
    assert bak_files, "expected a .bak file after corruption"


def test_store_gc_pending_removes_old_drafts(store, tmp_path):
    store.write_draft({"task_id": "old001"})
    pending_path = store.root / "episode" / "pending" / "old001.json"
    assert pending_path.exists()
    # Force mtime 30 days in the past
    old_ts = time.time() - 30 * 86400
    import os
    os.utime(pending_path, (old_ts, old_ts))
    removed = store.gc_pending(older_than_days=7)
    assert removed == 1
    assert not pending_path.exists()


def test_store_lookup_site_missing_returns_none(store):
    assert store.lookup_site("nope.com") is None
    assert store.lookup_site("../bad") is None


# ---------------------------------------------------------------------------
# Integration: profile update → store → render round trip
# ---------------------------------------------------------------------------


def test_end_to_end_episode_to_hint(store):
    selectors = {"list": {"title_link": "a.story"}}
    p = None
    for _ in range(3):
        p = update_profile_from_episode(p, _ep(verdict="correct", selectors=selectors))
    store.write_site(p)
    loaded = store.lookup_site("example.com")
    md = render_site_memory_hint(loaded)
    assert "a.story" in md
    assert "[过往经验提示]" in md


# ---------------------------------------------------------------------------
# Hole-2.A: walk_rerun_chain domain guard
# ---------------------------------------------------------------------------


def test_walk_rerun_chain_drops_hop_with_mismatching_domain(store):
    """Cross-domain rerun_of links must abort the chain — not return the
    cross-site episode and pretend it's relevant.
    """
    store.append_committed_episode({
        "task_id": "tA-aaaa",
        "domain": "site-a.com",
        "user_verdict": "wrong",
        "rerun_of": None,
    })
    # An attacker / corrupt batch run linked tB (domain B) to tA (domain A).
    store.append_committed_episode({
        "task_id": "tB-bbbb",
        "domain": "site-b.com",
        "user_verdict": "wrong",
        "rerun_of": "tA-aaaa",
    })

    chain = walk_rerun_chain(
        store, "tB-bbbb", max_hops=5, expected_domain="site-b.com"
    )
    # The head matches site-b → kept. The next hop (site-a) → bail out.
    assert [ep["task_id"] for ep in chain] == ["tB-bbbb"]


def test_walk_rerun_chain_returns_empty_when_head_domain_mismatches(store):
    """If the very first lookup is a different domain, return nothing
    (we'd rather inject zero hint than the wrong site's hint).
    """
    store.append_committed_episode({
        "task_id": "wrong-domain-task",
        "domain": "other.com",
        "user_verdict": "wrong",
        "rerun_of": None,
    })

    chain = walk_rerun_chain(
        store,
        "wrong-domain-task",
        max_hops=3,
        expected_domain="current-task.com",
    )
    assert chain == []


def test_walk_rerun_chain_no_expected_domain_keeps_legacy_behaviour(store):
    """Backward compat: when expected_domain is None we return everything
    just like before — no silent breakage for callers that don't pass it.
    """
    store.append_committed_episode({
        "task_id": "legacy-1",
        "domain": "any.com",
        "rerun_of": None,
    })
    chain = walk_rerun_chain(store, "legacy-1", max_hops=3)
    assert [ep["task_id"] for ep in chain] == ["legacy-1"]


# ---------------------------------------------------------------------------
# Hole-2.A.fallback: domain-based auto-discovery of prev_task_id
# ---------------------------------------------------------------------------


def test_find_recent_task_id_prefers_pending_drafts_over_committed(store):
    """Drafts hold the freshest signal — they should outrank committed
    episodes from the same domain.
    """
    store.append_committed_episode({
        "task_id": "old-committed",
        "domain": "example.com",
        "user_verdict": "correct",
        "rerun_of": None,
    })
    # Slight sleep ensures the draft's mtime is strictly later than any
    # filesystem operation done above (Windows mtime granularity ~10ms).
    time.sleep(0.05)
    store.write_draft({
        "task_id": "fresh-draft",
        "domain": "example.com",
        "user_verdict": None,
        "auto_findings": {"suspected_failures": ["x"]},
    })

    found = find_recent_task_id_for_domain(
        store, "example.com", max_age_days=30
    )
    assert found == "fresh-draft"


def test_find_recent_task_id_falls_back_to_committed_when_no_draft(store):
    store.append_committed_episode({
        "task_id": "committed-only",
        "domain": "example.com",
        "user_verdict": "wrong",
        "rerun_of": None,
    })

    found = find_recent_task_id_for_domain(store, "example.com")
    assert found == "committed-only"


def test_find_recent_task_id_ignores_other_domains(store):
    store.write_draft({
        "task_id": "wrong-site",
        "domain": "other.com",
        "user_verdict": None,
    })
    store.append_committed_episode({
        "task_id": "wrong-site-committed",
        "domain": "yet-another.com",
        "user_verdict": "wrong",
    })

    assert find_recent_task_id_for_domain(store, "example.com") is None


def test_find_recent_task_id_respects_max_age(store, tmp_path):
    """Drafts older than max_age_days are skipped."""
    store.write_draft({
        "task_id": "ancient-draft",
        "domain": "example.com",
        "user_verdict": None,
    })
    # Force the draft's mtime back ~30 days.
    pending = tmp_path / "memory" / "episode" / "pending" / "ancient-draft.json"
    assert pending.exists()
    backdated = time.time() - (30 * 86400)
    import os as _os

    _os.utime(pending, (backdated, backdated))

    # Default age-window is 14d → no draft, no committed → None.
    assert find_recent_task_id_for_domain(store, "example.com") is None
    # Generous window → returns the draft.
    assert (
        find_recent_task_id_for_domain(
            store, "example.com", max_age_days=60
        )
        == "ancient-draft"
    )


def test_find_recent_task_id_handles_empty_or_invalid_inputs(store):
    assert find_recent_task_id_for_domain(store, "") is None
    assert find_recent_task_id_for_domain(None, "example.com") is None


# ---------------------------------------------------------------------------
# Hole-1.b: fallback lessons must surface user_suggestion as a pitfall
# ---------------------------------------------------------------------------


def test_fallback_lessons_pushes_user_suggestion_into_pitfalls():
    """When the LLM fails entirely, the user's plain-English complaint
    must reach ``site_profile.known_pitfalls`` so the next run on the
    same domain inherits *something* concrete.
    """
    from memory.commit import _fallback_lessons  # noqa: WPS433 (private API on purpose)

    draft = {
        "user_verdict": "wrong",
        "user_suggestion": "只爬到一堆图标，没有正文",
        "auto_findings": {"redundant_tool_calls": ["tool A 调用了 4 次"]},
    }
    lessons = _fallback_lessons(draft)
    fa = lessons.get("failure_analysis")
    assert isinstance(fa, dict)
    # user_complaint_interpreted captures the raw words for traceability.
    assert "只爬到一堆图标" in (fa.get("user_complaint_interpreted") or "")
    # root_cause_guess + fix_direction are the keys that
    # update_profile_from_episode harvests into known_pitfalls — both
    # MUST carry the user's text so the next run sees the warning.
    assert "只爬到一堆图标" in (fa.get("root_cause_guess") or "")
    assert "只爬到一堆图标" in (fa.get("fix_direction") or "")


def test_fallback_lessons_still_safe_when_suggestion_empty():
    """No suggestion + verdict=wrong → empty placeholders, no crash."""
    from memory.commit import _fallback_lessons

    lessons = _fallback_lessons({"user_verdict": "wrong", "user_suggestion": ""})
    fa = lessons.get("failure_analysis")
    assert fa == {
        "user_complaint_interpreted": "",
        "root_cause_guess": "",
        "fix_direction": "",
    }


def test_fallback_pitfall_propagates_into_site_profile():
    """End-to-end: fallback lessons → update_profile → known_pitfalls."""
    from memory.commit import _fallback_lessons

    draft = {
        "domain": "example.com",
        "url": "https://example.com/news",
        "user_verdict": "wrong",
        "user_suggestion": "只爬到一堆图标",
        "auto_findings": {},
        "verified_selectors": {},
    }
    draft["lessons"] = _fallback_lessons(draft)

    profile = update_profile_from_episode(None, draft)
    pitfalls = profile.get("known_pitfalls") or []
    # Both fix_direction and root_cause_guess from the fallback should
    # have made it through; at minimum the user's words must appear.
    assert any("只爬到一堆图标" in p for p in pitfalls), pitfalls


# ---------------------------------------------------------------------------
# Stage-2 LLM output sanitiser: slot_verdicts must be locked-down to the
# closed enum + canonical slot list before reaching site_profile.
# ---------------------------------------------------------------------------


def test_validate_lessons_keeps_valid_slot_verdicts():
    from memory.commit import _validate_lessons

    raw = {
        "failure_analysis": {
            "user_complaint_interpreted": "只爬到图标",
            "root_cause_guess": "detail.content 选了外层 wrapper",
            "fix_direction": "改用 .elementor-widget-theme-post-content",
        },
        "optimization": ["跳过 enhanced_page_analysis"],
        "site_traits": {"platform": "WordPress + Elementor"},
        "slot_verdicts": {
            "list.title": "correct",
            "detail.content": "wrong",
            "detail.title": "unknown",
        },
    }
    out = _validate_lessons(raw, verdict="wrong")
    assert out["slot_verdicts"] == {
        "list.title": "correct",
        "detail.content": "wrong",
        "detail.title": "unknown",
    }


def test_validate_lessons_drops_garbage_slot_verdicts():
    """Hallucinated slots, illegal values, weird casing — all filtered."""
    from memory.commit import _validate_lessons

    raw = {
        "optimization": ["x"],
        "slot_verdicts": {
            "list.NOT_A_SLOT": "wrong",      # bogus slot name → drop
            "detail.content": "MAYBE",        # bogus verdict → drop
            "DETAIL.TITLE": "correct",        # wrong case in slot key → drop
            "list.container": "Correct",      # value case OK after lowercase
            "list.title": 42,                 # non-string value → drop
        },
    }
    out = _validate_lessons(raw, verdict="correct")
    assert out["slot_verdicts"] == {"list.container": "correct"}


def test_validate_lessons_omits_slot_verdicts_when_empty_or_absent():
    """Absent / all-garbage slot_verdicts → omit the key entirely so that
    update_profile_from_episode falls back to task-level verdict."""
    from memory.commit import _validate_lessons

    out_none = _validate_lessons({"optimization": ["x"]}, verdict="correct")
    assert "slot_verdicts" not in out_none

    out_empty = _validate_lessons(
        {"optimization": ["x"], "slot_verdicts": {}}, verdict="correct"
    )
    assert "slot_verdicts" not in out_empty

    out_garbage = _validate_lessons(
        {"optimization": ["x"], "slot_verdicts": {"bogus.slot": "wrong"}},
        verdict="correct",
    )
    assert "slot_verdicts" not in out_garbage
