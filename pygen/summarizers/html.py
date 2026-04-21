"""Rule-based HTML artifact summarizer.

Produces structured "decision signals" for the planner LLM without
spending any extra LLM tokens. Reuses the existing high-level-tool
heuristics where available so behavior stays consistent with the
``extract_list_and_pagination`` tool.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag


_DATE_PATTERNS = [
    (re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"), "YYYY-MM-DD"),
    (re.compile(r"\b\d{4}/\d{1,2}/\d{1,2}\b"), "YYYY/MM/DD"),
    (re.compile(r"\b\d{4}\.\d{1,2}\.\d{1,2}\b"), "YYYY.MM.DD"),
    (re.compile(r"\b\d{4}年\d{1,2}月\d{1,2}日"), "YYYY年MM月DD日"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), "DD/MM/YYYY or MM/DD/YYYY"),
    (re.compile(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    ), "Mon DD YYYY"),
]

_ANTIBOT_KEYWORDS = [
    ("cloudflare", ["cloudflare", "cf-ray", "checking your browser"]),
    ("captcha", ["captcha", "recaptcha", "hcaptcha", "geetest", "tencent captcha"]),
    ("login_wall", ["please log in", "请登录", "sign in to continue"]),
    ("js_required", ["please enable javascript", "javascript is required"]),
    ("403_forbidden", ["403 forbidden", "access denied"]),
    ("waf_block", ["waf", "request blocked", "incident id"]),
]


def summarize_html(content: Any, url: str = "") -> Dict[str, Any]:
    """Summarize an HTML artifact into structured candidate signals.

    ``content`` may be a raw HTML string OR a dict like ``{"html": "..."}``
    (the shape produced by ``tool_get_page_html``). Both are handled.
    """
    try:
        html = _coerce_html(content)
        if not html:
            return {"_summary_error": "no html content"}

        soup = BeautifulSoup(html, "html.parser")
        body_text = _body_text(soup)

        head_meta = _head_meta(soup)
        title = head_meta.get("title", "") or ""
        list_candidates = _list_candidates(soup, base_url=url)
        pagination = _pagination_signals(soup)
        date_signals = _date_signals(soup)
        antibot = _antibot_signals(html, body_text)
        kind = _guess_page_kind(soup, body_text, list_candidates, antibot, head_meta)

        result: Dict[str, Any] = {
            "url": url or head_meta.get("canonical", ""),
            "title": title,
            "rendered_size_chars": len(html),
            "body_text_chars": len(body_text),
            "page_kind_guess": kind,
            "list_candidates": list_candidates,
            "pagination_signals": pagination,
            "date_signals": date_signals,
            "anti_bot_signals": antibot,
            "head_meta": head_meta,
            "preview_head": body_text[:800],
            "preview_tail": body_text[-400:] if len(body_text) > 400 else "",
        }
        result["fallback_hints"] = _fallback_hints(result)
        return result
    except Exception as exc:  # pragma: no cover - defensive belt
        return {"_summary_error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_html(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("html", "page_html", "content", "body"):
            v = content.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _body_text(soup: BeautifulSoup) -> str:
    body = soup.body if soup.body else soup
    text = body.get_text(separator=" ", strip=True) if body else ""
    return re.sub(r"\s+", " ", text).strip()


def _head_meta(soup: BeautifulSoup) -> Dict[str, Any]:
    title_tag = soup.find("title")
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    canon = soup.find("link", attrs={"rel": re.compile("^canonical$", re.I)})
    scripts: List[str] = []
    for s in soup.find_all("script", src=True):
        src = s.get("src", "")
        if src:
            scripts.append(src[:160])
        if len(scripts) >= 10:
            break
    return {
        "title": (title_tag.get_text(strip=True) if title_tag else "")[:200],
        "description": (desc.get("content", "") if desc else "")[:300],
        "canonical": canon.get("href", "") if canon else "",
        "scripts": scripts,
    }


def _list_candidates(soup: BeautifulSoup, base_url: str = "") -> List[Dict[str, Any]]:
    """Top-N repeating-block candidates with selector + sample text.

    Tries to reuse ``high_level_tools._discover_list_candidates`` for
    parity with the existing ``extract_list_and_pagination`` tool. If
    that import fails (e.g. when called from a unit test that sets up a
    different sys.path), falls back to a built-in scorer.
    """
    try:
        try:
            from high_level_tools import _discover_list_candidates  # type: ignore
        except ImportError:  # package-style fallback
            from ..high_level_tools import _discover_list_candidates  # type: ignore
        cands = _discover_list_candidates(soup, base_url or "") or []
    except Exception:
        cands = _builtin_list_candidates(soup)

    out: List[Dict[str, Any]] = []
    for c in cands[:5]:
        sample_items = (c.get("items") or [])[:2]
        sample_text = " | ".join(
            (it.get("title") or "").strip()[:80] for it in sample_items if it
        )
        out.append({
            "selector": c.get("selector") or _first_candidate_selector(c),
            "count": int(c.get("count") or len(c.get("items") or [])),
            "score": int(c.get("score") or 0),
            "has_link": bool(c.get("hasLink")),
            "has_date": bool(c.get("hasDate")),
            "sample_text": sample_text,
        })
    return out


def _first_candidate_selector(c: Dict[str, Any]) -> str:
    cs = c.get("candidateSelectors") or []
    if cs and isinstance(cs[0], dict):
        return cs[0].get("selector", "") or ""
    if cs and isinstance(cs[0], str):
        return cs[0]
    return ""


def _builtin_list_candidates(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Lightweight fallback when high_level_tools is not importable."""
    out: List[Dict[str, Any]] = []
    for parent in soup.find_all(["div", "ul", "ol", "section", "main"]):
        groups: Dict[str, List[Tag]] = {}
        for child in parent.children:
            if not isinstance(child, Tag):
                continue
            key = child.name + "|" + ".".join(child.get("class", []))
            groups.setdefault(key, []).append(child)
        for key, blocks in groups.items():
            if len(blocks) >= 3:
                tag, cls = key.split("|", 1)
                selector = tag + ("." + cls.replace(" ", ".") if cls else "")
                has_link = sum(1 for b in blocks if b.find("a", href=True)) >= len(blocks) * 0.6
                has_date = any(_DATE_PATTERNS[0][0].search(b.get_text(" ", strip=True) or "") for b in blocks)
                out.append({
                    "selector": selector,
                    "count": len(blocks),
                    "score": len(blocks) + (20 if has_link else 0) + (10 if has_date else 0),
                    "hasLink": has_link,
                    "hasDate": has_date,
                    "items": [{"title": (b.find("a") or b).get_text(" ", strip=True)[:80]} for b in blocks[:2]],
                })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:5]


def _pagination_signals(soup: BeautifulSoup) -> Dict[str, Any]:
    """Compact pagination signals (next link, page param, load-more, infinite scroll hints)."""
    try:
        try:
            from high_level_tools import _discover_pagination  # type: ignore
        except ImportError:
            from ..high_level_tools import _discover_pagination  # type: ignore
        full = _discover_pagination(soup, "") or {}
        next_info = full.get("next") or None
        next_url = (next_info or {}).get("url") or (next_info or {}).get("href") or ""
        page_param = _detect_page_param(next_url)
        return {
            "next_link": (next_info or {}).get("href") or next_url or None,
            "next_text": (next_info or {}).get("text") or None,
            "page_param": page_param,
            "page_nums_count": len(full.get("pageNums") or []),
            "total_pages": full.get("totalPages") or 0,
            "load_more": _has_load_more(soup),
            "infinite_scroll": _has_infinite_scroll(soup),
        }
    except Exception as exc:
        return {"_error": str(exc)}


def _detect_page_param(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"[?&](page|p|pageNo|pageNum|page_num|pn|offset|start)=", url, re.I)
    return m.group(1) if m else None


def _has_load_more(soup: BeautifulSoup) -> Optional[str]:
    btn = soup.find(
        ["button", "a"],
        string=re.compile(r"(load\s*more|加载更多|查看更多|更多)", re.I),
    )
    if btn:
        cls = " ".join(btn.get("class", []))
        return f"<{btn.name} class='{cls}'>"
    return None


def _has_infinite_scroll(soup: BeautifulSoup) -> bool:
    for s in soup.find_all("script", src=True):
        src = (s.get("src") or "").lower()
        if any(k in src for k in ("infinite", "waypoint", "intersection")):
            return True
    return False


def _date_signals(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Find date-bearing elements and infer their format."""
    found: List[Dict[str, Any]] = []
    seen_selectors: set = set()
    candidates = soup.find_all(
        ["time", "span", "div", "p", "small", "li"],
        limit=400,
    )
    for el in candidates:
        text = el.get_text(" ", strip=True) or ""
        if not text or len(text) > 80:
            continue
        for pat, fmt in _DATE_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            cls = ".".join(el.get("class", []))
            sel = f"{el.name}" + (f".{cls}" if cls else "")
            if sel in seen_selectors:
                # collect another sample but don't list duplicates
                for f in found:
                    if f["selector"] == sel:
                        if m.group(0) not in f["samples"] and len(f["samples"]) < 3:
                            f["samples"].append(m.group(0))
                        break
                break
            seen_selectors.add(sel)
            found.append({
                "selector": sel,
                "format": fmt,
                "samples": [m.group(0)],
            })
            break
        if len(found) >= 5:
            break
    return found


def _antibot_signals(html: str, body_text: str) -> List[str]:
    hits: List[str] = []
    haystack = (html[:8000] + " " + body_text[:4000]).lower()
    for label, keywords in _ANTIBOT_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            hits.append(label)
    return hits


def _guess_page_kind(
    soup: BeautifulSoup,
    body_text: str,
    list_candidates: List[Dict[str, Any]],
    antibot: List[str],
    head_meta: Dict[str, Any],
) -> str:
    if antibot:
        if "captcha" in antibot or "cloudflare" in antibot or "waf_block" in antibot:
            return "blocked"
        if "login_wall" in antibot:
            return "login"
    body_len = len(body_text)
    has_lists = any(c.get("count", 0) >= 5 for c in list_candidates)
    if body_len < 400 and head_meta.get("scripts"):
        return "spa_shell"
    if has_lists:
        return "list"
    if body_len > 1500 and not has_lists:
        return "detail"
    return "unknown"


def _fallback_hints(result: Dict[str, Any]) -> List[str]:
    """Suggest concrete read_artifact invocations based on what we found."""
    hints: List[str] = []
    for cand in result.get("list_candidates") or []:
        sel = cand.get("selector")
        if sel:
            hints.append(f"read_artifact(id, scope='css:{sel}')")
            break
    if result.get("page_kind_guess") == "spa_shell":
        hints.append("read_artifact(id, scope='head:4000')  # SPA shell — inspect script tags")
    if not hints:
        hints.append("read_artifact(id, scope='head:4000')")
        hints.append("read_artifact(id)  # full content (capped)")
    return hints
