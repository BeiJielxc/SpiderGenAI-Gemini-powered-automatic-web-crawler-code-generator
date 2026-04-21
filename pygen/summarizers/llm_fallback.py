"""Small-model LLM fallback for artifact summaries.

Rule-based summarizers (``summarize_html``, ``summarize_json_payload``,
``summarize_analyze_page``) are fast, deterministic and free of LLM
tokens. But when the page structure is unusual (SPA shells, JSON dumped
into ``<pre>``, anti-bot interstitials, exotic layouts) the rule output
can come back almost empty, which leaves the planner LLM blind and
forces it to call ``read_artifact`` repeatedly — defeating the whole
working-memory optimization.

This module wraps a *small / cheap* LLM (configured via
``artifacts.small_model.alias`` in ``config.yaml``) and asks it to fill
in the missing decision signals **only when the rule summary is weak**.
The output is merged on top of the rule summary so the rule signals
always win where they exist.

The integration is gated by ``artifacts.small_model.enabled`` and is
defensive: any LLM error (timeout, auth, bad JSON, …) is swallowed and
the original rule summary is returned untouched. In other words, the
fallback can only *improve* a summary, never break the pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Weakness probe
# ---------------------------------------------------------------------------

_HTML_KEY_SIGNALS = ("list_candidates", "pagination", "date_signals", "meta")
_JSON_KEY_SIGNALS = ("candidate_endpoints", "top_hosts", "status_distribution")
_ANALYZE_KEY_SIGNALS = ("list_candidates", "candidate_endpoints", "date_signals")


def is_weak_summary(rule_summary: Dict[str, Any], kind: str = "html") -> bool:
    """Return True if the rule summarizer failed to extract anything actionable.

    ``kind`` is one of ``"html"`` / ``"json"`` / ``"analyze"`` and selects
    the set of "must have at least one of" keys.

    This is intentionally conservative: an empty ``list_candidates`` on a
    detail page is fine if ``date_signals`` is populated. We only flag a
    summary as weak when *all* the kind-specific signal buckets are empty
    or missing (or when ``_summary_error`` is set).
    """
    if not isinstance(rule_summary, dict):
        return True
    if rule_summary.get("_summary_error"):
        return True

    if kind == "html":
        keys = _HTML_KEY_SIGNALS
    elif kind == "json":
        keys = _JSON_KEY_SIGNALS
    elif kind == "analyze":
        keys = _ANALYZE_KEY_SIGNALS
    else:
        keys = _HTML_KEY_SIGNALS

    for k in keys:
        v = rule_summary.get(k)
        if isinstance(v, (list, dict)) and len(v) > 0:
            return False
        if isinstance(v, str) and v.strip():
            return False
    return True


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a *summary booster* for a web-scraping agent's working memory. "
    "A rule-based extractor already produced a summary dict, but it is weak "
    "(missing list candidates, pagination, dates or endpoints). Your job is "
    "to read a SHORT excerpt of the raw artifact and propose **candidate** "
    "signals as compact JSON. You must never invent data: only extract what "
    "is actually present in the excerpt. Output strict JSON only — no prose, "
    "no markdown fences."
)

_USER_TEMPLATE_HTML = (
    "Artifact kind: HTML\n"
    "Existing rule summary keys: {rule_keys}\n\n"
    "Return JSON with these optional keys (omit any you cannot fill):\n"
    "  list_candidates: list of objects {{selector, count, sample_text}} (max 5)\n"
    "  pagination: object {{type, hint}} where type ∈ {{'numeric','next','load_more','infinite_scroll'}}\n"
    "  date_signals: list of {{value, format}} (max 10, distinct)\n"
    "  detail_link_pattern: string regex or url-template you observe\n"
    "  notes: short free-text observation (≤140 chars)\n\n"
    "Raw HTML excerpt (truncated):\n```\n{excerpt}\n```\n"
)

_USER_TEMPLATE_JSON = (
    "Artifact kind: JSON / network capture\n"
    "Existing rule summary keys: {rule_keys}\n\n"
    "Return JSON with these optional keys (omit any you cannot fill):\n"
    "  candidate_endpoints: list of {{url, method, why}} (max 5)\n"
    "  list_field_path: dotted path to the list of items if you can spot one\n"
    "  pagination_param: name of the query/body param that controls page\n"
    "  date_field: field name(s) most likely to carry the publish date\n"
    "  notes: short free-text observation (≤140 chars)\n\n"
    "Raw payload excerpt (truncated):\n```\n{excerpt}\n```\n"
)


def _coerce_excerpt(content: Any, max_chars: int) -> str:
    """Best-effort flatten of ``content`` to a single string capped to ``max_chars``."""
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
    elif isinstance(content, (bytes, bytearray)):
        try:
            s = content.decode("utf-8", errors="replace")
        except Exception:
            s = str(content)
    elif isinstance(content, dict):
        # Common shape: {"html": "..."} -> prefer the html field if present
        for k in ("html", "raw", "text", "body", "content"):
            v = content.get(k)
            if isinstance(v, str) and v:
                return _coerce_excerpt(v, max_chars)
        try:
            s = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            s = str(content)
    else:
        try:
            s = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            s = str(content)
    s = s.strip()
    if len(s) <= max_chars:
        return s
    half = max_chars // 2
    return s[:half] + "\n...[truncated]...\n" + s[-half:]


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _parse_llm_json(text: str) -> Optional[Dict[str, Any]]:
    """Robustly extract the first top-level JSON object from ``text``."""
    if not text:
        return None
    text = text.strip()
    # Strip markdown fences if the model added them anyway.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # Fall back: find first {...} block.
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Whitelist of keys we are willing to merge from the LLM. Anything else is
# silently dropped to avoid prompt-injection writing weird fields into the
# planner-visible summary.
_HTML_ALLOWED = (
    "list_candidates",
    "pagination",
    "date_signals",
    "detail_link_pattern",
    "notes",
)
_JSON_ALLOWED = (
    "candidate_endpoints",
    "list_field_path",
    "pagination_param",
    "date_field",
    "notes",
)


def _allowed_keys(kind: str) -> tuple:
    if kind == "json":
        return _JSON_ALLOWED
    if kind == "analyze":
        return _HTML_ALLOWED + _JSON_ALLOWED
    return _HTML_ALLOWED


def enrich_summary_via_llm(
    content: Any,
    rule_summary: Dict[str, Any],
    *,
    kind: str = "html",
    model_factory: Optional[Callable[[], Any]] = None,
    raw_excerpt_chars: int = 6000,
    max_tokens: int = 800,
    timeout_sec: int = 15,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Return a *merged* summary: rule signals + LLM-supplied candidates.

    On any failure, returns ``rule_summary`` unchanged with an extra
    ``_fallback_skipped: <reason>`` marker so we can tell from the
    artifact metadata whether the LLM ran. On success, sets
    ``_summary_provenance: 'rule+llm'`` and merges only whitelisted keys.
    """
    if not isinstance(rule_summary, dict):
        rule_summary = {}

    if model_factory is None:
        merged = dict(rule_summary)
        merged["_fallback_skipped"] = "no model_factory"
        return merged

    excerpt = _coerce_excerpt(content, raw_excerpt_chars)
    if not excerpt:
        merged = dict(rule_summary)
        merged["_fallback_skipped"] = "empty content"
        return merged

    rule_keys = sorted(k for k in rule_summary.keys() if not k.startswith("_"))
    template = _USER_TEMPLATE_JSON if kind == "json" else _USER_TEMPLATE_HTML
    user_msg = template.format(rule_keys=rule_keys or "[]", excerpt=excerpt)

    try:
        model = model_factory()
    except Exception as exc:
        merged = dict(rule_summary)
        merged["_fallback_skipped"] = f"model build failed: {exc}"
        if log:
            log(f"[SUMMARIZER-LLM] skipped (model build): {exc}")
        return merged

    # We don't want a hard dep on langchain_core.messages here; build_chat_model
    # returns a BaseChatModel that accepts the OpenAI-style message dict list.
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        # Apply per-call max_tokens / timeout via .bind() so we don't have
        # to rebuild the model for each artifact.
        bound = model
        try:
            bound = model.bind(max_tokens=max_tokens, timeout=timeout_sec)
        except Exception:
            pass
        resp = bound.invoke(messages)
        text = getattr(resp, "content", None) or str(resp)
        if isinstance(text, list):
            # Some providers return a list of content parts.
            text = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in text
            )
    except Exception as exc:
        merged = dict(rule_summary)
        merged["_fallback_skipped"] = f"invoke failed: {exc.__class__.__name__}"
        if log:
            log(f"[SUMMARIZER-LLM] skipped (invoke): {exc}")
        return merged

    parsed = _parse_llm_json(text)
    if not parsed:
        merged = dict(rule_summary)
        merged["_fallback_skipped"] = "non-json response"
        if log:
            log("[SUMMARIZER-LLM] skipped: response was not parseable JSON")
        return merged

    allowed = _allowed_keys(kind)
    addons: Dict[str, Any] = {}
    for k in allowed:
        if k not in parsed:
            continue
        v = parsed[k]
        # Sanity caps so a runaway model can't blow up the summary size.
        if isinstance(v, list):
            addons[k] = v[:10]
        elif isinstance(v, str):
            addons[k] = v[:280]
        else:
            addons[k] = v

    if not addons:
        merged = dict(rule_summary)
        merged["_fallback_skipped"] = "no usable keys"
        return merged

    # Merge: rule summary wins on existing non-empty keys; LLM fills gaps.
    merged = dict(rule_summary)
    for k, v in addons.items():
        existing = merged.get(k)
        is_empty = (
            existing is None
            or (isinstance(existing, (list, dict, str)) and len(existing) == 0)
        )
        if is_empty:
            merged[k] = v

    merged["_summary_provenance"] = "rule+llm"
    merged["_llm_fallback_used"] = sorted(addons.keys())
    return merged


__all__ = ["enrich_summary_via_llm", "is_weak_summary"]
