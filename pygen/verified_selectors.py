"""Verified-selector ledger — the bridge between probe/verify tools and codegen.

Background
----------
The Planner repeatedly calls *exploration* tools (``extract_list_and_pagination``,
``probe_detail_page``, ``verify_selector``) that return concrete CSS selectors
which were proven to work on the live page. Those selectors used to live only
inside ``ctx.enhanced_analysis`` as loose dicts, which the codegen LLM read
through prose-style summarization and routinely **discarded** in favour of its
own "common WordPress patterns" guess (`.e-loop-item`, `article`, ...). The
result was Sec-Zambia-style failures where the title text was right but the
detail-page link pointed at the homepage.

This module gives the Planner a **structured, additive ledger** of verified
selectors that travels through ``AgentState`` into the codegen subgraph and is
rendered as a hard "MUST USE" block at the top of the user prompt. Codegen has
no excuse to invent new selectors when verified ones exist.

Schema (every key is optional — empty ledger is the legitimate default state)
----------------------------------------------------------------------------
``verified_selectors`` is a dict shaped like::

    {
      "list":   {                         # list / index page
        "container":              str,    # repeating list-item selector
        "container_alternatives": [str],  # bare/qualified variants
        "title_link":             str,    # the <a> carrying the article URL
        "title":                  str,    # title text (often same node as link)
        "date":                   str,
        "next_page":              str,    # pagination
      },
      "detail": {                         # article / detail page
        "content":                str,    # body container
        "content_alternatives":   [str],
        "title":                  str,
        "publish_date":           str,
      },
      "_provenance": {                    # how each named slot got its value
        "<dotted.path>": {
          "source": "extract_list_and_pagination | probe_detail_page | verify_selector",
          "total":   int,                 # verify_selector evidence
          "visible": int,
          "score":   int,                 # extract_list scoring (0..100)
          "ts":      iso8601 str,
        },
      },
      "_ad_hoc_verifications": [          # verify_selector calls that didn't
        {                                 # match a known slot are kept here
          "selector": str,
          "description": str,
          "total":    int,
          "visible":  int,
          "ts":       iso8601 str,
        },
      ],
    }

Design contract
---------------
* All ``merge_*`` helpers are pure: they take the current ledger and a tool
  payload and return a NEW ledger dict (no mutation, no I/O). This keeps them
  trivially testable and lets LangGraph diffing work without surprises.
* Empty / partial inputs never raise — missing keys silently noop. The
  Planner can call exploration tools in any order with any subset of fields.
* Promotion to a named slot requires *positive* evidence:
  ``extract_list_and_pagination`` requires ``itemCount > 0``; ``verify_selector``
  requires ``total > 0`` AND ``visible > 0``. Failed verifications are kept
  in ``_ad_hoc_verifications`` for debugging but never overwrite a slot.
* Once a slot is filled, it can be **upgraded** by stronger evidence
  (verify_selector beats extract_list beats probe_detail), but never
  silently downgraded. We keep both the current selector and the source
  in ``_provenance`` so codegen can show its work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def bootstrap() -> Dict[str, Any]:
    """Return an empty ledger with the canonical top-level keys present."""
    return {
        "list": {},
        "detail": {},
        "_provenance": {},
        "_ad_hoc_verifications": [],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure(led: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Defensively coerce ``led`` into the canonical shape."""
    if not isinstance(led, dict):
        return bootstrap()
    out = dict(led)  # shallow copy: callers must not see mutations
    out.setdefault("list", {})
    out.setdefault("detail", {})
    out.setdefault("_provenance", {})
    out.setdefault("_ad_hoc_verifications", [])
    if not isinstance(out["list"], dict):
        out["list"] = {}
    if not isinstance(out["detail"], dict):
        out["detail"] = {}
    if not isinstance(out["_provenance"], dict):
        out["_provenance"] = {}
    if not isinstance(out["_ad_hoc_verifications"], list):
        out["_ad_hoc_verifications"] = []
    out["list"] = dict(out["list"])
    out["detail"] = dict(out["detail"])
    out["_provenance"] = dict(out["_provenance"])
    out["_ad_hoc_verifications"] = list(out["_ad_hoc_verifications"])
    return out


# ---------------------------------------------------------------------------
# Source ranking — used to decide whether new evidence may overwrite an old
# slot value. Higher rank wins; ties resolve in favour of the new value.
# ---------------------------------------------------------------------------

_SOURCE_RANK = {
    "probe_detail_page": 1,
    "extract_list_and_pagination": 2,
    "verify_selector": 3,  # strongest: explicit live-DOM confirmation
}


def _rank(provenance: Dict[str, Any]) -> int:
    return _SOURCE_RANK.get((provenance or {}).get("source", ""), 0)


def _set_slot(
    led: Dict[str, Any],
    section: str,
    key: str,
    value: Any,
    *,
    source: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Assign ``value`` to ``led[section][key]`` if (a) slot empty OR
    (b) new source has rank >= existing source. Always update provenance."""
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return

    path = f"{section}.{key}"
    existing_prov = led["_provenance"].get(path)
    new_prov = {"source": source, "ts": _now_iso()}
    if extra:
        new_prov.update(extra)

    if section not in led:
        led[section] = {}
    section_dict = led[section]
    has_value = bool(section_dict.get(key))

    if not has_value or _rank(new_prov) >= _rank(existing_prov):
        section_dict[key] = value
        led["_provenance"][path] = new_prov


# ---------------------------------------------------------------------------
# Mergers — one per source tool
# ---------------------------------------------------------------------------


def merge_from_extract_list(
    led: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fold ``tool_extract_list_and_pagination`` output into the ledger.

    Reads ``payload['bestCandidate']`` for the primary list selectors and
    ``payload['pagination']`` for the next-page selector. Silent on
    missing/partial input.
    """
    out = _ensure(led)
    if not isinstance(payload, dict):
        return out

    best = payload.get("bestCandidate") or {}
    if not isinstance(best, dict):
        best = {}

    item_count = int(best.get("itemCount") or 0)
    if item_count <= 0:
        # No positive evidence — don't promote, but still record pagination if any.
        pass
    else:
        score = int(best.get("score") or 0)
        extra = {"score": score, "item_count": item_count}
        _set_slot(
            out, "list", "container", best.get("selector"),
            source="extract_list_and_pagination", extra=extra,
        )
        _set_slot(
            out, "list", "title_link", best.get("titleSelector"),
            source="extract_list_and_pagination", extra=extra,
        )
        _set_slot(
            out, "list", "date", best.get("dateSelector"),
            source="extract_list_and_pagination", extra=extra,
        )

        alts: List[str] = []
        bare = best.get("bareSelector")
        if bare and bare != best.get("selector"):
            alts.append(str(bare))
        for c in best.get("candidateSelectors") or []:
            sel = (c or {}).get("selector") if isinstance(c, dict) else None
            if sel and sel != best.get("selector") and sel not in alts:
                alts.append(str(sel))
        if alts:
            out["list"]["container_alternatives"] = alts[:8]

    pagination = payload.get("pagination") or {}
    if isinstance(pagination, dict):
        next_block = pagination.get("next") or {}
        if isinstance(next_block, dict):
            sel = next_block.get("selector") or next_block.get("css")
            if sel:
                _set_slot(
                    out, "list", "next_page", str(sel),
                    source="extract_list_and_pagination",
                    extra={"hint": next_block.get("url") or next_block.get("text") or ""},
                )

    return out


def merge_from_probe_detail(
    led: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fold ``tool_probe_detail_page`` output into the ledger."""
    out = _ensure(led)
    if not isinstance(payload, dict):
        return out

    content_sel = payload.get("contentSelector")
    if content_sel:
        text_len = int(payload.get("contentTextLength") or 0)
        _set_slot(
            out, "detail", "content", content_sel,
            source="probe_detail_page",
            extra={"text_length": text_len},
        )

    title_sel = payload.get("titleSelector")
    if title_sel:
        _set_slot(
            out, "detail", "title", title_sel,
            source="probe_detail_page",
        )

    candidates = payload.get("contentCandidates") or []
    if isinstance(candidates, list) and candidates:
        alts: List[str] = []
        for c in candidates:
            sel = None
            if isinstance(c, dict):
                sel = c.get("selector")
            elif isinstance(c, str):
                sel = c
            if sel and sel != content_sel and sel not in alts:
                alts.append(sel)
        if alts:
            out["detail"]["content_alternatives"] = alts[:10]

    return out


# ---- verify_selector promotion heuristic --------------------------------


_DESC_RULES: Tuple[Tuple[str, Tuple[str, str]], ...] = (
    # Order matters: first match wins. Patterns are case-insensitive.
    # ---- detail-page (must come before generic "container" / "title")
    # so "body container candidate" maps to detail.content, not list.container.
    ("article body",     ("detail", "content")),
    ("article content",  ("detail", "content")),
    ("body container",   ("detail", "content")),
    ("body",             ("detail", "content")),
    ("article",          ("detail", "content")),
    ("content",          ("detail", "content")),
    # ---- pagination (must come before generic "link" so "next page link"
    # routes to next_page, not title_link)
    ("next page",        ("list", "next_page")),
    ("pagination",       ("list", "next_page")),
    ("page-numbers",     ("list", "next_page")),
    # ---- list-page links (must come before generic "title")
    ("title link",       ("list", "title_link")),
    ("title-link",       ("list", "title_link")),
    ("read more",        ("list", "title_link")),
    ("detail link",      ("list", "title_link")),
    ("article link",     ("list", "title_link")),
    ("link",             ("list", "title_link")),
    # ---- list-page container (generic, last among container patterns)
    ("list item",        ("list", "container")),
    ("list-item",        ("list", "container")),
    ("list container",   ("list", "container")),
    ("item selector",    ("list", "container")),
    ("card",             ("list", "container")),
    ("row",              ("list", "container")),
    ("container",        ("list", "container")),
    # ---- date
    ("publish date",     ("list", "date")),
    ("publish-date",     ("list", "date")),
    ("date",             ("list", "date")),
    ("time",             ("list", "date")),
    # ---- catch-all titles (last; ambiguous)
    ("title",            ("list", "title")),
)


def _classify_description(description: str) -> Optional[Tuple[str, str]]:
    """Map a verify_selector description like 'Check title and link' to a
    ``(section, key)`` slot. Returns ``None`` when nothing matches."""
    if not description:
        return None
    d = description.lower()
    for needle, slot in _DESC_RULES:
        if needle in d:
            return slot
    return None


def merge_from_verify_selector(
    led: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fold ``tool_verify_selector`` output into the ledger.

    Promotes the selector to a named slot when the ``description`` matches
    a known pattern AND the selector matched at least one visible element.
    Otherwise keeps the verification in ``_ad_hoc_verifications`` for
    debugging."""
    out = _ensure(led)
    if not isinstance(payload, dict):
        return out

    selector = payload.get("selector")
    if not selector:
        return out

    total = int(payload.get("totalMatches") or 0)
    visible = int(payload.get("visibleMatches") or 0)
    description = str(payload.get("description") or "").strip()
    ts = _now_iso()

    # Always remember the verification fact so debugging can see what was tried.
    out["_ad_hoc_verifications"].append(
        {
            "selector": selector,
            "description": description,
            "total": total,
            "visible": visible,
            "ts": ts,
        }
    )
    # Cap to last 25 to avoid unbounded state growth.
    if len(out["_ad_hoc_verifications"]) > 25:
        out["_ad_hoc_verifications"] = out["_ad_hoc_verifications"][-25:]

    if total <= 0 or visible <= 0:
        return out  # negative evidence — don't promote

    slot = _classify_description(description)
    if slot is None:
        return out

    section, key = slot
    _set_slot(
        out, section, key, selector,
        source="verify_selector",
        extra={"total": total, "visible": visible, "description": description},
    )
    return out


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


_SECTION_LABELS = {
    "list": "List page",
    "detail": "Detail page",
}

_FIELD_LABELS = {
    "container":              "list-item container",
    "container_alternatives": "container alternatives",
    "title_link":             "title link (carries the article URL)",
    "title":                  "title text",
    "date":                   "publish date (list)",
    "next_page":              "next-page button / link",
    "content":                "article body container",
    "content_alternatives":   "body alternatives",
    "publish_date":           "publish date (detail)",
}


def _format_provenance(prov: Dict[str, Any]) -> str:
    if not isinstance(prov, dict):
        return ""
    src = prov.get("source", "?")
    bits = [f"source={src}"]
    if "total" in prov:
        bits.append(f"total={prov['total']}")
    if "visible" in prov:
        bits.append(f"visible={prov['visible']}")
    if "score" in prov:
        bits.append(f"score={prov['score']}")
    if "item_count" in prov:
        bits.append(f"items={prov['item_count']}")
    if "text_length" in prov:
        bits.append(f"text_len={prov['text_length']}")
    return ", ".join(bits)


def render_for_prompt(led: Optional[Dict[str, Any]]) -> str:
    """Render the ledger as a strict 'MUST USE' block for the codegen prompt.

    Returns ``""`` if the ledger has nothing useful — caller should then
    skip the whole section so the prompt isn't polluted with empty headers.

    The rendered text intentionally uses imperative language because it
    sits at the very top of the user prompt and is the single most
    important contract for the codegen LLM."""
    led = _ensure(led)
    sections_with_data = [
        s for s in ("list", "detail") if any(led.get(s, {}).values())
    ]
    if not sections_with_data:
        return ""

    out: List[str] = [
        "## 【强约束】已验证的选择器（必须 100% 复用，禁止改写）",
        "",
        "下列 CSS 选择器均已通过 `verify_selector` / `extract_list_and_pagination` /",
        "`probe_detail_page` 在真实浏览器中验证成功（含命中数证据）。生成代码时**必须**：",
        "",
        "1. 将这些选择器**逐字写入代码**，不得拼接成 `selector_a, selector_b, ...` 的 OR 串，",
        "   不得改成 `.first` 之外的兜底（如 `item.locator('a').first`），不得换成 `.e-loop-item /",
        "   article / .post / .entry-title` 等通用模板猜测。",
        "2. 对所有列表项的链接抽取，必须使用 `list.title_link`（如有），其它链接源一律忽略。",
        "3. 详情页正文抽取依次尝试 `detail.content` 与 `detail.content_alternatives`；",
        "   `detail.title` 用作标题选择器（若 list 已给则可保留 list 的）。",
        "4. 翻页必须使用 `list.next_page`（若提供）；不得自创 `a.next` 等通用兜底。",
        "5. 若下表中**没有**某字段，则可按常规启发式自行决定，但必须在代码注释里说明来源。",
        "",
    ]

    for section in sections_with_data:
        items = led.get(section) or {}
        if not items:
            continue
        out.append(f"### {_SECTION_LABELS[section]}")
        out.append("")
        for key, val in items.items():
            label = _FIELD_LABELS.get(key, key)
            prov = led["_provenance"].get(f"{section}.{key}", {})
            prov_text = _format_provenance(prov)
            if isinstance(val, list):
                if not val:
                    continue
                out.append(f"- **{label}** ({prov_text}):")
                for v in val:
                    out.append(f"  - `{v}`")
            else:
                if prov_text:
                    out.append(f"- **{label}**: `{val}` ({prov_text})")
                else:
                    out.append(f"- **{label}**: `{val}`")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


__all__ = [
    "bootstrap",
    "merge_from_extract_list",
    "merge_from_probe_detail",
    "merge_from_verify_selector",
    "render_for_prompt",
]
