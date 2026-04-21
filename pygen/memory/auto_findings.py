"""Zero-token heuristic scans that produce a "model self-check report".

These run in :mod:`pygen.agents.summarize_node` immediately after the
LangGraph main loop ends. They look at the tool-call log, the verified
selectors ledger, the critic verdict, and the generated code, and try
to spot three classes of trouble *without* invoking any LLM:

* ``redundant_tool_calls`` — the planner called the same probing tool
  with the same arguments twice in a row, or invoked equivalent tools
  back-to-back. Wastes the iteration budget and signals planning bugs.

* ``suspected_failures`` — the run produced "looks-okay" code that
  the heuristic believes will silently scrape junk. The Sec-Zambia
  failure mode is the canonical example: every record's ``sourceUrl``
  ends up equal to the base URL because the `<a>` selector returned
  ``None`` for every list item.

* ``redundant_code_blocks`` — duplicate try/except scaffolding,
  obviously copy-pasted helper functions, etc. Surface so the user can
  flag overly verbose generations.

These findings are shown to the user in the feedback Modal so they can
make a more informed verdict, and they're also fed into the LLM at
commit time (so the model can refine the diagnosis using the user's
plain-English complaint).

All scanners are exception-safe: if a particular check explodes we
return ``[]`` for that bucket and continue.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_auto_findings(
    state: Dict[str, Any],
    *,
    ctx: Optional[Any] = None,
    generated_code: Optional[str] = None,
) -> Dict[str, List[str]]:
    """Run all heuristic scans and return a {bucket: [findings]} dict.

    The dict shape is stable: empty buckets are still present so the
    caller can render them uniformly.
    """
    state = state or {}
    code = generated_code if generated_code is not None else (state.get("generated_code") or "")
    tool_calls = list(state.get("tool_calls_log") or [])
    critic_verdict = state.get("critic_verdict") or {}
    critic_evidence = list(state.get("critic_evidence") or [])

    findings: Dict[str, List[str]] = {
        "redundant_tool_calls": [],
        "suspected_failures": [],
        "redundant_code_blocks": [],
    }

    try:
        findings["redundant_tool_calls"] = _detect_redundant_tool_calls(tool_calls)
    except Exception:
        pass
    try:
        findings["suspected_failures"] = _detect_suspected_failures(
            code, tool_calls, critic_verdict, critic_evidence,
        )
    except Exception:
        pass
    try:
        findings["redundant_code_blocks"] = _detect_redundant_code_blocks(code)
    except Exception:
        pass

    return findings


# ---------------------------------------------------------------------------
# Bucket 1: redundant tool calls
# ---------------------------------------------------------------------------

# Tools whose results we expect the agent to *cache* — calling them twice
# in a row with identical input is almost always a planning slip.
_CACHEABLE_TOOLS = (
    "verify_selector",
    "extract_list_and_pagination",
    "probe_detail_page",
    "open_page",
    "get_page_html",
    "analyze_page",
    "enhanced_page_analysis",
    "get_site_menu_tree",
    "smart_date_api_scan",
)

# Pairs of tools that probe the same surface — calling both for the same
# URL is redundant.
_OVERLAPPING_PAIRS = {
    frozenset({"analyze_page", "enhanced_page_analysis"}),
    frozenset({"get_page_html", "analyze_page"}),
    frozenset({"extract_list_and_pagination", "analyze_page"}),
}


def _input_signature(entry: Dict[str, Any]) -> str:
    """Stable string fingerprint of (tool_name, action_input) used to
    detect duplicate calls. Order-insensitive on dict keys."""
    name = str(entry.get("action") or entry.get("name") or "")
    raw = entry.get("action_input") or {}
    if isinstance(raw, dict):
        try:
            kv = sorted((str(k), repr(v)) for k, v in raw.items())
            payload = ";".join(f"{k}={v}" for k, v in kv)
        except Exception:
            payload = repr(raw)
    else:
        payload = repr(raw)
    return f"{name}::{payload}"


def _detect_redundant_tool_calls(tool_calls: List[Dict[str, Any]]) -> List[str]:
    """Heuristic: same (name, input) ≥ 2 times AND first call already
    succeeded; or two equivalent probes for the same URL within a window."""
    if not tool_calls:
        return []
    findings: List[str] = []

    # ---- Duplicate-input detection (per-tool, per-input) ----
    seen: Dict[str, Dict[str, Any]] = {}  # signature -> {first_index, count, first_success}
    for idx, entry in enumerate(tool_calls):
        name = str(entry.get("action") or entry.get("name") or "")
        if name not in _CACHEABLE_TOOLS:
            continue
        sig = _input_signature(entry)
        info = seen.get(sig)
        if info is None:
            seen[sig] = {
                "first_index": idx,
                "count": 1,
                "first_success": bool(entry.get("success", True)),
                "tool": name,
            }
            continue
        info["count"] += 1
        # Only flag if the FIRST call succeeded (so a legitimate retry of
        # a failed call doesn't get tagged as redundant).
        if info["first_success"] and info["count"] == 2:
            findings.append(
                f"`{name}` 在第 {info['first_index'] + 1} 步成功后又被重复调用了相同参数 "
                f"(第 {idx + 1} 步)，第一次结果应当复用而非重新探测"
            )

    # ---- Overlapping-pairs detection ----
    used_overlap: set = set()
    for i in range(len(tool_calls) - 1):
        n1 = str(tool_calls[i].get("action") or "")
        for j in range(i + 1, min(i + 6, len(tool_calls))):
            n2 = str(tool_calls[j].get("action") or "")
            if not n1 or not n2 or n1 == n2:
                continue
            pair = frozenset({n1, n2})
            if pair in _OVERLAPPING_PAIRS and pair not in used_overlap:
                used_overlap.add(pair)
                findings.append(
                    f"`{n1}` 与 `{n2}` 都在探测同一个页面的结构 (第 {i + 1} / {j + 1} 步)，"
                    f"这两个工具功能重叠，保留其中一个即可"
                )
    return findings


# ---------------------------------------------------------------------------
# Bucket 2: suspected failures hidden in "successful" code
# ---------------------------------------------------------------------------


def _detect_suspected_failures(
    code: str,
    tool_calls: List[Dict[str, Any]],
    critic_verdict: Dict[str, Any],
    critic_evidence: List[Dict[str, Any]],
) -> List[str]:
    findings: List[str] = []

    # ---- Sec-Zambia smell: every detail URL falls back to baseUrl ----
    # If the code uses urljoin(base, link) and the link extraction lacks
    # a non-trivial selector, we suspect every record's URL == base.
    if isinstance(code, str) and code.strip():
        # Look for href/source extraction patterns that catch nothing
        suspect_patterns = [
            (
                r"\.locator\([^)]*\)\.first",
                ".first 兜底",
            ),
            (
                r"item\.locator\(['\"]a['\"]\)\.first",
                "通用 item.locator('a').first 抓取链接，未指定具体选择器",
            ),
        ]
        for pattern, desc in suspect_patterns:
            if re.search(pattern, code):
                # Only flag if the planner did NOT verify a list.title_link
                # selector (otherwise the fallback is intentional).
                # Best-effort: scan tool_calls for a verify_selector success
                # mentioning "title" or "link".
                verified_link = any(
                    str(tc.get("action") or "") == "verify_selector"
                    and tc.get("success") is True
                    and any(
                        kw in str(tc.get("action_input", "")).lower()
                        for kw in ("title", "link", "href")
                    )
                    for tc in tool_calls or []
                )
                if not verified_link:
                    findings.append(
                        f"代码中出现 {desc}，但 verify_selector 未确认链接选择器 → "
                        f"详情页 URL 可能全部回退到列表页地址，疑似爬到的是图标/封面而非新闻正文"
                    )
                    break

        # urljoin(base, "") == base - explicit empty-string concern
        if re.search(r"urljoin\([^,]+,\s*['\"]\s*['\"]\)", code):
            findings.append("代码里出现 `urljoin(base, '')`，所有记录的 sourceUrl 会等于列表页地址")

    # ---- Critic passed but evidence shows zero records ----
    if critic_verdict and critic_verdict.get("passed"):
        for ev in critic_evidence:
            if not isinstance(ev, dict):
                continue
            details = ev.get("details") or ev
            for key in ("records", "record_count", "items_count", "count", "total"):
                v = details.get(key) if isinstance(details, dict) else None
                if isinstance(v, int) and v == 0:
                    findings.append(
                        f"critic 判定通过但执行证据中 `{key}=0`，疑似爬到空结果却被误判为成功"
                    )
                    break

    # ---- Critic failed — surface its primary cause ----
    if critic_verdict and not critic_verdict.get("passed"):
        cause = (critic_verdict.get("details") or {}).get("primary_cause")
        if cause:
            findings.append(f"critic 判定失败，主因: {cause}")

    return findings


# ---------------------------------------------------------------------------
# Bucket 3: redundant / duplicate code blocks
# ---------------------------------------------------------------------------


_BLOCK_MIN_LINES = 5
_BLOCK_MIN_DUPLICATES = 2


def _normalize_line(line: str) -> str:
    s = line.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _detect_redundant_code_blocks(code: Optional[str]) -> List[str]:
    """Find runs of ≥5 lines that appear ≥2 times in the generated code."""
    if not code or not isinstance(code, str):
        return []
    lines = code.splitlines()
    if len(lines) < _BLOCK_MIN_LINES * 2:
        return []
    norm = [_normalize_line(ln) for ln in lines]

    # Use a sliding window of size _BLOCK_MIN_LINES; record (block, [start_indices]).
    seen: Dict[str, List[int]] = {}
    for i in range(len(norm) - _BLOCK_MIN_LINES + 1):
        window = norm[i : i + _BLOCK_MIN_LINES]
        # Skip windows that are mostly blank or trivial (e.g. all imports)
        non_empty = [w for w in window if w]
        if len(non_empty) < _BLOCK_MIN_LINES - 1:
            continue
        key = "\n".join(window)
        seen.setdefault(key, []).append(i + 1)  # 1-indexed line numbers

    findings: List[str] = []
    for key, starts in seen.items():
        if len(starts) >= _BLOCK_MIN_DUPLICATES:
            preview = key.split("\n", 1)[0][:60]
            ranges = ", ".join(
                f"第 {s}-{s + _BLOCK_MIN_LINES - 1} 行" for s in starts[:3]
            )
            findings.append(
                f"发现重复代码块 ({len(starts)} 处, 起始行 {ranges}): `{preview}...`"
            )
            if len(findings) >= 3:
                break
    return findings


__all__ = ["run_auto_findings"]
