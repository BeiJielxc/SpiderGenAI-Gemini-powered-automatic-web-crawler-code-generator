"""Render site profile / pending feedback into prompt blocks.

Two functions matter to the planner read path:

* :func:`render_site_memory_hint` produces the ``[过往经验提示]`` section.
  It is a *hint*: every selector is paired with an explicit instruction
  to re-validate via ``verify_selector`` before reuse. Quarantined sites
  emit a warning-only block (no selectors leak through).

* :func:`render_feedback_replay_hint` produces the ``[反馈回放]`` section
  used by the rerun flow. It carries the previous run's auto_findings
  plus the user's plain-English suggestion, stamped with the highest
  priority so the planner cannot route around it.

Both renderers must be safe with empty / partial inputs: missing keys
silently noop. The output is always either ``""`` (nothing to inject)
or a self-contained markdown block.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

try:
    from .site_profile import MAX_BLACKLIST_SELECTORS  # type: ignore
except ImportError:  # pragma: no cover
    MAX_BLACKLIST_SELECTORS = 12


# ---------------------------------------------------------------------------
# Defaults for the failed-selector blacklist
# ---------------------------------------------------------------------------
# All three are tunable per-call (planner runner reads them from PygenConfig).
# Defaults err on the conservative side: a selector must have failed at
# least twice IN A ROW and won less than 20% of the time before we promote
# it onto the "do NOT try" list. This avoids blacklisting selectors that
# fail transiently due to a one-off network blip *or* a single Stage-2
# LLM misjudgment of slot-level verdict.
DEFAULT_BLACKLIST_MIN_LOSSES = 2
DEFAULT_BLACKLIST_MAX_WINRATE = 0.2
# When True, require ``consecutive_losses >= min_losses`` (set by
# site_profile bookkeeping). Means "the failures must be unbroken — a
# single user-confirmed win in between resets the streak". Strongly
# recommended once Stage-2 LLM slot_verdicts are in play, because a
# single mis-attributed slot can legitimately bump a healthy selector's
# total losses without indicating it's actually broken.
DEFAULT_BLACKLIST_REQUIRE_CONSECUTIVE = True


# ---------------------------------------------------------------------------
# Site memory hint (read path, hint priority)
# ---------------------------------------------------------------------------


def should_inject_profile(
    profile: Optional[Dict[str, Any]],
    *,
    min_confidence: float = 0.3,
    blacklist_min_losses: int = DEFAULT_BLACKLIST_MIN_LOSSES,
    blacklist_max_winrate: float = DEFAULT_BLACKLIST_MAX_WINRATE,
    blacklist_require_consecutive: bool = DEFAULT_BLACKLIST_REQUIRE_CONSECUTIVE,
) -> bool:
    """Gate the renderer to avoid injecting useless / harmful hints.

    A profile is worth injecting when **any** of these is true:

    * It is quarantined (we still want the warning block out).
    * It has stable selectors / known pitfalls / traits / drift flag and
      passes the confidence floor.
    * It carries a non-empty selector blacklist (low-confidence sites
      whose only signal is "what NOT to try" still benefit the planner).
    """
    if not isinstance(profile, dict):
        return False

    blacklist = _collect_selector_blacklist(
        profile,
        min_losses=blacklist_min_losses,
        max_winrate=blacklist_max_winrate,
        require_consecutive=blacklist_require_consecutive,
    )

    # Quarantined profiles still inject — but only as a "stay-away" warning,
    # never with selectors. We let the renderer handle that distinction.
    if not profile.get("quarantined"):
        try:
            confidence_ok = float(profile.get("confidence", 0)) >= float(min_confidence)
        except (TypeError, ValueError):
            confidence_ok = False
        # Confidence floor is ignored when the only payload is the
        # blacklist — telling the LLM "don't try X" is useful even
        # when overall trust is low.
        if not confidence_ok and not blacklist:
            return False

    has_anything = (
        bool(profile.get("stable_selectors"))
        or bool(profile.get("site_traits"))
        or bool(profile.get("known_pitfalls"))
        or profile.get("quarantined")
        or profile.get("has_drift")
        or bool(blacklist)
    )
    return bool(has_anything)


def render_site_memory_hint(
    profile: Optional[Dict[str, Any]],
    *,
    min_confidence: float = 0.3,
    blacklist_min_losses: int = DEFAULT_BLACKLIST_MIN_LOSSES,
    blacklist_max_winrate: float = DEFAULT_BLACKLIST_MAX_WINRATE,
    blacklist_require_consecutive: bool = DEFAULT_BLACKLIST_REQUIRE_CONSECUTIVE,
) -> str:
    """Markdown block for the planner; ``""`` means "do not inject"."""
    if not should_inject_profile(
        profile,
        min_confidence=min_confidence,
        blacklist_min_losses=blacklist_min_losses,
        blacklist_max_winrate=blacklist_max_winrate,
        blacklist_require_consecutive=blacklist_require_consecutive,
    ):
        return ""
    assert isinstance(profile, dict)

    domain = profile.get("domain", "(unknown)")
    quarantined = bool(profile.get("quarantined"))
    has_drift = bool(profile.get("has_drift"))

    blacklist = _collect_selector_blacklist(
        profile,
        min_losses=blacklist_min_losses,
        max_winrate=blacklist_max_winrate,
        require_consecutive=blacklist_require_consecutive,
    )

    lines = [
        "## [过往经验提示] (hint, must re-verify before use)",
        "",
        f"该域名 `{domain}` 在历史任务中出现过；以下信息**仅作参考**，",
        "**所有选择器必须先调 `verify_selector` 确认 visible > 0 才能写进代码**。",
        "如果 `verify_selector` 返回 0，说明网站已变化，必须忽略本提示并独立探测。",
        "",
    ]

    if quarantined:
        lines.append(
            "> **WARNING: 该站近期连续被人工标定为'运行错误'，"
            "过往选择器已隔离不再透露。**请按零信息任务正常探测。"
        )
        lines.append("")
        # Don't leak *positive* selectors when quarantined, but the
        # blacklist is the opposite signal — telling the LLM what to
        # avoid is *more* important now, not less.
        _render_selector_blacklist(blacklist, lines)
        _render_pitfalls(profile.get("known_pitfalls"), lines)
        _render_traits(profile.get("site_traits"), lines, brief=True)
        return "\n".join(lines).rstrip() + "\n"

    if has_drift:
        lines.append(
            "> **DRIFT 警告**: 上次运行的 HTML 指纹与历史不匹配，"
            "网站结构可能已经变化。下面的选择器仅供参考，**优先按零信息独立探测**。"
        )
        lines.append("")

    confidence = profile.get("confidence", 0)
    wins = int(profile.get("wins", 0))
    losses = int(profile.get("losses", 0))
    lines.append(
        f"- 历史 wins/losses (人工标定): **{wins}** / **{losses}**, "
        f"confidence ≈ {float(confidence):.2f}"
    )
    lines.append("")

    stable = profile.get("stable_selectors") or {}
    if stable:
        lines.append("### 历史上稳定的 CSS 选择器（仍需 verify_selector 重新验证）")
        lines.append("")
        for slot, info in sorted(stable.items()):
            sel = info.get("selector", "")
            wr = info.get("winrate", 0)
            lines.append(
                f"- `{slot}`: `{sel}`  (winrate {float(wr):.2f}, wins {info.get('wins', 0)})"
            )
        lines.append("")

    # Blacklist sits BELOW positives so the LLM reads "use these" first
    # then "don't try those". Order matches the way a human reviewer
    # would write a code-review comment.
    _render_selector_blacklist(blacklist, lines)
    _render_traits(profile.get("site_traits"), lines)
    _render_pitfalls(profile.get("known_pitfalls"), lines)

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Selector blacklist (failed selectors → "don't try X" hint)
# ---------------------------------------------------------------------------


def _collect_selector_blacklist(
    profile: Optional[Dict[str, Any]],
    *,
    min_losses: int,
    max_winrate: float,
    require_consecutive: bool = DEFAULT_BLACKLIST_REQUIRE_CONSECUTIVE,
) -> List[Dict[str, Any]]:
    """Pick out selectors that have repeatedly failed under user verdict.

    We pull from BOTH ``candidate_selectors`` (the typical case: tried,
    flopped, never made it to stable) AND ``stable_selectors`` (rare:
    something used to work but the site changed and it now consistently
    fails). The site-profile updater can demote stable selectors but
    we don't want to depend on that — surface them either way.

    Eligibility (must satisfy ALL):
      * ``losses >= min_losses``
      * ``winrate <= max_winrate``
      * if ``require_consecutive``: ``consecutive_losses >= min_losses``
        (set by site_profile bookkeeping; a single user-confirmed win
        in between resets it back to 0). Older profiles without this
        field default ``consecutive_losses == losses`` so the gate stays
        backward-compatible.

    Returns a list sorted by ``losses`` desc, capped at
    :data:`MAX_BLACKLIST_SELECTORS` so the prompt stays bounded.
    """
    if not isinstance(profile, dict):
        return []

    seen: Dict[str, Dict[str, Any]] = {}
    for bucket_name in ("candidate_selectors", "stable_selectors"):
        bucket = profile.get(bucket_name) or {}
        if not isinstance(bucket, dict):
            continue
        for slot, info in bucket.items():
            if not isinstance(info, dict):
                continue
            sel = str(info.get("selector") or "").strip()
            if not sel:
                continue
            try:
                wins = int(info.get("wins", 0) or 0)
                losses = int(info.get("losses", 0) or 0)
            except (TypeError, ValueError):
                continue
            if losses < min_losses:
                continue
            total = wins + losses
            winrate = (wins / total) if total else 0.0
            if winrate > max_winrate:
                continue
            if require_consecutive:
                # consecutive_losses is the per-entry counter that resets
                # whenever a verdict=correct comes in for the same selector.
                # Profiles created before this field existed will not have
                # it; for those we conservatively assume the current `losses`
                # ARE consecutive (they were all failures, no win to break
                # the streak), so the gate is purely additive — old profiles
                # behave exactly like before.
                try:
                    consec = int(info.get("consecutive_losses", losses) or 0)
                except (TypeError, ValueError):
                    consec = losses
                if consec < min_losses:
                    continue
            key = f"{slot}::{sel}"
            existing = seen.get(key)
            if existing is None or losses > int(existing.get("losses", 0)):
                seen[key] = {
                    "slot": str(slot),
                    "selector": sel,
                    "wins": wins,
                    "losses": losses,
                    "winrate": winrate,
                }

    items = sorted(seen.values(), key=lambda r: (-int(r["losses"]), r["slot"]))
    return items[:MAX_BLACKLIST_SELECTORS]


def _render_selector_blacklist(items: List[Dict[str, Any]], lines: list) -> None:
    if not items:
        return
    lines.append("### 历史上验证失败的选择器（**不要再试**, do NOT use）")
    lines.append("")
    lines.append(
        "下列选择器在过去运行中被人工标定为错误（或 verify_selector 反复失败），"
        "**本次禁止再次写入代码**。如果你必须用同一个 slot，请用 `analyze_page` "
        "或 `verify_selector` 重新发掘新的选择器。"
    )
    lines.append("")
    for it in items:
        slot = it.get("slot", "?")
        sel = it.get("selector", "")
        wins = it.get("wins", 0)
        losses = it.get("losses", 0)
        wr = float(it.get("winrate", 0))
        lines.append(
            f"- `{slot}`: `{sel}`  (losses {losses}, wins {wins}, winrate {wr:.2f})"
        )
    lines.append("")


def _render_traits(traits: Any, lines: list, *, brief: bool = False) -> None:
    if not isinstance(traits, dict) or not traits:
        return
    lines.append("### 网站特征 (历史归纳)")
    lines.append("")
    items = list(traits.items())
    if brief:
        items = items[:3]
    for k, v in items:
        if v in (None, "", []):
            continue
        lines.append(f"- **{k}**: {v}")
    lines.append("")


def _render_pitfalls(pitfalls: Any, lines: list) -> None:
    if not isinstance(pitfalls, list) or not pitfalls:
        return
    lines.append("### 已知坑点 (历史教训)")
    lines.append("")
    for p in pitfalls[-8:]:  # last few are the most relevant
        if isinstance(p, str) and p.strip():
            lines.append(f"- {p.strip()}")
    lines.append("")


# ---------------------------------------------------------------------------
# Feedback replay hint (rerun path, highest priority)
# ---------------------------------------------------------------------------


def render_feedback_replay_hint(
    prev_episode: Union[
        None,
        Dict[str, Any],
        Sequence[Dict[str, Any]],
    ],
) -> str:
    """Render the high-priority rerun block.

    Accepts either a single previous Episode (legacy single-jump) or a
    *chain* of previous attempts ordered **newest-first** (multi-hop
    replay). For a chain we render the most recent hop in full and
    older hops in a compact form so the planner sees the trajectory of
    failures without bloating the prompt.

    From each hop we extract:

    * ``user_suggestion`` — verbatim user text (top of mind for the LLM)
    * ``auto_findings`` — heuristic self-check report
    * ``lessons.failure_analysis`` — present after commit-time LLM enrichment

    Returns ``""`` when no hop has actionable content.
    """
    chain: List[Dict[str, Any]] = _coerce_episode_chain(prev_episode)
    if not chain:
        return ""

    # Latest hop must be informative; if even it is empty, skip.
    if not _episode_has_replay_content(chain[0]) and len(chain) == 1:
        return ""

    # Drop trailing empty hops so we don't render placeholder bullets.
    chain = [ep for ep in chain if _episode_has_replay_content(ep)]
    if not chain:
        return ""

    header_total = len(chain)
    lines = [
        "## [反馈回放] - 上次任务问题，本次必须修正（最高优先级）",
        "",
        f"过去 **{header_total}** 次同任务运行的反馈如下（最近一次最优先）。",
        "**本次必须正面解决这些问题，不能换个名字绕过。** 修复方向若与下方 ",
        "`[强约束/已验证选择器]` 冲突，以本节为准。",
        "",
    ]

    # --- latest hop: full detail --------------------------------------
    _render_replay_hop_full(
        chain[0],
        lines,
        index=1,
        total=header_total,
    )

    # --- older hops: compact ------------------------------------------
    for i, ep in enumerate(chain[1:], start=2):
        _render_replay_hop_compact(ep, lines, index=i, total=header_total)

    return "\n".join(lines).rstrip() + "\n"


def _coerce_episode_chain(
    prev_episode: Union[None, Dict[str, Any], Sequence[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if prev_episode is None:
        return []
    if isinstance(prev_episode, dict):
        return [prev_episode]
    if isinstance(prev_episode, (list, tuple)):
        return [ep for ep in prev_episode if isinstance(ep, dict)]
    return []


def _episode_has_replay_content(ep: Dict[str, Any]) -> bool:
    if not isinstance(ep, dict):
        return False
    suggestion = (ep.get("user_suggestion") or "").strip()
    findings = ep.get("auto_findings") or {}
    lessons = ep.get("lessons") or {}
    fa = lessons.get("failure_analysis") if isinstance(lessons, dict) else None
    return bool(suggestion) or _findings_have_content(findings) or bool(fa)


def _render_replay_hop_full(
    ep: Dict[str, Any],
    lines: list,
    *,
    index: int,
    total: int,
) -> None:
    suggestion = (ep.get("user_suggestion") or "").strip()
    verdict = (ep.get("user_verdict") or "").strip() or "(待评价)"
    findings = ep.get("auto_findings") or {}
    lessons = ep.get("lessons") or {}
    fa = lessons.get("failure_analysis") if isinstance(lessons, dict) else None
    task_id = (ep.get("task_id") or "").strip()
    tid_suffix = f" · task_id={task_id[:8]}…" if task_id else ""

    lines.append(f"### Hop {index}/{total}（最近一次, verdict={verdict}{tid_suffix}）")
    lines.append("")

    if suggestion:
        lines.append("**用户原话**（业务方反馈，可能为大白话）：")
        lines.append("")
        lines.append(f"> {suggestion}")
        lines.append("")

    if isinstance(fa, dict):
        interp = fa.get("user_complaint_interpreted")
        cause = fa.get("root_cause_guess")
        fix = fa.get("fix_direction")
        if interp or cause or fix:
            lines.append("**上次运行的失败诊断（LLM 解读）**：")
            lines.append("")
            if interp:
                lines.append(f"- 用户抱怨翻译: {interp}")
            if cause:
                lines.append(f"- 推断根因: {cause}")
            if fix:
                lines.append(f"- 修复方向: {fix}")
            lines.append("")

    if _findings_have_content(findings):
        lines.append("**模型自检报告（启发式扫描，零 token）**：")
        lines.append("")
        for bucket, label in (
            ("suspected_failures", "疑似失败/隐蔽错误"),
            ("redundant_tool_calls", "工具冗余调用"),
            ("redundant_code_blocks", "重复代码块"),
        ):
            items = findings.get(bucket) or []
            if not items:
                continue
            lines.append(f"- **{label}**:")
            for item in items[:5]:
                lines.append(f"  - {item}")
            lines.append("")


def _render_replay_hop_compact(
    ep: Dict[str, Any],
    lines: list,
    *,
    index: int,
    total: int,
) -> None:
    """Older hops: one bullet block per attempt, just enough context.

    We deliberately drop the heuristic-findings blob here — it duplicates
    a lot of text and the latest hop is what the planner needs to act
    on. Older hops carry value as *trend* signal: "we've tried this 3
    times and X keeps coming up".
    """
    suggestion = (ep.get("user_suggestion") or "").strip()
    verdict = (ep.get("user_verdict") or "").strip() or "(无评价)"
    lessons = ep.get("lessons") or {}
    fa = lessons.get("failure_analysis") if isinstance(lessons, dict) else None
    task_id = (ep.get("task_id") or "").strip()
    tid_suffix = f" · task_id={task_id[:8]}…" if task_id else ""

    lines.append(f"### Hop {index}/{total}（更早一次, verdict={verdict}{tid_suffix}）")
    lines.append("")

    if suggestion:
        lines.append(f"- 用户原话: > {_one_line(suggestion, 240)}")
    if isinstance(fa, dict):
        cause = fa.get("root_cause_guess")
        fix = fa.get("fix_direction")
        if cause:
            lines.append(f"- 当时推断根因: {_one_line(str(cause), 200)}")
        if fix:
            lines.append(f"- 当时修复方向: {_one_line(str(fix), 200)}")
    if not suggestion and not isinstance(fa, dict):
        # Last resort so the hop isn't a totally blank section.
        suspected = (ep.get("auto_findings") or {}).get("suspected_failures") or []
        if suspected:
            lines.append(f"- 当时启发式怀疑: {_one_line(str(suspected[0]), 200)}")
    lines.append("")


def _one_line(text: str, max_chars: int) -> str:
    s = " ".join(str(text).split())
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _findings_have_content(findings: Any) -> bool:
    if not isinstance(findings, dict):
        return False
    return any(bool(v) for v in findings.values() if isinstance(v, (list, dict, str)))


# ---------------------------------------------------------------------------
# Multi-hop chain walker (used by runner to assemble the replay block)
# ---------------------------------------------------------------------------


def walk_rerun_chain(
    store: Any,
    latest_task_id: Optional[str],
    *,
    max_hops: int = 3,
    expected_domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Walk ``rerun_of`` pointers backward and return episodes newest-first.

    Strategy per hop:

    1. Try ``store.read_draft(task_id)`` first — drafts hold the
       freshest auto_findings even before the user evaluates.
    2. If no draft, scan ``store.iter_committed_episodes()`` for a
       matching ``task_id``. Linear scan is fine: max_hops is tiny
       (default 3) and the file is bounded by the ring-buffer config.
    3. Stop early on missing episode, missing ``rerun_of``, cycle
       detection, or once we've collected ``max_hops`` entries.

    Hole-2.A guard — when ``expected_domain`` is provided we **drop** any
    hop whose ``domain`` field doesn't match. This is the safety net for
    batch runs where the previous task in the queue might belong to a
    completely different site: we'd rather inject nothing than mislead the
    planner with cross-domain feedback. Stops at the first mismatch (we
    don't try to "skip and continue" because rerun lineage is by
    construction same-site; a mismatch means the chain has been corrupted
    or the caller passed in the wrong id).

    The walker swallows store errors (any exception) so a corrupt
    memory file can never block the main agent flow.
    """
    if not latest_task_id or store is None or max_hops <= 0:
        return []

    expected = (expected_domain or "").strip().lower() or None

    chain: List[Dict[str, Any]] = []
    seen: set = set()
    current_id: Optional[str] = str(latest_task_id)

    while current_id and len(chain) < max_hops:
        if current_id in seen:
            break
        seen.add(current_id)
        ep = _lookup_episode_by_task_id(store, current_id)
        if ep is None:
            break
        if expected is not None:
            ep_domain = str(ep.get("domain") or "").strip().lower()
            if ep_domain and ep_domain != expected:
                # Cross-domain link — abandon the chain. Do not include
                # *this* episode either; partial chains across domains
                # are worse than no chain at all (planner would render
                # rules for site A inside site B's prompt).
                break
        chain.append(ep)
        next_id = ep.get("rerun_of")
        current_id = str(next_id) if next_id else None

    return chain


def find_recent_task_id_for_domain(
    store: Any,
    domain: str,
    *,
    max_age_days: float = 14.0,
    log: Optional[Any] = None,
) -> Optional[str]:
    """Hole-2.A fallback: scan memory for the most recent task on ``domain``.

    Used by the runner when the caller did **not** pass ``prev_task_id``
    (typical batch / fresh-form path) or when the prev_task_id pointed at
    a different domain. Lets the LLM still inherit yesterday's lessons
    even though the frontend forgot to forward the id.

    Search order — newest-first across both stores:

    1. Pending drafts in ``episode/pending/<task_id>.json`` whose
       embedded ``domain`` matches; sorted by file mtime.
    2. Committed episodes in ``episode/episodes.jsonl`` whose ``domain``
       matches; we use ``episodes_for_domain(limit=1)`` which returns
       newest-first.

    ``max_age_days`` caps how stale a draft can be before we ignore it.
    Set to 0 / negative to disable the age check (test path).

    Returns ``None`` if no candidate is found. Never raises.
    """
    domain = (domain or "").strip().lower()
    if not domain or store is None:
        return None

    log_fn = log if callable(log) else (lambda _msg: None)

    # ---- Pending drafts (freshest signal) ---------------------------------
    try:
        pending_dir = getattr(store, "root", None)
        pending_subdir = getattr(store, "PENDING_DIR", "episode/pending")
        if pending_dir is not None:
            from pathlib import Path  # local import keeps render.py importable in micro-envs

            pending_path = Path(pending_dir) / pending_subdir
            if pending_path.exists():
                import json as _json
                import time as _time

                cutoff_ts = (
                    _time.time() - (max_age_days * 86400.0)
                    if max_age_days and max_age_days > 0
                    else 0.0
                )
                candidates: List[tuple] = []
                for p in pending_path.iterdir():
                    if not p.is_file() or p.suffix.lower() != ".json":
                        continue
                    try:
                        mtime = p.stat().st_mtime
                    except OSError:
                        continue
                    if cutoff_ts and mtime < cutoff_ts:
                        continue
                    try:
                        data = _json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if str(data.get("domain") or "").strip().lower() != domain:
                        continue
                    tid = str(data.get("task_id") or "").strip()
                    if not tid:
                        continue
                    candidates.append((mtime, tid))
                if candidates:
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    chosen = candidates[0][1]
                    log_fn(
                        f"[MEMORY] domain-fallback found pending draft "
                        f"task_id={chosen[:8]} (domain={domain})"
                    )
                    return chosen
    except Exception as exc:
        log_fn(f"[MEMORY] domain-fallback pending scan failed (non-fatal): {exc}")

    # ---- Committed episodes (older but still useful) ----------------------
    try:
        if hasattr(store, "episodes_for_domain"):
            recent = store.episodes_for_domain(domain, limit=1)
            if recent:
                tid = str(recent[0].get("task_id") or "").strip() or None
                if tid:
                    log_fn(
                        f"[MEMORY] domain-fallback found committed episode "
                        f"task_id={tid[:8]} (domain={domain})"
                    )
                    return tid
    except Exception as exc:
        log_fn(f"[MEMORY] domain-fallback committed scan failed (non-fatal): {exc}")

    return None


def _lookup_episode_by_task_id(store: Any, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        if hasattr(store, "read_draft"):
            ep = store.read_draft(task_id)
            if ep is not None:
                return ep
    except Exception:
        pass
    try:
        if hasattr(store, "iter_committed_episodes"):
            for ep in store.iter_committed_episodes():
                if ep.get("task_id") == task_id:
                    return ep
    except Exception:
        pass
    return None


__all__ = [
    "DEFAULT_BLACKLIST_MAX_WINRATE",
    "DEFAULT_BLACKLIST_MIN_LOSSES",
    "find_recent_task_id_for_domain",
    "render_feedback_replay_hint",
    "render_site_memory_hint",
    "should_inject_profile",
    "walk_rerun_chain",
]
