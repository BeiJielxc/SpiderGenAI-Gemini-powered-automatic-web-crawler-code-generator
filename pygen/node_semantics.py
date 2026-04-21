"""Multi-signal "is this a container of icons?" scorer.

Why this exists
---------------
Tools like ``verify_selector`` and ``probe_detail_page`` historically
returned only **count** ("25 nodes match") and a **textContent preview**
("PUBLIC NOTICE..."). On Elementor / Bootstrap-style sites that's
exactly the data shape that fools the LLM into picking
``.elementor-icon-list-text`` as a "news content" container, because:

* The 25 ``<i>`` icons each carry a tiny readable label;
* The first 3 previews look like real article titles (they're the
  link captions next to the icons);
* No signal in the tool output reveals "actually 22/25 nodes are
  icon-shaped 24×24 buttons".

The fix is **not** "guess what ``<i>`` means semantically" — that
varies wildly across sites — but to combine several **weak,
independently-fallible** signals into a single transparent score:

    icon_class_hit          (+0.40)  classes match icon/fa-/glyphicon/material-icons/...
    icon_tag_ratio  > 0.5   (+0.20)  i/svg/img dominate the children
    median_text_len < 20    (+0.20)  per-node text is tiny
    aria_hidden_ratio > 0.3 (+0.15)  many nodes carry aria-hidden
    median size < 64×64     (+0.20)  (live-browser only)
    size_uniformity         (+0.10)  (live-browser only) all nodes geometrically identical
    svg_no_text             (+0.10)  the <svg> children have no <text> elements

with a **dampening cap**: median_text_len ≥ 200 → score *= 0.5
(big text content trumps any icon signal — likely a content container
that *includes* a few icons, not an icon list).

The scorer **always** emits an ``evidence`` list naming every signal
that fired and its weight. LLMs (and humans reading logs) can sanity-
check the verdict instead of taking it on faith.

Usage shape
-----------

* ``aggregate_node_stats(per_node_list)`` → ``AggregateStats``
  Pure function. ``per_node_list`` is a list of dicts collected
  either by JS in the live browser (verify_selector) or by
  BeautifulSoup (probe_detail_page).

* ``compute_icon_likelihood(stats)`` → ``IconLikelihood``
  Same input shape from both call sites. Geometry-related signals
  are skipped automatically when ``stats.median_width is None``.

This module has **zero** browser / network / IO dependencies — it's
pure Python over plain dicts so it's trivially unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median, pstdev
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Class-name patterns that strongly suggest icon usage.
# ---------------------------------------------------------------------------
#
# Each entry is a substring (case-insensitive) we look for inside the
# space-joined class string of any node in the candidate set. The
# patterns deliberately come from real-world conventions across the
# major frontend stacks we keep tripping over:
#
#   - Font Awesome:        fa-, fas, far, fab, fal, fad, fontawesome
#   - Bootstrap (legacy):  glyphicon
#   - Material Icons:      material-icons, mdi-, material-symbols
#   - Elementor:           elementor-icon, icon-list (covers Seczambia)
#   - Generic:             icon, ion-, bi- (Bootstrap Icons), feather, lucide
#
# We deliberately keep this list LOOSE and ALWAYS combine it with at
# least one other signal before deciding "icon container". A site that
# just happens to have a class containing the word "icon" but actually
# carries 800-char paragraphs WILL escape the verdict because the
# text-length signal won't fire.
_ICON_CLASS_PATTERNS: Tuple[str, ...] = (
    "icon",            # generic: also catches icon-list, ico_, .has-icon
    "fa-", "fas-", "far-", "fab-", "fal-", "fad-",
    "fontawesome",
    "glyphicon",
    "material-icons", "material-symbols", "mdi-",
    "ion-",
    "bi-",
    "feather",
    "lucide",
)

# Tags that act as visual icon carriers across (almost) every framework.
# We sum their counts to compute icon_tag_ratio. The list intentionally
# excludes ``<a>`` because links are a content signal in their own
# right and would inflate the icon ratio of *every* navigation list.
_ICON_TAGS: Tuple[str, ...] = ("i", "svg", "use", "img")


# Score weights — all between 0 and 1. The cap at score=1.0 is
# enforced inside ``compute_icon_likelihood``.
_W_ICON_CLASS = 0.40
_W_ICON_TAG_RATIO = 0.20
_W_SHORT_TEXT = 0.20
_W_ARIA_HIDDEN = 0.15
_W_TINY_BOX = 0.20
_W_SIZE_UNIFORM = 0.10
_W_SVG_NO_TEXT = 0.10

# Verdict thresholds.
_T_ICON = 0.60       # high confidence: planner should refuse this candidate
_T_AMBIGUOUS = 0.35  # planner should at least double-check before using it

# Dampening: when median text length is comfortably long the candidate
# is almost certainly a content block that happens to embed a few
# decorative icons — halve the score so tag/aria signals can't swamp
# a real article container.
_TEXT_DAMPEN_THRESHOLD = 200
_TEXT_DAMPEN_FACTOR = 0.5


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregateStats:
    """Set-level statistics computed from a list of per-node dicts.

    Geometry-derived fields (``median_width`` / ``median_height`` /
    ``size_uniform``) are ``None`` when the input was BS4-only (no
    live layout). The scorer skips those signals automatically.
    """

    node_count: int
    class_str_sample: str           # first non-empty class string, for evidence text
    icon_class_pattern: Optional[str]  # which pattern matched, or None
    median_text_len: float
    icon_tag_ratio: float           # 0..1: i/svg/use/img share of all children
    aria_hidden_ratio: float        # 0..1: nodes with aria-hidden="true"
    median_width: Optional[float]
    median_height: Optional[float]
    size_uniform: Optional[bool]    # True if width/height std/median < 0.1
    svg_no_text_ratio: float        # 0..1: nodes containing svg-without-text


@dataclass(frozen=True)
class IconLikelihood:
    """Score-and-evidence verdict.

    ``evidence`` lists every signal that fired and its contribution,
    in human-readable form. Both the scoring engine and downstream
    consumers (logs, planner prompts) read ``evidence`` directly so
    the verdict is always traceable.
    """

    score: float                # 0..1 after dampening + cap
    evidence: List[str] = field(default_factory=list)
    verdict: str = "content"    # "icon_container" | "ambiguous" | "content"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_node_stats(per_node: List[Dict[str, Any]]) -> AggregateStats:
    """Reduce a list of per-node dicts into one :class:`AggregateStats`.

    Each per-node dict is expected (but not required) to carry:

    * ``class_str``        – space-joined class names, lower-cased ok
    * ``child_tags``       – ``Counter``-shaped dict ``{"i": 5, "span": 1, ...}``
    * ``text_len``         – ``int`` text content length
    * ``width`` / ``height`` – ``float`` pixel size (``None`` if BS4-only)
    * ``has_aria_hidden``  – ``bool``
    * ``has_svg_no_text``  – ``bool`` (any ``<svg>`` child without ``<text>``)

    Missing fields default to safe values (zero / False / None) so the
    function never raises on partial input.
    """
    if not per_node:
        return AggregateStats(
            node_count=0, class_str_sample="", icon_class_pattern=None,
            median_text_len=0.0, icon_tag_ratio=0.0, aria_hidden_ratio=0.0,
            median_width=None, median_height=None, size_uniform=None,
            svg_no_text_ratio=0.0,
        )

    text_lens: List[int] = []
    icon_tag_total = 0
    child_tag_total = 0
    aria_hits = 0
    svg_hits = 0
    widths: List[float] = []
    heights: List[float] = []
    class_sample = ""
    matched_pattern: Optional[str] = None

    for node in per_node:
        cls_str = str(node.get("class_str") or "").lower().strip()
        if cls_str and not class_sample:
            class_sample = cls_str
        if matched_pattern is None and cls_str:
            for pat in _ICON_CLASS_PATTERNS:
                if pat in cls_str:
                    matched_pattern = pat
                    break

        text_lens.append(int(node.get("text_len") or 0))

        tags = node.get("child_tags") or {}
        if isinstance(tags, dict):
            for tname, count in tags.items():
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    continue
                child_tag_total += n
                if str(tname).lower() in _ICON_TAGS:
                    icon_tag_total += n

        if node.get("has_aria_hidden"):
            aria_hits += 1
        if node.get("has_svg_no_text"):
            svg_hits += 1

        w = node.get("width")
        h = node.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)):
            if w > 0 and h > 0:
                widths.append(float(w))
                heights.append(float(h))

    n = len(per_node)
    icon_tag_ratio = (icon_tag_total / child_tag_total) if child_tag_total else 0.0
    aria_ratio = aria_hits / n if n else 0.0
    svg_ratio = svg_hits / n if n else 0.0

    median_w: Optional[float] = None
    median_h: Optional[float] = None
    uniform: Optional[bool] = None
    if widths and heights:
        median_w = float(median(widths))
        median_h = float(median(heights))
        if len(widths) >= 3 and median_w > 0 and median_h > 0:
            wsig = pstdev(widths) / median_w
            hsig = pstdev(heights) / median_h
            uniform = bool(wsig < 0.1 and hsig < 0.1)

    return AggregateStats(
        node_count=n,
        class_str_sample=class_sample,
        icon_class_pattern=matched_pattern,
        median_text_len=float(median(text_lens)) if text_lens else 0.0,
        icon_tag_ratio=icon_tag_ratio,
        aria_hidden_ratio=aria_ratio,
        median_width=median_w,
        median_height=median_h,
        size_uniform=uniform,
        svg_no_text_ratio=svg_ratio,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def compute_icon_likelihood(stats: AggregateStats) -> IconLikelihood:
    """Run the multi-signal rule set against ``stats`` and return a
    :class:`IconLikelihood`.

    Single-signal verdicts are deliberately impossible: even when an
    icon class name is matched, the score caps at 0.4 unless a second
    signal also fires. This is the "tag alone is not semantics"
    safeguard the user explicitly asked for.
    """
    if stats.node_count == 0:
        return IconLikelihood(score=0.0, evidence=["no nodes to evaluate"], verdict="content")

    score = 0.0
    evidence: List[str] = []

    if stats.icon_class_pattern:
        score += _W_ICON_CLASS
        evidence.append(
            f"class contains '{stats.icon_class_pattern}' (+{_W_ICON_CLASS:.2f})"
        )

    if stats.icon_tag_ratio > 0.5:
        score += _W_ICON_TAG_RATIO
        evidence.append(
            f"icon tags (i/svg/use/img) {stats.icon_tag_ratio*100:.0f}% of children "
            f"(+{_W_ICON_TAG_RATIO:.2f})"
        )

    if 0 < stats.median_text_len < 20:
        score += _W_SHORT_TEXT
        evidence.append(
            f"median text length {int(stats.median_text_len)} chars "
            f"(+{_W_SHORT_TEXT:.2f})"
        )

    if stats.aria_hidden_ratio > 0.3:
        score += _W_ARIA_HIDDEN
        evidence.append(
            f"aria-hidden on {stats.aria_hidden_ratio*100:.0f}% of nodes "
            f"(+{_W_ARIA_HIDDEN:.2f})"
        )

    if stats.svg_no_text_ratio > 0.3:
        score += _W_SVG_NO_TEXT
        evidence.append(
            f"<svg> without <text> on {stats.svg_no_text_ratio*100:.0f}% of nodes "
            f"(+{_W_SVG_NO_TEXT:.2f})"
        )

    if stats.median_width is not None and stats.median_height is not None:
        if stats.median_width < 64 and stats.median_height < 64:
            score += _W_TINY_BOX
            evidence.append(
                f"median size {stats.median_width:.0f}x{stats.median_height:.0f}px "
                f"(+{_W_TINY_BOX:.2f})"
            )
    if stats.size_uniform:
        score += _W_SIZE_UNIFORM
        evidence.append(
            f"all nodes geometrically uniform (+{_W_SIZE_UNIFORM:.2f})"
        )

    # Big-text dampening: a real article container can carry decorative
    # icons; halve the score so a single icon-class hit can't tag it.
    if stats.median_text_len >= _TEXT_DAMPEN_THRESHOLD and score > 0:
        old = score
        score *= _TEXT_DAMPEN_FACTOR
        evidence.append(
            f"median text length {int(stats.median_text_len)} chars >= "
            f"{_TEXT_DAMPEN_THRESHOLD} → score dampened {old:.2f}*"
            f"{_TEXT_DAMPEN_FACTOR:.2f}={score:.2f}"
        )

    score = min(1.0, max(0.0, score))

    if score >= _T_ICON:
        verdict = "icon_container"
    elif score >= _T_AMBIGUOUS:
        verdict = "ambiguous"
    else:
        verdict = "content"

    if not evidence:
        evidence.append("no icon signals detected")

    return IconLikelihood(score=round(score, 3), evidence=evidence, verdict=verdict)


# ---------------------------------------------------------------------------
# BeautifulSoup helpers (used by probe_detail_page; verify_selector
# collects per-node stats in JS so doesn't need this side)
# ---------------------------------------------------------------------------


def collect_node_stats_from_bs4(elements: List[Any]) -> List[Dict[str, Any]]:
    """Build per-node stats dicts from a list of BeautifulSoup ``Tag``s.

    Geometry fields are ``None`` (BS4 has no layout). Each element's
    *direct* children — not the recursive descendant tag soup — are
    counted, mirroring what JS ``el.children`` returns. This keeps the
    BS4 path comparable to the live-browser path.
    """
    out: List[Dict[str, Any]] = []
    for el in elements:
        if el is None or not hasattr(el, "name"):
            continue
        try:
            class_str = " ".join(el.get("class") or [])
        except Exception:
            class_str = ""

        child_tags: Dict[str, int] = {}
        try:
            for child in getattr(el, "children", []):
                cname = getattr(child, "name", None)
                if not cname:
                    continue
                cn = str(cname).lower()
                child_tags[cn] = child_tags.get(cn, 0) + 1
        except Exception:
            pass

        try:
            text_len = len((el.get_text(strip=True) or ""))
        except Exception:
            text_len = 0

        try:
            aria_hidden = (el.get("aria-hidden") == "true")
        except Exception:
            aria_hidden = False

        has_svg_no_text = False
        try:
            for svg in el.find_all("svg", recursive=False):
                if not svg.find("text"):
                    has_svg_no_text = True
                    break
        except Exception:
            pass

        out.append({
            "class_str": class_str,
            "child_tags": child_tags,
            "text_len": text_len,
            "width": None,
            "height": None,
            "has_aria_hidden": aria_hidden,
            "has_svg_no_text": has_svg_no_text,
        })
    return out


__all__ = [
    "AggregateStats",
    "IconLikelihood",
    "aggregate_node_stats",
    "collect_node_stats_from_bs4",
    "compute_icon_likelihood",
]
