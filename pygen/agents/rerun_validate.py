"""Rerun pre-validation — Module B of the persistent-learning plan.

When we are about to re-run a task on the same site, we already have
TWO valuable things in hand:

1. A PageCache row with the HTML we captured last time (Module A).
2. The previous episode's ``lessons.failure_analysis.fix_direction``
   (and the structured ``verified_selectors`` we used last time).

Instead of immediately handing all of that as raw text to the planner
LLM and hoping it correctly tries the right selectors, we run a *cheap
offline pre-check*:

* Parse the cached HTML with BeautifulSoup.
* For every selector-shaped string we can extract from the previous
  ``fix_direction`` and from previous ``verified_selectors``, count the
  matches in the cached DOM.
* Bucket each candidate as **pre_validated** (matches > 0), **disproved**
  (matches == 0) or **unknown** (cache miss / drift / parse error).

We then render that bucketing into a Chinese-prose hint that gets
*prepended* to ``feedback_replay_hint`` (highest-priority slot in the
prompt). The planner sees a concrete, structurally-checked
recommendation like:

    ### 🔍 上次失败修复假设的预验证结果
    - ✅ 优先尝试: `.elementor-widget-theme-post-content` —— cached DOM 命中 6 个节点
    - ❌ 不要再试: `.elementor-icon-list-text` —— cached DOM 命中 0 节点
    - ⏭ 暂不可判: `<其它>` —— 缓存 miss / drift

This module is **deliberately not** a LangGraph node:

* The PageCache is read-only and side-effect-free at this point.
* The pre-validation result is rendered into the SAME slot that the
  feedback-replay hint occupies, so we don't have to touch the planner
  graph topology, the critic flow, or AgentState shape (all high-blast-
  radius surfaces).
* Cache miss → empty render → caller proceeds exactly as before.

The whole module returns ``("", []) `` on any error so the runner can
inject it unconditionally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Selector extraction (shared logic with memory.site_profile)
# ---------------------------------------------------------------------------
#
# Why duplicate the regex here instead of importing the ``_SELECTOR_HINT_RE``
# from ``memory.site_profile``? Two reasons:
# 1. The site_profile pattern is built to *detect* "is this a selector?",
#    so it matches ANY substring containing a selector-like token. Here
#    we want to *extract* the exact selector text, which means greedy
#    boundaries and tighter capture groups.
# 2. We deliberately keep this list narrow — only patterns that BS4
#    knows how to evaluate. Pseudo-classes like ``:hover`` are useless
#    for our offline DOM check (BS4 doesn't compute layout state) so
#    we don't try to extract them.

# A "CSS-selector-ish" token: starts with `.` / `#` / a tag name; can
# carry one or more class chains, attribute predicates, descendant
# combinators (`>`, `+`, `~`, ` `) and nested element selectors.
_SELECTOR_TOKEN = r"""
    (?:[a-zA-Z][\w\-]*)?         # optional tag (div, span, h1)
    (?:                          # at least one of these qualifiers
        \.[a-zA-Z][\w\-]+        #   .class
      | \#[a-zA-Z][\w\-]+        #   #id
      | \[[^\]]+\]               #   [attr=val]
    )
    (?:                          # may chain: .foo.bar[x][y]
        \.[a-zA-Z][\w\-]+
      | \#[a-zA-Z][\w\-]+
      | \[[^\]]+\]
    )*
"""

# A selector path: SELECTOR (combinator SELECTOR)*. Combinators include
# the descendant combinator (one or more spaces). We bound the path to
# 6 tokens so a runaway match doesn't accidentally swallow a paragraph.
_PATH_RE = re.compile(
    rf"""(?x)
    (?P<sel>
        {_SELECTOR_TOKEN}
        (?:
            \s*[>+~]\s*           # explicit combinator
          | \s+                   # descendant
        )
        {_SELECTOR_TOKEN}
        (?:
            (?:\s*[>+~]\s* | \s+)
            {_SELECTOR_TOKEN}
        ){{0,4}}
        |
        {_SELECTOR_TOKEN}         # or a single-token selector
    )
    """
)


def extract_selectors_from_text(text: str, *, max_selectors: int = 8) -> List[str]:
    """Return a deduped list of selector-shaped substrings found in
    ``text``.

    Conservative: anything we can't parse as a CSS selector path with
    the regex above is silently dropped. Order is preserved (earlier
    mentions in the text are kept first), capped at ``max_selectors``
    so we don't explode the offline-check budget on a chatty
    fix_direction paragraph.
    """
    if not text or not isinstance(text, str):
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for m in _PATH_RE.finditer(text):
        sel = m.group("sel").strip()
        if not sel:
            continue
        # Strip surrounding punctuation that the regex sometimes drags
        # in (trailing periods/commas/colons).
        sel = sel.rstrip(".,:;)")
        # Reject single-tag matches without any class/id/attr (too generic
        # to be useful — "div", "a" alone always match thousands of nodes).
        if not re.search(r"[.#\[]", sel):
            continue
        if sel in seen:
            continue
        seen.add(sel)
        out.append(sel)
        if len(out) >= max_selectors:
            break
    return out


def collect_selectors_from_verified(verified: Any) -> List[str]:
    """Walk a ``verified_selectors`` ledger (or any nested dict/list of
    strings) and pull out the leaf selector strings.

    The ledger schema is loosely structured (see
    ``pygen.verified_selectors``); this helper just does a depth-first
    collect of every string leaf that LOOKS like a selector. We dedup
    while preserving discovery order.
    """
    seen: Set[str] = set()
    out: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            s = node.strip()
            if s and re.search(r"[.#\[]", s) and s not in seen:
                seen.add(s)
                out.append(s)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    _walk(verified)
    return out


# ---------------------------------------------------------------------------
# Pre-validation result
# ---------------------------------------------------------------------------


@dataclass
class PreValidationReport:
    """Structured outcome of a single rerun pre-validation pass.

    Used both for rendering and for tests (so we don't have to assert
    on prose).
    """

    cache_url: str = ""
    cache_age_sec: Optional[float] = None
    cache_drift: bool = False
    pre_validated: List[Dict[str, Any]] = field(default_factory=list)  # [{selector, count}]
    disproved: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)        # [{selector, reason}]

    @property
    def empty(self) -> bool:
        return not (self.pre_validated or self.disproved or self.skipped)


# ---------------------------------------------------------------------------
# Core pre-validation
# ---------------------------------------------------------------------------


def pre_validate_rerun_selectors(
    *,
    url: str,
    chain: Iterable[Dict[str, Any]],
    page_cache: Any,
    max_selectors: int = 5,
    log: Optional[Callable[[str], None]] = None,
) -> PreValidationReport:
    """Run the offline DOM-based pre-validation for a rerun.

    Parameters
    ----------
    url:
        The URL the upcoming run will open. Used as the PageCache key.
    chain:
        Result of ``walk_rerun_chain`` — the most-recent same-domain
        episodes (sorted recent → old). We pull selector candidates from
        the FIRST episode that has any (typically the most recent
        previous attempt).
    page_cache:
        Anything quacking like a ``PageCache`` (must implement
        ``get(url)`` returning ``CacheEntry`` or ``None``). Pass
        ``None`` to disable.
    max_selectors:
        Hard cap on how many candidates we actually run through BS4.
        Keeps the pre-validation budget bounded even if the LLM
        produced a 20-line ``fix_direction`` listing 30 selectors.
    log:
        Optional structured log callback (same signature as the
        runner's ``log_callback``).

    Returns
    -------
    A :class:`PreValidationReport` — empty (all three lists empty) when
    we have nothing to say (cache miss, no chain, no extractable
    selectors, BS4 unavailable, etc.). Caller can render that into a
    no-op block.
    """
    log = log or (lambda _msg: None)
    report = PreValidationReport(cache_url=url or "")

    # ---- 0. Sanity gates --------------------------------------------
    chain_list = list(chain or [])
    if not chain_list or not url or page_cache is None:
        return report

    # ---- 1. Pull the last episode that carries actionable selectors --
    candidates: List[Tuple[str, str]] = []  # (selector, source_label)
    for ep in chain_list:
        lessons = ep.get("lessons") or {}
        fa = lessons.get("failure_analysis") or {}
        text_parts: List[str] = []
        for key in ("fix_direction", "root_cause_guess", "user_complaint_interpreted"):
            v = fa.get(key)
            if isinstance(v, str) and v.strip():
                text_parts.append(v)
        # Also harvest any selectors the episode *actually used*.
        verified = ep.get("verified_selectors") or {}

        from_text = extract_selectors_from_text("\n".join(text_parts), max_selectors=max_selectors)
        from_verified = collect_selectors_from_verified(verified)[:max_selectors]

        for s in from_text:
            candidates.append((s, "fix_direction"))
        for s in from_verified:
            candidates.append((s, "previous_run_used"))
        if candidates:
            break  # one episode is enough; older hops add noise

    if not candidates:
        return report

    # Dedup while preserving order (a selector may appear in both
    # fix_direction text and verified_selectors — keep the first source).
    deduped: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for sel, src in candidates:
        if sel in seen:
            continue
        seen.add(sel)
        deduped.append((sel, src))
        if len(deduped) >= max_selectors:
            break
    candidates = deduped

    # ---- 2. Fetch cached HTML ---------------------------------------
    try:
        entry = page_cache.get(url)
    except Exception as exc:
        log(f"[RERUN_VALIDATE] page_cache.get failed (non-fatal): {exc}")
        return report
    if entry is None:
        log(
            f"[RERUN_VALIDATE] cache miss for {url[:80]} — "
            f"skipping pre-validation, planner will explore from scratch"
        )
        # Still surface the candidates as "skipped" so the prompt at
        # least mentions what we *would* have checked. Reason is
        # ``cache_miss`` so the planner knows it's not a verdict.
        for sel, _src in candidates:
            report.skipped.append({"selector": sel, "reason": "cache_miss"})
        return report
    report.cache_age_sec = entry.age_sec
    report.cache_drift = entry.last_drift

    if entry.last_drift:
        # Cache row exists but its fingerprint changed at write-time
        # → DOM has shifted, our selector counts are unreliable. Surface
        # everything as skipped/drift so the planner re-explores.
        log(
            f"[RERUN_VALIDATE] cache row for {url[:80]} marked drift — "
            f"refusing to pre-validate (counts would be misleading)"
        )
        for sel, _src in candidates:
            report.skipped.append({"selector": sel, "reason": "drift"})
        return report

    # ---- 3. BS4 evaluate every candidate -----------------------------
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is project-wide
        log("[RERUN_VALIDATE] bs4 unavailable, skipping pre-validation")
        return report

    try:
        soup = BeautifulSoup(entry.html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(entry.html, "html.parser")
        except Exception as exc:
            log(f"[RERUN_VALIDATE] HTML parse failed (non-fatal): {exc}")
            return report

    for sel, src in candidates:
        try:
            matches = soup.select(sel)
        except Exception as exc:
            # Invalid selector for the BS4 grammar — surface as skipped
            # so the planner doesn't think we silently ignored it.
            report.skipped.append({"selector": sel, "reason": f"parse_error: {exc}"})
            continue
        n = len(matches)
        bucket = report.pre_validated if n > 0 else report.disproved
        bucket.append({"selector": sel, "count": n, "source": src})

    log(
        f"[RERUN_VALIDATE] {url[:80]} → "
        f"pre_validated={len(report.pre_validated)}, "
        f"disproved={len(report.disproved)}, "
        f"skipped={len(report.skipped)} "
        f"(cache_age={entry.age_sec:.0f}s)"
    )
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_pre_validation_hint(report: PreValidationReport) -> str:
    """Render a :class:`PreValidationReport` into the Chinese hint block
    that goes at the top of the planner prompt.

    Returns ``""`` for an empty report so the caller can skip
    concatenation entirely.
    """
    if report.empty:
        return ""

    lines: List[str] = [
        "### 🔍 上次失败修复假设的预验证结果（基于本地缓存 HTML，无需重开浏览器）",
    ]
    if report.cache_age_sec is not None:
        lines.append(
            f"_缓存来源：{_short_url(report.cache_url)} | 缓存年龄：{int(report.cache_age_sec)}s_"
        )
    lines.append("")

    if report.pre_validated:
        lines.append("**✅ 优先尝试（cached DOM 命中）：**")
        for row in report.pre_validated:
            lines.append(
                f"- `{row['selector']}` —— 命中 {row['count']} 个节点 "
                f"(来源: {row['source']})"
            )
        lines.append("")

    if report.disproved:
        lines.append("**❌ 不要再试（cached DOM 命中数为 0，已被本地预校验否定）：**")
        for row in report.disproved:
            lines.append(
                f"- `{row['selector']}` —— 命中 0 个节点 "
                f"(来源: {row['source']})"
            )
        lines.append("")

    if report.skipped:
        lines.append("**⏭ 暂不可判（缓存 miss / drift / 解析失败，需要重新探索）：**")
        for row in report.skipped:
            lines.append(f"- `{row['selector']}` —— {row['reason']}")
        lines.append("")

    lines.append(
        "_说明：上面的命中数来自离线 BeautifulSoup，不包含可见性判断；"
        "强烈建议先用 `verify_selector` 在 live 浏览器上复核 ✅ 项再下决心。_"
    )
    return "\n".join(lines)


def _short_url(url: str) -> str:
    if not url:
        return "-"
    return url if len(url) <= 80 else url[:77] + "..."


__all__ = [
    "PreValidationReport",
    "extract_selectors_from_text",
    "collect_selectors_from_verified",
    "pre_validate_rerun_selectors",
    "render_pre_validation_hint",
]
