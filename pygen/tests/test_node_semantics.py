"""Tests for the multi-signal icon-likelihood scorer.

The whole point of this scorer is to fix the very specific failure
mode where ``verify_selector`` and ``probe_detail_page`` recommend an
icon-list container as if it were the news content. So the test
matrix is built around three families of inputs:

1. Real-world icon containers (Elementor icon list, Font Awesome,
   Material Icons) — must score >= 0.6 → ``icon_container``.
2. Real news content (long paragraph with one decorative icon, plain
   article body) — must score < 0.35 → ``content``.
3. Edge cases: empty input, BS4 mode (no geometry), conflicting
   signals (icon class but long text → dampening must trigger).

We deliberately exercise both call paths:
* ``aggregate_node_stats(...)`` from raw dicts (mirrors the JS path
  used by ``verify_selector``).
* ``collect_node_stats_from_bs4(...)`` from BeautifulSoup (mirrors
  ``probe_detail_page``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from node_semantics import (  # noqa: E402  (path tweak)
    aggregate_node_stats,
    collect_node_stats_from_bs4,
    compute_icon_likelihood,
)


# ---------------------------------------------------------------------------
# Family 1: real-world icon containers
# ---------------------------------------------------------------------------


def test_elementor_icon_list_is_flagged_as_icon_container():
    """Mirrors the Seczambia bug: 25 ``<i>`` icons each carry a tiny
    label. The selector should be flagged ``icon_container`` and
    score >= 0.6 because three independent signals fire (icon class +
    short text + tiny uniform box)."""
    nodes = []
    for _ in range(25):
        nodes.append({
            "class_str": "elementor-icon-list-text",
            "child_tags": {"i": 1, "span": 1},
            "text_len": 18,
            "width": 24.0,
            "height": 24.0,
            "has_aria_hidden": True,
            "has_svg_no_text": False,
        })
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)

    assert ilk.verdict == "icon_container"
    assert ilk.score >= 0.6
    assert any("icon" in e for e in ilk.evidence)
    assert any("text length" in e for e in ilk.evidence)


def test_font_awesome_icon_grid_is_flagged():
    """No geometry signal (BS4 mode) — must still flag because three
    DOM-only signals fire: fa- class, icon-tag-ratio, short text."""
    nodes = [{
        "class_str": "fa-stack fa-2x",
        "child_tags": {"i": 2},
        "text_len": 0,
        "width": None,
        "height": None,
        "has_aria_hidden": True,
        "has_svg_no_text": False,
    }] * 8
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict == "icon_container"


def test_material_icons_button_grid():
    nodes = [{
        "class_str": "material-icons mdc-icon-button",
        "child_tags": {"svg": 1},
        "text_len": 8,
        "width": 32.0,
        "height": 32.0,
        "has_aria_hidden": False,
        "has_svg_no_text": True,
    }] * 12
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict in ("icon_container", "ambiguous")
    assert ilk.score >= 0.4


# ---------------------------------------------------------------------------
# Family 2: real news content (must NOT be flagged)
# ---------------------------------------------------------------------------


def test_long_article_is_content_even_with_icon_class():
    """Article body class happens to contain "icon" (it's actually
    ``content-with-icon``) but each node is 1500 chars long. The
    text-dampening rule must halve the score so we don't tag it."""
    nodes = [{
        "class_str": "content-with-icon article-body",
        "child_tags": {"p": 12, "h2": 2, "img": 1},
        "text_len": 1500,
        "width": 720.0,
        "height": 4000.0,
        "has_aria_hidden": False,
        "has_svg_no_text": False,
    }]
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict == "content"
    assert ilk.score < 0.35


def test_plain_article_body():
    nodes = [{
        "class_str": "post-content",
        "child_tags": {"p": 8, "h2": 1, "blockquote": 1},
        "text_len": 3200,
        "width": 800.0,
        "height": 5200.0,
        "has_aria_hidden": False,
        "has_svg_no_text": False,
    }]
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict == "content"
    assert ilk.score == 0.0


def test_navigation_links_are_not_icon_container():
    """Plain link list — links have moderate text, no icon-y signals.
    Should not flag, even though items are visually small."""
    nodes = [{
        "class_str": "nav-link",
        "child_tags": {"a": 1},
        "text_len": 28,
        "width": 90.0,
        "height": 22.0,
        "has_aria_hidden": False,
        "has_svg_no_text": False,
    }] * 6
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    # tiny size + short text fires (0.2 + 0.2) but no icon class
    # → at most 0.4 → "ambiguous", which is correct: small text-only
    # link rows DO look icon-shaped from layout alone, the LLM should
    # be told to think twice. Crucially we do NOT cross 0.6.
    assert ilk.verdict in ("content", "ambiguous")
    assert ilk.score < 0.6


# ---------------------------------------------------------------------------
# Family 3: edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_safe_zero():
    stats = aggregate_node_stats([])
    ilk = compute_icon_likelihood(stats)
    assert ilk.score == 0.0
    assert ilk.verdict == "content"
    assert stats.node_count == 0


def test_partial_input_does_not_raise():
    # Missing fields should default to safe values.
    stats = aggregate_node_stats([{"class_str": "foo"}, {}])
    ilk = compute_icon_likelihood(stats)
    assert isinstance(ilk.score, float)
    assert ilk.verdict == "content"


def test_evidence_lists_each_firing_signal():
    nodes = [{
        "class_str": "elementor-icon-list-text",
        "child_tags": {"i": 1},
        "text_len": 12,
        "width": 24.0,
        "height": 24.0,
        "has_aria_hidden": True,
        "has_svg_no_text": False,
    }] * 5
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    blob = " ".join(ilk.evidence).lower()
    assert "icon" in blob
    assert "text length" in blob
    assert "aria" in blob


def test_icon_class_alone_does_not_cross_icon_threshold():
    """A node whose class merely *contains* "icon" but has long text,
    no aria, normal-sized box, no icon tags. Single signal must not
    promote to ``icon_container``."""
    nodes = [{
        "class_str": "icon-banner-wrapper",
        "child_tags": {"div": 3, "p": 2},
        "text_len": 800,
        "width": 1200.0,
        "height": 400.0,
        "has_aria_hidden": False,
        "has_svg_no_text": False,
    }]
    stats = aggregate_node_stats(nodes)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict != "icon_container"


# ---------------------------------------------------------------------------
# BS4 path (probe_detail_page)
# ---------------------------------------------------------------------------


def test_bs4_collect_handles_icon_list_html():
    html = """
    <div class="elementor-icon-list-text">
      <i aria-hidden="true"></i><span>News A</span>
    </div>
    <div class="elementor-icon-list-text">
      <i aria-hidden="true"></i><span>News B</span>
    </div>
    <div class="elementor-icon-list-text">
      <i aria-hidden="true"></i><span>News C</span>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    elements = soup.select(".elementor-icon-list-text")
    per_node = collect_node_stats_from_bs4(elements)
    assert len(per_node) == 3
    assert per_node[0]["text_len"] > 0
    assert per_node[0]["child_tags"].get("i") == 1
    assert per_node[0]["has_aria_hidden"] is False  # aria is on <i>, not on the wrapper
    assert per_node[0]["width"] is None  # BS4 has no layout

    stats = aggregate_node_stats(per_node)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict in ("icon_container", "ambiguous")
    assert stats.icon_class_pattern is not None


def test_bs4_collect_handles_real_article_body():
    html = """
    <article class="post-content">
      <p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.""" + (" word" * 200) + """</p>
      <p>""" + ("more text " * 80) + """</p>
    </article>
    """
    soup = BeautifulSoup(html, "html.parser")
    elements = soup.select("article.post-content")
    per_node = collect_node_stats_from_bs4(elements)
    stats = aggregate_node_stats(per_node)
    ilk = compute_icon_likelihood(stats)
    assert ilk.verdict == "content"
    assert ilk.score == 0.0
