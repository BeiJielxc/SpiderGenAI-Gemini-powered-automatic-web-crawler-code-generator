"""Per-site aggregate profile + update logic.

A *SiteProfile* is the rolling summary of everything we have learned
about a single domain: which selectors keep working, which ones fail,
what the platform looks like, whether the structure has been drifting.

Critical anti-misleading invariants
-----------------------------------
1. **Updates only from human-validated runs.** ``update_profile_from_episode``
   is a *no-op* unless the episode carries a non-null ``user_verdict``.
   This is the central guarantee that a model's self-deception (critic
   marked passed, but the page actually had no real data) never poisons
   the profile.

2. **Selectors must earn their stripes.** A selector enters
   ``stable_selectors`` only after ``promote_min_wins`` user-confirmed
   wins AND a win-rate ≥ ``promote_min_winrate``. A single happy run
   never produces a stable entry.

3. **Confidence decays with both time and failures.** Each successful
   commit nudges confidence up; a user_verdict=wrong slashes it.
   30 days of silence cost ``confidence_decay_per_30d`` automatically
   (computed lazily on read so we never need a cron job).

4. **Drift detection.** We keep the most recent ``MAX_FINGERPRINTS``
   list-page fingerprints. When a new episode's fingerprint matches
   none of them we mark ``has_drift=True`` so the read-path renderer can
   warn the planner ("this site changed shape; verify before reusing
   selectors").

5. **Quarantine.** ``consecutive_failures >= quarantine_after`` flips
   ``quarantined=True``. A quarantined profile never injects selectors
   into the prompt — only a "[WARNING] this site has recent
   user-marked failures" hint, so the planner doesn't trip over the
   same trap again. A user-marked success resets the counter and
   un-quarantines automatically.

6. **Pitfall hygiene** (Module C of the persistent-learning plan).
   ``known_pitfalls`` only carries *abstract experience* (e.g. "this
   site needs Playwright", "list page is lazy-loaded"). Concrete
   selector guesses live in ``candidate_selectors`` where they get
   structured win/loss accounting. To enforce this we filter LLM
   ``fix_direction`` text on write: if it looks like a CSS selector
   (contains ``.classname`` / ``#id`` / ``[attr=...]`` / etc.) we
   *drop* it from pitfalls because the selector itself is already
   tracked structurally — keeping the textual advice would just create
   conflicting "权威建议" the next planner has to choose between.
   We also de-dup per slot: a new pitfall mentioning the same slot as
   an older one *replaces* the old text instead of stacking, so each
   slot has exactly one active textual advice.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FINGERPRINTS = 5
"""How many recent list-page fingerprints we keep for drift comparison."""

MAX_KNOWN_PITFALLS = 20
"""Cap on the per-domain pitfall list to keep profile size sane."""

MAX_BLACKLIST_SELECTORS = 12
"""Hard cap on how many failed selectors get rendered into the planner hint.

Anything beyond this is dropped (sorted by losses desc) — keeping the
prompt block bounded even when a site has been hammered for months.
"""


# Selector slot paths inside ``verified_selectors``: only these contribute
# to ``stable_selectors`` upgrades. We deliberately ignore alternatives
# (which are noisier, by-design fallbacks).
_TRACKABLE_SLOTS = (
    "list.container",
    "list.title_link",
    "list.title",
    "list.date",
    "list.next_page",
    "detail.content",
    "detail.title",
    "detail.publish_date",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class SiteProfile(dict):
    """Plain-dict container; same rationale as :class:`Episode`."""

    @classmethod
    def empty(cls, domain: str) -> "SiteProfile":
        now = _now_iso()
        return cls({
            "domain": domain,
            "version": 0,
            "first_seen_at": now,
            "last_updated_at": now,
            "last_success_at": None,
            "last_failure_at": None,
            "consecutive_failures": 0,
            "quarantined": False,
            "html_fingerprints": [],
            "has_drift": False,
            "confidence": 0.5,
            "wins": 0,
            "losses": 0,
            "stable_selectors": {},
            "candidate_selectors": {},  # selectors that haven't earned promotion yet
            "site_traits": {},
            "known_pitfalls": [],
        })

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "SiteProfile":
        if not isinstance(raw, dict):
            raise TypeError(f"SiteProfile must be dict, got {type(raw).__name__}")
        return cls(raw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _walk_slot(verified: Dict[str, Any], path: str) -> Optional[str]:
    """Return the selector string at dotted ``path`` or ``None``."""
    cur: Any = verified
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, str) and cur.strip():
        return cur.strip()
    return None


# ---------------------------------------------------------------------------
# Pitfall hygiene (Module C)
# ---------------------------------------------------------------------------
#
# Selector-shaped substring detector — used to keep raw selector advice OUT of
# the freeform ``known_pitfalls`` list. Patterns we treat as "this is a
# selector, not an experiential note":
#
#   .classname              CSS class
#   #anId                   CSS id
#   [attr=value]            CSS attribute predicate
#   ::pseudo / :pseudo()    pseudo-elements / pseudo-classes
#   tag.class               qualified element-with-class
#   div > span / a + b      combinators between two name-shaped tokens
#
# We deliberately use lightweight regex (not a real CSS parser): false
# positives just demote one piece of LLM-written advice; the structural
# truth lives in candidate_selectors anyway.
_SELECTOR_HINT_RE = re.compile(
    r"""(?xi)
    (?:                              # whole-text disjunction
        (?<![\w])\.[a-zA-Z][\w\-]+   # .class (not preceded by alphanumeric)
        | \#[a-zA-Z][\w\-]+          # #id
        | \[\s*[\w\-]+\s*[~|^$*]?=   # [attr=...]
        | ::?[a-zA-Z][\w\-]+         # ::pseudo / :hover
        | \w+\s*[>+~]\s*\w+          # combinators
    )
    """
)


# All trackable slot keywords we recognise inside pitfall text. Matches
# the canonical _TRACKABLE_SLOTS plus the bare leaves people sometimes
# write (e.g. "title" instead of "list.title"). We keep the canonical
# dotted form first so dedup honours the most specific tag.
_SLOT_KEYWORD_PATTERNS = [
    re.compile(r"\b" + re.escape(s) + r"\b", re.IGNORECASE) for s in _TRACKABLE_SLOTS
]


def _looks_like_selector_advice(text: str) -> bool:
    """True iff the pitfall text mentions a CSS selector pattern.

    We treat anything matching :data:`_SELECTOR_HINT_RE` as "concrete
    selector advice" → it should NOT enter known_pitfalls because the
    same information is already accounted for slot-by-slot in
    ``candidate_selectors``. Two such pitfalls written by two different
    runs are exactly the conflicting-advice situation that prompted
    Module C.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    return _SELECTOR_HINT_RE.search(text) is not None


def _extract_slot_keywords(text: str) -> Set[str]:
    """Return the set of canonical slot names mentioned in ``text``.

    Used to dedup: a new pitfall referencing ``list.title`` replaces
    the existing ``list.title`` pitfall (if any) instead of stacking.
    Empty set means "this is an abstract / cross-slot piece of advice"
    and we just append normally.
    """
    if not isinstance(text, str) or not text:
        return set()
    found: Set[str] = set()
    for pattern, slot in zip(_SLOT_KEYWORD_PATTERNS, _TRACKABLE_SLOTS):
        if pattern.search(text):
            found.add(slot)
    return found


def _merge_pitfall(existing: List[str], new_pitfall: str) -> List[str]:
    """Return a new pitfall list with ``new_pitfall`` merged in.

    Behaviour:

    * If ``new_pitfall`` looks like raw selector advice → drop it
      (concrete selectors belong in ``candidate_selectors``, not here).
    * If it mentions one or more slots → remove any *older* pitfall
      that mentioned the same slot(s), then append the new one. This
      stops the "两条互斥的修复建议同时摆在 prompt 顶部" scenario.
    * Otherwise (abstract experience, e.g. "needs Playwright") →
      append if not exact-duplicate.

    Pure: never mutates ``existing``.
    """
    new_pitfall = (new_pitfall or "").strip()
    if not new_pitfall:
        return list(existing or [])

    if _looks_like_selector_advice(new_pitfall):
        # Pure selector advice — refuse silently. The structural truth
        # lives in candidate_selectors / blacklist already.
        return list(existing or [])

    new_slots = _extract_slot_keywords(new_pitfall)
    if not new_slots:
        # Abstract advice. Append unless duplicate.
        if new_pitfall in (existing or []):
            return list(existing or [])
        return list(existing or []) + [new_pitfall]

    # Same-slot dedup: drop older entries that mention any of new_slots.
    kept = [
        p for p in (existing or [])
        if not (_extract_slot_keywords(p) & new_slots)
    ]
    if new_pitfall in kept:
        return kept
    kept.append(new_pitfall)
    return kept


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def apply_time_decay(
    profile: SiteProfile,
    *,
    decay_per_30d: float = 0.1,
) -> SiteProfile:
    """Return a *copy* of ``profile`` with confidence decayed by elapsed time.

    Pure: never mutates input. Safe to call on read.
    """
    if not isinstance(profile, dict):
        return profile  # type: ignore[return-value]
    out = SiteProfile(dict(profile))
    last = _parse_iso(out.get("last_updated_at"))
    if last is None:
        return out
    now = datetime.now(timezone.utc)
    try:
        elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)
    except Exception:
        return out
    if elapsed_days < 1.0 or decay_per_30d <= 0:
        return out
    decay = (elapsed_days / 30.0) * decay_per_30d
    out["confidence"] = _clamp01(_coerce_float(out.get("confidence"), 0.5) - decay)
    return out


# ---------------------------------------------------------------------------
# Update from a committed episode (the only mutation path that matters)
# ---------------------------------------------------------------------------


def update_profile_from_episode(
    profile: Optional[SiteProfile],
    episode: Dict[str, Any],
    *,
    promote_min_wins: int = 3,
    promote_min_winrate: float = 0.8,
    confidence_penalty_on_fail: float = 0.3,
    confidence_bonus_on_success: float = 0.1,
    quarantine_after: int = 2,
    drift_check: bool = True,
) -> SiteProfile:
    """Apply a *committed* episode's signal to a site profile and return
    the new profile. Pure (returns a new dict; never mutates input).

    Returns the input profile unchanged when:

    * ``episode["user_verdict"]`` is None / not in {"correct", "wrong"};
    * ``episode["domain"]`` is empty (we can't index it).
    """
    domain = str(episode.get("domain") or "").strip()
    if not domain:
        return profile if isinstance(profile, dict) else SiteProfile.empty("")  # type: ignore[arg-type]

    verdict = episode.get("user_verdict")
    if verdict not in ("correct", "wrong"):
        # No human signal — never touch the profile (the central anti-misleading rule)
        return profile if isinstance(profile, dict) else SiteProfile.empty(domain)  # type: ignore[arg-type]

    base = SiteProfile.from_json(dict(profile)) if isinstance(profile, dict) else SiteProfile.empty(domain)
    out = SiteProfile(dict(base))
    out["domain"] = domain
    out["version"] = int(out.get("version", 0)) + 1
    out["last_updated_at"] = _now_iso()
    if not out.get("first_seen_at"):
        out["first_seen_at"] = out["last_updated_at"]

    # ---- fingerprint drift bookkeeping ----
    fp = episode.get("html_fingerprint")
    if drift_check and isinstance(fp, str) and fp and not fp.startswith("sha256:empty"):
        history = list(out.get("html_fingerprints") or [])
        seen_before = fp in history
        # Always record the new fingerprint at the head, dedupe, cap.
        history = [fp] + [h for h in history if h != fp]
        history = history[:MAX_FINGERPRINTS]
        out["html_fingerprints"] = history
        # has_drift fires when this fingerprint is brand new AND we had at
        # least one prior fingerprint to compare against.
        out["has_drift"] = (not seen_before) and len(history) > 1

    # ---- counters / streak / confidence ----
    if verdict == "correct":
        out["wins"] = int(out.get("wins", 0)) + 1
        out["last_success_at"] = out["last_updated_at"]
        out["consecutive_failures"] = 0
        out["confidence"] = _clamp01(
            _coerce_float(out.get("confidence"), 0.5) + confidence_bonus_on_success
        )
        # Coming back from quarantine requires a human-confirmed success.
        if out.get("quarantined"):
            out["quarantined"] = False
    else:  # verdict == "wrong"
        out["losses"] = int(out.get("losses", 0)) + 1
        out["last_failure_at"] = out["last_updated_at"]
        out["consecutive_failures"] = int(out.get("consecutive_failures", 0)) + 1
        out["confidence"] = _clamp01(
            _coerce_float(out.get("confidence"), 0.5) - confidence_penalty_on_fail
        )
        if out["consecutive_failures"] >= quarantine_after:
            out["quarantined"] = True

    # ---- selector accounting (slot-level when the LLM gave us slot_verdicts) ----
    #
    # Why this is non-trivial: the user's verdict is task-level ("整次任务对/错"),
    # but a single failed run usually only breaks ONE slot (e.g. detail.content
    # picked up a wrapper full of icons while list.title / detail.title were
    # fine). Naively pushing the task verdict down to every slot's selector
    # would (a) starve correct selectors of wins, and (b) put innocent
    # selectors on the blacklist after a single misjudged run.
    #
    # Stage-2 LLM is asked to fill ``lessons.slot_verdicts`` with one of
    # {"correct","wrong","unknown"} per slot. The contract:
    #
    #   * key present and value=="correct"  → +1 win for that slot's selector
    #   * key present and value=="wrong"    → +1 loss for that slot's selector
    #   * key present and value=="unknown"  → DO NOT TOUCH counters (LLM
    #                                          explicitly refused to judge)
    #   * key absent                        → fall back to the task-level
    #                                          verdict (legacy behaviour;
    #                                          covers no-LLM / fallback paths
    #                                          and pre-slot_verdicts episodes)
    #
    # We also track ``consecutive_losses`` per entry so the blacklist can
    # require N losses *in a row* (a single win in between resets the
    # counter — protects honest selectors against one-off misjudgments).
    verified = episode.get("verified_selectors") or {}
    candidates = dict(out.get("candidate_selectors") or {})
    stable = dict(out.get("stable_selectors") or {})

    lessons = episode.get("lessons") or {}
    slot_verdicts = lessons.get("slot_verdicts") if isinstance(lessons, dict) else None
    slot_verdicts = slot_verdicts if isinstance(slot_verdicts, dict) else {}

    for slot in _TRACKABLE_SLOTS:
        selector = _walk_slot(verified, slot)
        if not selector:
            continue

        slot_verdict = slot_verdicts.get(slot)
        if isinstance(slot_verdict, str):
            slot_verdict = slot_verdict.strip().lower()
        if slot_verdict in ("correct", "wrong"):
            effective = slot_verdict
        elif slot_verdict == "unknown":
            # LLM intentionally passed — do not touch counters, but still
            # make sure the candidate entry exists (for visibility).
            effective = None
        else:
            # No slot-level signal → fall back to task-level verdict.
            effective = verdict

        key = f"{slot}::{selector}"
        entry = candidates.get(key) or {
            "slot": slot,
            "selector": selector,
            "wins": 0,
            "losses": 0,
            "consecutive_losses": 0,
            "last_verdict": None,
        }

        if effective == "correct":
            entry["wins"] = int(entry.get("wins", 0)) + 1
            entry["consecutive_losses"] = 0
            entry["last_verdict"] = "correct"
        elif effective == "wrong":
            entry["losses"] = int(entry.get("losses", 0)) + 1
            entry["consecutive_losses"] = int(entry.get("consecutive_losses", 0)) + 1
            entry["last_verdict"] = "wrong"
        else:
            # unknown / no-op: just refresh metadata, don't move counters.
            entry.setdefault("consecutive_losses", 0)
            entry.setdefault("last_verdict", None)

        candidates[key] = entry

        wins = int(entry.get("wins", 0))
        total = wins + int(entry.get("losses", 0))
        winrate = (wins / total) if total else 0.0
        if wins >= promote_min_wins and winrate >= promote_min_winrate:
            stable[slot] = {
                "selector": selector,
                "wins": wins,
                "losses": int(entry.get("losses", 0)),
                "winrate": round(winrate, 3),
                "promoted_at": out["last_updated_at"],
            }

    out["candidate_selectors"] = candidates
    out["stable_selectors"] = stable

    # ---- absorb LLM-derived site_traits and pitfalls (additive) ----
    lessons = episode.get("lessons") or {}
    if isinstance(lessons, dict):
        traits = dict(out.get("site_traits") or {})
        ep_traits = lessons.get("site_traits") or {}
        if isinstance(ep_traits, dict):
            for k, v in ep_traits.items():
                if v in (None, "", []):
                    continue
                traits[str(k)] = v
        out["site_traits"] = traits

        # Module C: route every candidate pitfall through ``_merge_pitfall``
        # so concrete-selector advice gets dropped (lives in
        # candidate_selectors instead) and same-slot advice replaces older
        # entries instead of stacking. This breaks the "两条互斥的修复建议
        # 同时摆在 prompt 顶部" failure mode that prompted Module C.
        pitfalls = list(out.get("known_pitfalls") or [])
        fa = lessons.get("failure_analysis") or {}
        if isinstance(fa, dict):
            for key in ("fix_direction", "root_cause_guess"):
                v = fa.get(key)
                if isinstance(v, str) and v.strip():
                    pitfalls = _merge_pitfall(pitfalls, v)
        for tip in (lessons.get("optimization") or [])[:3]:
            if isinstance(tip, str) and tip.strip():
                pitfalls = _merge_pitfall(pitfalls, tip)
        out["known_pitfalls"] = pitfalls[-MAX_KNOWN_PITFALLS:]

    return out


__all__ = [
    "MAX_BLACKLIST_SELECTORS",
    "MAX_FINGERPRINTS",
    "MAX_KNOWN_PITFALLS",
    "SiteProfile",
    "apply_time_decay",
    "update_profile_from_episode",
]
