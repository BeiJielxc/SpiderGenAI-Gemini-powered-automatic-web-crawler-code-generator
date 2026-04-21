"""
High-level tools for the Planner agent.

These tools encapsulate multi-step CDP/Playwright workflows behind simple
interfaces so the LLM only needs to make high-level decisions.

Three tools:
  1. extract_list_and_pagination  – universal list + pagination discovery
  2. capture_api_and_infer_params – dynamic API sniffing + parameter attribution
  3. turn_page_and_verify_change  – paginate + verify content actually changed
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup, Tag

try:
    from tools import ToolContext, ToolResult
except ImportError:
    from .tools import ToolContext, ToolResult  # type: ignore

try:
    from node_semantics import (
        aggregate_node_stats,
        collect_node_stats_from_bs4,
        compute_icon_likelihood,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from .node_semantics import (  # type: ignore
        aggregate_node_stats,
        collect_node_stats_from_bs4,
        compute_icon_likelihood,
    )

try:
    from verified_selectors import (
        merge_from_extract_list,
        merge_from_probe_detail,
        merge_from_verify_selector,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from .verified_selectors import (  # type: ignore
        merge_from_extract_list,
        merge_from_probe_detail,
        merge_from_verify_selector,
    )


# =====================================================================
# Shared helpers
# =====================================================================

_DATE_RE = re.compile(
    r"(\d{4})[\/\.\-年](\d{1,2})[\/\.\-月](\d{1,2})[日]?"
)


def _normalize_date(s: str) -> str:
    if not s:
        return ""
    m = _DATE_RE.search(s.strip())
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _text_of(tag: Optional[Tag]) -> str:
    return (tag.get_text(strip=True) if tag else "").strip()


def _extract_go_page_target(onclick: Optional[str]) -> str:
    if not onclick:
        return ""
    m = re.search(r"goPageApp\(['\"]([^'\"]+)['\"]\)", onclick)
    return m.group(1) if m else ""


def _html_sig(html: str) -> str:
    return hashlib.md5(html.encode("utf-8", "ignore")).hexdigest()


# =====================================================================
# 1. extract_list_and_pagination
# =====================================================================

def _score_candidate_block(
    blocks: List[Tag],
    base_url: str,
    parent_tag: Optional[Tag] = None,
) -> Dict[str, Any]:
    """Score a group of sibling blocks on how 'list-like' they are."""
    if not blocks:
        return {"score": 0}

    has_link = 0
    has_date = 0
    has_file = 0  # 新增：文件链接计数
    items: List[Dict[str, Any]] = []

    # 文件扩展名列表（用于识别文件列表）
    file_exts = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".rar", ".7z"}

    for b in blocks[:30]:
        a = b.select_one("a[href]")
        if a:
            has_link += 1
            # 检查是否为文件链接
            href_lower = (a.get("href") or "").lower().strip()
            if any(href_lower.endswith(ext) for ext in file_exts):
                has_file += 1  # 这是一个文件列表项
        
        all_text = b.get_text(" ", strip=True)
        date_str = _normalize_date(all_text)
        if date_str:
            has_date += 1
        title = _text_of(a) if a else ""
        href = (a.get("href", "") if a else "") or ""
        abs_url = urljoin(base_url, href) if href else ""

        date_elem = None
        for candidate in b.select("span, em, td, div, time"):
            d = _normalize_date(_text_of(candidate))
            if d:
                date_elem = candidate
                date_str = d
                break

        items.append({
            "title": title,
            "url": abs_url or href,
            "date": date_str,
            "dateText": _text_of(date_elem) if date_elem else "",
        })

    score = 0
    n = len(blocks)
    if n >= 3:
        score += 20
    score += min(has_link, 30) * 2
    score += min(has_date, 30) * 3
    # 文件列表加分：如果包含文件链接，即使文本较短也认为是有效列表
    score += min(has_file, 30) * 5

    if has_link > 0 and has_date > 0:
        score += 30

    # 降权：侧栏/导航式列表（短链接文本、常见菜单词）避免误选
    titles = [i.get("title") or "" for i in items[:15]]
    short_count = sum(1 for t in titles if len(t.strip()) < 25)
    nav_like_phrases = {
        "find a licensee", "publications & reports", "online services",
        "news", "notices", "press release", "reports", "warnings & alerts",
        "home", "about us", "credit rating", "dealers license", "investment advisors",
        "securities exchange", "enforcement actions", "registration", "careers",
        "contact", "faq", "document search", "annual reports", "current openings",
        "internships", "photo gallery", "events calendar", "multimedia", "events",
    }
    nav_like_count = sum(1 for t in titles if t.strip().lower() in nav_like_phrases)
    if short_count >= 5 or nav_like_count >= 4:
        score -= 50
    # 加分：文章列表常含 "READ MORE" 或较长标题
    if any("read more" in t.lower() for t in titles):
        score += 45
    avg_title_len = (sum(len(t.strip()) for t in titles if t.strip()) / max(len([t for t in titles if t.strip()]), 1))
    if avg_title_len > 35:
        score += 20

    first = blocks[0]

    # --- Build child selector ---
    child_sel = ""
    if first.get("class"):
        child_sel = f".{'.'.join(first['class'])}"
    elif first.name:
        child_sel = first.name

    # --- Build multiple candidate selectors for LLM verification ---
    _dynamic_tags = {"ul", "ol", "tbody"}
    p = parent_tag if parent_tag is not None else first.parent
    parent_sel = ""
    ancestor_sel = ""

    if p and hasattr(p, "name") and p.name not in (None, "body", "[document]"):
        if p.get("id"):
            parent_sel = f"#{p['id']}"
        elif p.get("class"):
            parent_sel = f"{p.name}.{'.'.join(p['class'])}"

        if p.name in _dynamic_tags:
            ancestor = getattr(p, "parent", None)
            for _ in range(5):
                if not ancestor or not hasattr(ancestor, "name"):
                    break
                if ancestor.name in (None, "body", "[document]"):
                    break
                if ancestor.name in ("div", "section", "main", "article") and ancestor.get("class"):
                    ancestor_sel = f".{'.'.join(ancestor['class'])}"
                    break
                ancestor = getattr(ancestor, "parent", None)

    bare_selector = child_sel or first.name or "div"

    # Build candidateSelectors: ordered list of selectors from most specific to broadest
    candidate_selectors: List[Dict[str, str]] = []
    if parent_sel and child_sel:
        candidate_selectors.append({
            "selector": f"{parent_sel} > {child_sel}",
            "label": "parent-qualified (direct child)",
        })
    if ancestor_sel and child_sel:
        candidate_selectors.append({
            "selector": f"{ancestor_sel} {child_sel}",
            "label": "ancestor-qualified (descendant)",
        })
    if bare_selector not in [c["selector"] for c in candidate_selectors]:
        candidate_selectors.append({
            "selector": bare_selector,
            "label": "bare (unqualified)",
        })

    # Default selector: pick parent-qualified if available, else ancestor, else bare
    if parent_sel and child_sel:
        selector = f"{parent_sel} > {child_sel}"
    elif ancestor_sel and child_sel:
        selector = f"{ancestor_sel} {child_sel}"
    else:
        selector = bare_selector

    date_selector = ""
    title_selector = "a"
    if items and items[0].get("date"):
        for candidate_sel in ["span", "em", "td", "time", "div"]:
            el = first.select_one(candidate_sel)
            if el and _normalize_date(_text_of(el)):
                date_selector = candidate_sel
                break

    a_el = first.select_one("a[href]")
    if a_el:
        if a_el.get("class"):
            title_selector = f"a.{'.'.join(a_el['class'])}"
        elif a_el.parent and a_el.parent != first and a_el.parent.get("class"):
            title_selector = f".{'.'.join(a_el.parent['class'])} a"

    # Build sampleHtml: raw HTML of the first 2 list items (truncated).
    sample_html_parts = []
    for b in blocks[:2]:
        raw = str(b)
        sample_html_parts.append(raw[:600] + ("..." if len(raw) > 600 else ""))
    sample_html = "\n".join(sample_html_parts)

    # Build structureHint: a human-readable one-liner describing DOM layout.
    tag_name = first.name or "div"
    cls = ".".join(first.get("class", []))
    tag_desc = f"<{tag_name} class='{cls}'>" if cls else f"<{tag_name}>"

    # Parent container description for structureHint (prefer stable ancestor over ul/ol)
    parent_container_desc = ""
    if ancestor_sel:
        parent_container_desc = f"ancestor: {ancestor_sel}"
    elif p and hasattr(p, "name") and p.name not in (None, "body", "[document]"):
        p_cls = ".".join(p.get("class", []))
        parent_container_desc = f"<{p.name} class='{p_cls}'>" if p_cls else f"<{p.name}>"

    title_tag_desc = ""
    if a_el:
        a_cls = ".".join(a_el.get("class", []))
        a_parent = a_el.parent
        if a_parent and a_parent != first and a_parent.get("class"):
            p_cls = ".".join(a_parent.get("class", []))
            title_tag_desc = f"<{a_parent.name} class='{p_cls}'> > <a>"
        elif a_cls:
            title_tag_desc = f"<a class='{a_cls}'>"
        else:
            title_tag_desc = "<a>"

    date_tag_desc = ""
    if date_selector:
        d_el = first.select_one(date_selector)
        if d_el:
            d_cls = ".".join(d_el.get("class", []))
            date_tag_desc = f"<{d_el.name} class='{d_cls}'>" if d_cls else f"<{d_el.name}>"

    hint_parts = []
    if parent_container_desc:
        hint_parts.append(f"parent container: {parent_container_desc}")
    hint_parts.append(f"Each item is a {tag_desc}")
    if title_tag_desc:
        hint_parts.append(f"title/link in {title_tag_desc}")
    if date_tag_desc:
        hint_parts.append(f"date in {date_tag_desc}")
    structure_hint = "; ".join(hint_parts)

    return {
        "score": score,
        "count": n,
        "selector": selector,
        "bareSelector": bare_selector,
        "candidateSelectors": candidate_selectors,
        "titleSelector": title_selector,
        "dateSelector": date_selector,
        "hasLink": has_link,
        "hasDate": has_date,
        "items": items,
        "sampleHtml": sample_html,
        "structureHint": structure_hint,
    }


def _discover_list_candidates(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    """Find all potential repeating-block groups in the page."""
    skip_tags = {"script", "style", "noscript", "link", "meta", "head"}
    skip_classes = {"header", "footer", "nav", "menu", "sidebar", "copyright", "icon-list"}

    candidates: List[Dict[str, Any]] = []

    for parent in soup.find_all(["div", "ul", "ol", "tbody", "section", "main"]):
        if parent.name in skip_tags:
            continue
        cls_str = " ".join(parent.get("class", []))
        if any(sc in cls_str.lower() for sc in skip_classes):
            continue

        child_tags: Dict[str, List[Tag]] = {}
        for child in parent.children:
            if not isinstance(child, Tag) or child.name in skip_tags:
                continue
            key = child.name + "|" + ".".join(child.get("class", []))
            child_tags.setdefault(key, []).append(child)

        for key, blocks in child_tags.items():
            if len(blocks) < 3:
                continue
            scored = _score_candidate_block(blocks, base_url, parent_tag=parent)
            if scored["score"] > 30:
                candidates.append(scored)

    # 补充发现：通过 "READ MORE" / "read more" 链接反推文章列表（Elementor 等卡片式列表）
    read_more_blocks = _discover_list_via_read_more(soup, base_url)
    for cand in read_more_blocks:
        if cand["score"] > 30 and cand not in candidates:
            candidates.append(cand)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:5]


def _discover_list_via_read_more(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    """Find list of article cards by locating 'READ MORE' / 'read more' links and grouping their containers."""
    out: List[Dict[str, Any]] = []
    read_more_links = soup.find_all(
        "a",
        href=True,
        string=re.compile(r"^\s*read\s+more\s*$", re.I),
    )
    # 也匹配仅包含 "READ MORE" 的链接（可能被包裹在 span 里）
    for a in soup.find_all("a", href=True):
        t = _text_of(a).strip()
        if t and re.match(r"^read\s+more\s*$", t, re.I):
            read_more_links.append(a)
    read_more_links = list({id(x): x for x in read_more_links}.values())
    if len(read_more_links) < 3:
        return out
    # 每个链接向上找“卡片”容器：含标题（h1/h2/h3）且含该链接的最近祖先
    cards: List[Tag] = []
    for a in read_more_links:
        node = a.parent
        while node and node.name != "body":
            if node.name in ("div", "article", "section", "li"):
                h = node.find(["h1", "h2", "h3"])
                if h and a in node.descendants:
                    cards.append(node)
                    break
            node = node.parent if hasattr(node, "parent") else None
    if len(cards) < 3:
        return out
    # 按 tag|class 分组，取数量最多的一组
    key_to_nodes: Dict[str, List[Tag]] = {}
    for c in cards:
        k = c.name + "|" + ".".join(c.get("class", []))
        key_to_nodes.setdefault(k, []).append(c)
    best_key = max(key_to_nodes, key=lambda k: len(key_to_nodes[k]))
    blocks = key_to_nodes[best_key]
    if len(blocks) < 3:
        return out
    common_parent = blocks[0].parent if blocks[0].parent else None
    scored = _score_candidate_block(blocks, base_url, parent_tag=common_parent)
    # 来自 READ MORE 的列表给予额外加分，确保优先于侧栏
    scored["score"] = scored["score"] + 40
    out.append(scored)
    return out


def _discover_pagination(soup: BeautifulSoup, base_url: str) -> Dict[str, Any]:
    """Extract pagination controls (next, prev, page numbers)."""
    def _pick(el: Optional[Tag]) -> Optional[Dict[str, Any]]:
        if not el:
            return None
        target = _extract_go_page_target(el.get("onclick"))
        href = el.get("href")
        url = ""
        if href and href != "#" and "javascript:" not in (href or "").lower():
            url = urljoin(base_url, href)
        elif target:
            url = urljoin(base_url, target)
        return {
            "tag": el.name,
            "text": _text_of(el),
            "href": href,
            "onclick": el.get("onclick"),
            "target": target or None,
            "url": url,
            "class": " ".join(el.get("class", [])),
        }

    next_selectors = [
        ".pageNext", "a.pageNext", "a[rel='next']", "a.next",
        "button.next", "a:contains('下一页')", "a:contains('Next')",
        "a:contains('>')",
    ]
    prev_selectors = [
        ".pagePrev", "a.pagePrev", "a[rel='prev']", "a.prev",
        "button.prev",
    ]

    next_el = None
    for sel in next_selectors:
        try:
            next_el = soup.select_one(sel)
        except Exception:
            pass
        if next_el:
            break

    if not next_el:
        for a in soup.find_all("a"):
            txt = _text_of(a)
            onclick = a.get("onclick", "")
            if txt in ("下一页", ">", "›", "Next", ">>") or "goPage" in onclick:
                cls = " ".join(a.get("class", []))
                if "prev" not in cls.lower():
                    next_el = a
                    break

    prev_el = None
    for sel in prev_selectors:
        try:
            prev_el = soup.select_one(sel)
        except Exception:
            pass
        if prev_el:
            break

    page_nums: List[Dict[str, Any]] = []
    for a in soup.select("a.pageNum, a.page-num, a[class*='page']"):
        text = _text_of(a)
        if text.isdigit():
            info = _pick(a) or {}
            info["text"] = text
            page_nums.append(info)

    if not page_nums:
        for container in soup.find_all(["div", "nav", "ul"], class_=lambda c: c and any(
            kw in " ".join(c).lower() for kw in ("page", "pager", "pagination")
        )):
            for a in container.find_all("a"):
                text = _text_of(a)
                if text.isdigit():
                    info = _pick(a) or {}
                    info["text"] = text
                    page_nums.append(info)
            if page_nums:
                break

    return {
        "next": _pick(next_el),
        "prev": _pick(prev_el),
        "pageNums": page_nums[:20],
        "totalPages": max((int(p["text"]) for p in page_nums if p.get("text", "").isdigit()), default=0),
    }


_SHADOW_DOM_EXTRACT_JS = """
() => {
    const hosts = [];
    for (const el of document.querySelectorAll('*')) {
        if (el.shadowRoot) hosts.push(el);
    }
    if (hosts.length === 0) return null;

    let bestHost = null;
    let bestTextLen = 0;
    for (const h of hosts) {
        const len = (h.shadowRoot.textContent || '').length;
        if (len > bestTextLen) { bestTextLen = len; bestHost = h; }
    }
    if (!bestHost || bestTextLen < 100) return null;

    const sr = bestHost.shadowRoot;
    const hostId = bestHost.id || '';
    const hostTag = bestHost.tagName.toLowerCase();

    const SKIP_TAGS = new Set(['STYLE', 'SCRIPT', 'LINK', 'META', 'NOSCRIPT', 'TEMPLATE']);

    function findCards(root, depth) {
        if (depth > 8) return [];
        const children = Array.from(root.children).filter(c => !SKIP_TAGS.has(c.tagName));
        if (children.length === 0) return [];
        const textChildren = children.filter(c => (c.innerText || '').trim().length > 30);
        if (textChildren.length >= 3) return textChildren;
        let best = null, bestLen = 0;
        for (const c of children) {
            const l = (c.innerText || '').length;
            if (l > bestLen) { bestLen = l; best = c; }
        }
        if (best) return findCards(best, depth + 1);
        return [];
    }

    const cards = findCards(sr, 0);
    if (cards.length === 0) return { hostId, hostTag, shadowDOM: true, items: [], totalText: bestTextLen };

    const items = [];
    for (let i = 0; i < cards.length; i++) {
        const card = cards[i];
        const text = (card.innerText || '').trim();
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);

        // First line is usually source/author, second is time, rest is content
        let source = '', timeStr = '', title = '', url = '';
        const links = Array.from(card.querySelectorAll('a'));
        if (links.length > 0) {
            source = links[0].innerText.trim();
            url = links[0].href || '';
        }
        // Find time-like text
        for (const l of lines) {
            if (/^vor\\s|ago|\\d{1,2}[\\.\\/-]\\d{1,2}[\\.\\/-]\\d{2,4}|\\d{4}-\\d{2}-\\d{2}|hours?|minutes?|days?|Stunden|Minuten|Tagen/i.test(l)) {
                timeStr = l;
                break;
            }
        }
        // Title: first substantial line that's not source or time
        for (const l of lines) {
            if (l !== source && l !== timeStr && l.length > 10) {
                title = l.substring(0, 200);
                break;
            }
        }

        // Detect platform from links
        let platform = '';
        for (const a of links) {
            const h = a.href || '';
            if (h.includes('facebook.com')) { platform = 'facebook'; break; }
            if (h.includes('instagram.com')) { platform = 'instagram'; break; }
            if (h.includes('linkedin.com')) { platform = 'linkedin'; break; }
            if (h.includes('youtube.com')) { platform = 'youtube'; break; }
            if (h.includes('bsky.app')) { platform = 'bluesky'; break; }
            if (h.includes('twitter.com') || h.includes('x.com')) { platform = 'twitter'; break; }
        }

        items.push({
            index: i,
            title: title,
            source: source,
            date: timeStr,
            url: url,
            platform: platform,
            textPreview: text.substring(0, 300)
        });
    }

    // Build a sample of the card HTML (first card only, truncated)
    let sampleHtml = '';
    if (cards.length > 0) {
        sampleHtml = cards[0].innerHTML.substring(0, 1500);
    }

    return {
        shadowDOM: true,
        hostId: hostId,
        hostTag: hostTag,
        hostSelector: hostId ? '#' + hostId : hostTag,
        itemCount: cards.length,
        items: items,
        sampleHtml: sampleHtml,
        totalText: bestTextLen
    };
}
"""


def _build_shadow_dom_code_template(host_selector: str) -> str:
    """Build a generic Playwright code template for extracting data from Shadow DOM."""
    return f'''
# --- Shadow DOM extraction (auto-generated template) ---
# Host element: "{host_selector}"
# This code uses page.evaluate() to pierce the Shadow DOM and extract items
# via innerText parsing. CSS class selectors are NOT used because Shadow DOM
# frameworks typically produce hashed class names.

SHADOW_EXTRACT_JS = """
() => {{
    const SKIP_TAGS = new Set(['STYLE','SCRIPT','LINK','META','NOSCRIPT','TEMPLATE']);
    function findCards(root, depth) {{
        if (depth > 8) return [];
        const children = Array.from(root.children).filter(c => !SKIP_TAGS.has(c.tagName));
        if (!children.length) return [];
        const textChildren = children.filter(c => (c.innerText || '').trim().length > 30);
        if (textChildren.length >= 3) return textChildren;
        let best = null, bestLen = 0;
        for (const c of children) {{
            const l = (c.innerText || '').length;
            if (l > bestLen) {{ bestLen = l; best = c; }}
        }}
        return best ? findCards(best, depth + 1) : [];
    }}

    const host = document.querySelector('{host_selector}');
    if (!host || !host.shadowRoot) return [];
    const cards = findCards(host.shadowRoot, 0);

    return cards.map((card, i) => {{
        const text = (card.innerText || '').trim();
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
        const links = Array.from(card.querySelectorAll('a'));
        const firstLink = links.length > 0 ? links[0] : null;
        const source = firstLink ? firstLink.innerText.trim() : '';
        const sourceUrl = firstLink ? firstLink.href : '';
        let date = '';
        for (const l of lines) {{
            if (/\\d{{1,2}}[\\.\\/\\-]\\d{{1,2}}[\\.\\/\\-]\\d{{2,4}}|\\d{{4}}-\\d{{2}}-\\d{{2}}|ago|hours?|minutes?|days?|vor\\s/i.test(l)) {{
                date = l; break;
            }}
        }}
        let title = '';
        for (const l of lines) {{
            if (l !== source && l !== date && l.length > 10) {{
                title = l; break;
            }}
        }}
        return {{
            index: i,
            title: title.substring(0, 200),
            date: date,
            source: source,
            sourceUrl: sourceUrl,
            content: text
        }};
    }});
}}
"""

async def extract_shadow_dom_items(page, max_items=10):
    """Extract items from Shadow DOM using page.evaluate()."""
    raw = await page.evaluate(SHADOW_EXTRACT_JS)
    return raw[:max_items] if raw else []
# --- End Shadow DOM template ---
'''


async def tool_extract_list_and_pagination(ctx: ToolContext) -> ToolResult:
    """
    Universal list + pagination discovery.

    Automatically:
    1. Grabs the current page HTML (from browser, always fresh).
    2. Scans for repeating blocks that look like a data list.
    3. Extracts items (title, url, date) from the best candidate.
    4. Discovers pagination controls (next, prev, page numbers).
    5. Returns structured data + recommended selectors.
    6. Falls back to Shadow DOM piercing if no list found in regular DOM.
    """
    try:
        html = await ctx.browser.get_full_html()
        ctx.page_html = html
        if not html or len(html) < 200:
            return ToolResult(
                success=False,
                error="Page HTML is empty or too short",
                summary="Page HTML is empty or too short – page may not have loaded",
                error_code="empty_page",
                recoverable=True,
                suggested_next_tools=["open_page", "detect_data_status"],
            )

        page_info = await ctx.browser.get_page_info()
        ctx.page_info = page_info
        base_url = (page_info.get("url") or ctx.url or "").strip()

        soup = BeautifulSoup(html, "html.parser")

        candidates = _discover_list_candidates(soup, base_url)
        pagination = _discover_pagination(soup, base_url)

        if not candidates:
            # Fallback: try piercing Shadow DOM
            shadow_result = None
            try:
                if ctx.browser.page:
                    shadow_result = await ctx.browser.page.evaluate(_SHADOW_DOM_EXTRACT_JS)
                    if shadow_result:
                        ctx.log(f"[TOOL] Shadow DOM probe: shadowDOM={shadow_result.get('shadowDOM')}, "
                                f"items={len(shadow_result.get('items', []))}, "
                                f"host={shadow_result.get('hostSelector', '?')}")
                    else:
                        ctx.log("[TOOL] Shadow DOM probe: no shadow hosts found")
            except Exception as e:
                ctx.log(f"[TOOL] Shadow DOM probe error: {e}")

            if shadow_result and shadow_result.get("shadowDOM") and shadow_result.get("items"):
                shadow_items = shadow_result["items"]
                host_selector = shadow_result.get("hostSelector", "")

                code_template = _build_shadow_dom_code_template(host_selector)

                payload = {
                    "baseUrl": base_url,
                    "shadowDOM": True,
                    "shadowHostSelector": host_selector,
                    "bestCandidate": {
                        "selector": f"shadow:{host_selector}",
                        "titleSelector": "",
                        "dateSelector": "",
                        "score": 0,
                        "itemCount": len(shadow_items),
                        "hasLink": any(it.get("url") for it in shadow_items),
                        "hasDate": any(it.get("date") for it in shadow_items),
                    },
                    "structureHint": (
                        f"Content is inside Shadow DOM (host: {host_selector}). "
                        f"Found {len(shadow_items)} cards."
                    ),
                    "codeTemplate": code_template,
                    "sampleHtml": shadow_result.get("sampleHtml", ""),
                    "items": [
                        {
                            "title": it.get("title", ""),
                            "url": it.get("url", ""),
                            "date": it.get("date", ""),
                            "source": it.get("source", ""),
                            "platform": it.get("platform", ""),
                        }
                        for it in shadow_items
                    ],
                    "pagination": {},
                    "dateHints": {},
                    "otherCandidates": [],
                }

                ctx.enhanced_analysis["list_extract"] = {
                    "selector": f"shadow:{host_selector}",
                    "shadowDOM": True,
                    "shadowHostSelector": host_selector,
                    "structureHint": payload["structureHint"],
                    "codeTemplate": code_template,
                    "itemCount": len(shadow_items),
                }
                ctx.enhanced_analysis["_last_list_items"] = payload["items"]

                summary = (
                    f"Found {len(shadow_items)} items inside Shadow DOM "
                    f"(host: {host_selector})"
                )
                ctx.log(f"[TOOL] extract_list_and_pagination: {summary}")
                return ToolResult(
                    success=True,
                    data=payload,
                    summary=summary,
                    suggested_next_tools=["generate_crawler_code"],
                )

            return ToolResult(
                success=False,
                error="No repeating list blocks found on page",
                summary=(
                    "No list-like repeating blocks found. "
                    "This page may load data dynamically via API – try capture_api_and_infer_params."
                ),
                error_code="no_list_found",
                recoverable=True,
                suggested_next_tools=["capture_api_and_infer_params", "detect_data_status"],
            )

        best = candidates[0]

        # --- Browser-based validation of ALL candidate selectors ---
        candidate_selectors = best.get("candidateSelectors", [])
        verified_candidates: List[Dict[str, Any]] = []

        if ctx.browser.page and candidate_selectors:
            js_validate_multi = """
            (selectors) => {
                return selectors.map(sel => {
                    const els = document.querySelectorAll(sel);
                    let visible = 0;
                    els.forEach(el => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (style.visibility !== 'hidden' && style.display !== 'none' && rect.height > 0) {
                            visible++;
                        }
                    });
                    return { selector: sel, total: els.length, visible: visible };
                });
            }
            """
            try:
                sel_strings = [c["selector"] for c in candidate_selectors]
                raw_results = await ctx.browser.page.evaluate(
                    js_validate_multi, sel_strings,
                )
                for cand, counts in zip(candidate_selectors, raw_results or []):
                    entry = {
                        "selector": cand["selector"],
                        "label": cand["label"],
                        "totalMatches": counts.get("total", 0),
                        "visibleMatches": counts.get("visible", 0),
                    }
                    verified_candidates.append(entry)
                    ctx.log(
                        f"[TOOL] Candidate selector '{cand['selector']}' ({cand['label']}): "
                        f"{entry['totalMatches']} total, {entry['visibleMatches']} visible"
                    )

                # Auto-select best: pick the first candidate with visible > 0
                auto_selected = False
                for vc in verified_candidates:
                    if vc["visibleMatches"] > 0:
                        best["selector"] = vc["selector"]
                        auto_selected = True
                        break
                if not auto_selected:
                    ctx.log("[TOOL] WARNING: no candidate selector matched visible elements")

            except Exception as e:
                ctx.log(f"[TOOL] Candidate selector validation skipped: {e}")
                verified_candidates = [
                    {"selector": c["selector"], "label": c["label"],
                     "totalMatches": -1, "visibleMatches": -1}
                    for c in candidate_selectors
                ]
        elif candidate_selectors:
            verified_candidates = [
                {"selector": c["selector"], "label": c["label"],
                 "totalMatches": -1, "visibleMatches": -1}
                for c in candidate_selectors
            ]

        items = best.get("items", [])

        parsed_dates = [it["date"] for it in items if it.get("date")]
        min_date = min(parsed_dates) if parsed_dates else ""
        max_date = max(parsed_dates) if parsed_dates else ""
        sd = (ctx.start_date or "").strip()
        ed = (ctx.end_date or "").strip()
        has_in_range = bool(parsed_dates and sd and ed and any(sd <= d <= ed for d in parsed_dates))
        has_older = bool(parsed_dates and sd and any(d < sd for d in parsed_dates))

        date_hints = {
            "minDate": min_date,
            "maxDate": max_date,
            "startDate": sd,
            "endDate": ed,
            "hasInRange": has_in_range,
            "hasOlderThanStart": has_older,
            "suggestStopPaging": bool(has_in_range and has_older),
        }

        best_candidate_info = {
            "selector": best["selector"],
            "bareSelector": best.get("bareSelector", best["selector"]),
            "candidateSelectors": verified_candidates,
            "titleSelector": best["titleSelector"],
            "dateSelector": best["dateSelector"],
            "score": best["score"],
            "itemCount": best["count"],
            "hasLink": best["hasLink"],
            "hasDate": best["hasDate"],
        }
        if best.get("selectorNote"):
            best_candidate_info["selectorNote"] = best["selectorNote"]

        payload = {
            "baseUrl": base_url,
            "bestCandidate": best_candidate_info,
            "structureHint": best.get("structureHint", ""),
            "sampleHtml": best.get("sampleHtml", ""),
            "items": items,
            "pagination": pagination,
            "dateHints": date_hints,
            "otherCandidates": [
                {"selector": c["selector"], "score": c["score"], "count": c["count"]}
                for c in candidates[1:]
            ],
        }

        ctx.enhanced_analysis["list_extract"] = {
            "selector": best["selector"],
            "titleSelector": best["titleSelector"],
            "dateSelector": best["dateSelector"],
            "structureHint": best.get("structureHint", ""),
            "itemCount": len(items),
            "pagination_next": pagination.get("next"),
        }
        ctx.enhanced_analysis["_last_list_items"] = items

        summary_parts = [
            f"Found {len(items)} list items (selector: {best['selector']}, score: {best['score']})",
        ]
        if min_date and max_date:
            summary_parts.append(f"dates: {min_date}..{max_date}")
        if pagination.get("next"):
            next_url = pagination["next"].get("url", "")
            summary_parts.append(f"next page: {next_url[:80]}" if next_url else "next: onclick-based")
        if pagination.get("totalPages"):
            summary_parts.append(f"~{pagination['totalPages']} pages")

        next_tools = ["generate_crawler_code", "validate_code"]
        if not date_hints.get("suggestStopPaging") and pagination.get("next"):
            next_tools = ["turn_page_and_verify_change", "generate_crawler_code"]

        ctx.log(f"[TOOL] extract_list_and_pagination: {'; '.join(summary_parts)}")
        try:
            ctx.verified_selectors = merge_from_extract_list(
                ctx.verified_selectors, payload
            )
        except Exception as merge_exc:  # never fail the tool because of a bookkeeping bug
            ctx.log(f"[TOOL] verified_selectors merge (extract_list) failed: {merge_exc}")
        return ToolResult(
            success=True,
            data=payload,
            summary="; ".join(summary_parts),
            suggested_next_tools=next_tools,
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"extract_list_and_pagination failed: {exc}",
            error_code="extract_list_pagination_failed",
            recoverable=True,
            suggested_next_tools=["analyze_page", "capture_api_and_infer_params"],
        )


# =====================================================================
# 2. capture_api_and_infer_params
# =====================================================================


def _extract_data_apis(requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract data-bearing APIs from a list of captured request dicts."""
    data_apis: List[Dict[str, Any]] = []
    for r in requests:
        body = r.get("response_body") or r.get("responseBody") or r.get("response_preview") or ""
        if not body or not isinstance(body, str):
            continue
        try:
            parsed = json.loads(body.strip()) if body.strip() else {}
        except Exception:
            continue

        arrays = _find_arrays_in_json(parsed)
        if not arrays:
            continue

        best_array = max(arrays, key=lambda a: len(a[1]))
        arr_path, arr_data = best_array

        if len(arr_data) < 2:
            continue

        item_fields = set()
        if isinstance(arr_data[0], dict):
            item_fields = set(arr_data[0].keys())

        has_title = bool(item_fields & {"title", "name", "TITLE", "announcementTitle", "secName"})
        has_date = bool(item_fields & {"date", "time", "publishDate", "publishTime", "announcementTime", "NOTICE_DATE"})

        url_str = r.get("url", "")
        method = r.get("method", "GET")
        parsed_url = urlparse(url_str)
        query_params = parse_qs(parsed_url.query)
        post_body = r.get("postData") or r.get("post_data") or ""
        post_params = {}
        if post_body:
            try:
                post_params = json.loads(post_body)
            except Exception:
                post_params = dict(parse_qs(post_body))

        data_apis.append({
            "url": url_str,
            "method": method,
            "queryParams": {k: v[0] if len(v) == 1 else v for k, v in query_params.items()},
            "postParams": post_params,
            "arrayPath": arr_path,
            "arrayLength": len(arr_data),
            "itemFields": sorted(item_fields)[:20],
            "hasTitle": has_title,
            "hasDate": has_date,
            "sampleItem": _safe_preview(arr_data[0]) if arr_data else {},
        })
    return data_apis


async def tool_capture_api_and_infer_params(ctx: ToolContext) -> ToolResult:
    """
    Dynamic API sniffing + parameter attribution.

    Automatically:
    1. Analyzes requests already captured during page load.
    2. If none found, attempts pagination interactions (click next, scroll).
    3. Captures new XHR/Fetch requests triggered by interactions.
    4. Identifies the data-bearing API (response contains array of items).
    5. Diffs parameters between requests to infer page/category/date params.
    """
    try:
        if not ctx.browser.page:
            return ToolResult(
                success=False, error="No browser page", summary="No browser page available",
                error_code="no_browser", recoverable=True,
                suggested_next_tools=["open_page"],
            )

        page = ctx.browser.page
        import asyncio

        original_url = page.url

        before_requests = list(ctx.browser.get_captured_requests().get("api_requests", []))
        before_urls = {r.get("url", "") for r in before_requests}

        interaction_succeeded = False
        new_requests: List[Dict[str, Any]] = []

        # Phase 1: analyze requests already captured during page load
        data_apis = _extract_data_apis(before_requests)

        if not data_apis:
            # Phase 2: trigger interactions to capture new requests
            html_before = await page.content()
            first_text_before = ""
            try:
                first_text_before = await page.evaluate("""
                    () => {
                        const items = document.querySelectorAll('tr, li, div[class]');
                        for (const el of items) {
                            const text = el.innerText?.trim();
                            if (text && text.length > 10 && text.length < 300) return text;
                        }
                        return '';
                    }
                """)
            except Exception:
                pass

            ctx.browser._clear_captured_requests()

            interaction_succeeded = False

            next_selectors = [
                ".pageNext", "a.pageNext", "a.next", ".next",
                "a:has-text('下一页')", "a:has-text('>')", "a:has-text('Next')",
                "button:has-text('下一页')",
                ".pagination a:nth-child(2)",
                "a.page-num:nth-child(2)", "a.pageNum:nth-child(2)",
            ]
            for sel in next_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        interaction_succeeded = True
                        break
                except Exception:
                    continue

            if not interaction_succeeded:
                try:
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    interaction_succeeded = True
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await asyncio.sleep(3)

            after_requests = list(ctx.browser.get_captured_requests().get("api_requests", []))

            new_requests = []
            for r in after_requests:
                url = r.get("url", "")
                if url and url not in before_urls:
                    ct = (r.get("response_headers") or r.get("responseHeaders") or {})
                    ct = (ct.get("content-type", "") or "").lower()
                    body = r.get("response_body") or r.get("responseBody") or r.get("response_preview") or ""
                    if "json" in ct or body.strip().startswith(("{", "[")):
                        new_requests.append(r)

            data_apis = _extract_data_apis(new_requests)

        # Restore browser to original URL so subsequent tools see the same page.
        await _restore_page(page, original_url)

        if not data_apis:
            return ToolResult(
                success=False,
                error="No data-bearing API found after interactions",
                summary=(
                    "No data API discovered. "
                    "The page may use server-rendered HTML – try extract_list_and_pagination instead."
                ),
                error_code="no_data_api",
                recoverable=True,
                suggested_next_tools=["extract_list_and_pagination", "detect_data_status"],
            )

        inferred_params = _infer_pagination_params(data_apis, before_requests)

        best_api = data_apis[0]
        payload = {
            "dataApis": data_apis,
            "bestApi": best_api,
            "inferredParams": inferred_params,
            "interactionSucceeded": interaction_succeeded,
            "newRequestCount": len(new_requests),
        }

        summary_parts = [
            f"Found {len(data_apis)} data API(s)",
            f"best: {best_api['method']} {best_api['url'][:80]}",
            f"{best_api['arrayLength']} items in '{best_api['arrayPath']}'",
            "CRITICAL: These are API parameters, NOT main page URL parameters. Do NOT append them to the main page URL.",
        ]
        if inferred_params:
            summary_parts.append(f"inferred params: {inferred_params}")

        ctx.enhanced_analysis["captured_data_api"] = payload
        ctx.log(f"[TOOL] capture_api_and_infer_params: {'; '.join(summary_parts)}")

        return ToolResult(
            success=True,
            data=payload,
            summary="; ".join(summary_parts),
            suggested_next_tools=["generate_crawler_code", "validate_code"],
        )
    except Exception as exc:
        try:
            await _restore_page(page, original_url)
        except Exception:
            pass
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"capture_api_and_infer_params failed: {exc}",
            error_code="capture_api_infer_failed",
            recoverable=True,
            suggested_next_tools=["extract_list_and_pagination", "analyze_page"],
        )


async def _restore_page(page, original_url: str) -> None:
    """Navigate back to original_url if the page has moved away."""
    import asyncio
    try:
        if page.url != original_url:
            await page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
    except Exception:
        pass


def _find_arrays_in_json(obj: Any, path: str = "", max_depth: int = 5) -> List[Tuple[str, list]]:
    """Recursively find arrays in a JSON object."""
    results = []
    if max_depth <= 0:
        return results
    if isinstance(obj, list) and len(obj) >= 2:
        results.append((path or "root", obj))
    elif isinstance(obj, dict):
        for key, val in obj.items():
            child_path = f"{path}.{key}" if path else key
            results.extend(_find_arrays_in_json(val, child_path, max_depth - 1))
    return results


def _safe_preview(obj: Any, limit: int = 500) -> Any:
    """Truncate a JSON object for preview."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
        if len(s) > limit:
            return json.loads(s[:limit] + "...")
    except Exception:
        pass
    return obj


def _infer_pagination_params(
    data_apis: List[Dict[str, Any]],
    before_requests: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Compare before/after API params to guess which ones control pagination."""
    inferred: Dict[str, str] = {}

    page_keywords = {"page", "pagenum", "pageNo", "pageno", "p", "currentpage", "pageindex", "offset", "start"}
    size_keywords = {"pagesize", "pageSize", "size", "limit", "count", "rows", "num"}
    date_keywords = {"date", "startdate", "enddate", "begin", "end", "from", "to", "sedate"}
    category_keywords = {"category", "type", "column", "plate", "stock", "code", "tabname", "classid"}

    for api in data_apis:
        all_params = {}
        all_params.update(api.get("queryParams", {}))
        all_params.update(api.get("postParams", {}))
        for key, val in all_params.items():
            key_lower = key.lower()
            if key_lower in page_keywords:
                inferred[key] = "page"
            elif key_lower in size_keywords:
                inferred[key] = "pageSize"
            elif key_lower in date_keywords:
                inferred[key] = "date"
            elif key_lower in category_keywords:
                inferred[key] = "category"

    return inferred


# =====================================================================
# 3. turn_page_and_verify_change
# =====================================================================

async def tool_turn_page_and_verify_change(ctx: ToolContext, next_url: str = "") -> ToolResult:
    """
    Navigate to the next page and verify content actually changed.

    Strategies (tried in order):
    1. If next_url is provided, navigate directly.
    2. Try clicking common "next page" selectors.
    3. If all fail, parse pagination from ctx to find a fallback URL.

    After navigation, verifies content changed by comparing first list item text.
    """
    try:
        if not ctx.browser.page:
            return ToolResult(
                success=False, error="No browser page", summary="No browser page available",
                error_code="no_browser", recoverable=True,
                suggested_next_tools=["open_page"],
            )

        page = ctx.browser.page
        import asyncio

        snapshot_before = await _get_content_fingerprint(page)

        navigated = False
        method_used = ""

        if next_url and next_url.strip():
            try:
                ctx.page_html = None
                ctx.page_structure = None
                try:
                    ctx.enhanced_analysis.pop("_last_html_sig", None)
                except Exception:
                    pass

                await page.goto(next_url.strip(), wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    await asyncio.sleep(2)
                navigated = True
                method_used = f"direct URL: {next_url[:80]}"
            except Exception as e:
                ctx.log(f"[TOOL] turn_page direct navigation failed: {e}")

        if not navigated:
            next_selectors = [
                ".pageNext", "a.pageNext", "a.next", ".next",
                "a:has-text('下一页')", "a:has-text('>')", "a:has-text('Next')",
                "button:has-text('下一页')",
            ]
            for sel in next_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        ctx.page_html = None
                        ctx.page_structure = None

                        await el.click()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                        navigated = True
                        method_used = f"click selector: {sel}"
                        break
                except Exception:
                    continue

        if not navigated:
            fallback_url = _find_fallback_next_url(ctx)
            if fallback_url:
                try:
                    ctx.page_html = None
                    ctx.page_structure = None

                    await page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    navigated = True
                    method_used = f"fallback URL: {fallback_url[:80]}"
                except Exception as e:
                    ctx.log(f"[TOOL] turn_page fallback navigation failed: {e}")

        if not navigated:
            return ToolResult(
                success=False,
                error="Could not navigate to next page",
                summary="All pagination methods failed. No next page button or URL found.",
                error_code="turn_page_failed",
                recoverable=True,
                suggested_next_tools=["extract_list_and_pagination", "generate_crawler_code"],
            )

        snapshot_after = await _get_content_fingerprint(page)
        content_changed = snapshot_before != snapshot_after

        try:
            ctx.page_info = await ctx.browser.get_page_info()
        except Exception:
            pass

        new_url = page.url

        if not content_changed:
            return ToolResult(
                success=False,
                error="Page content did not change after navigation",
                summary=f"Navigated via {method_used} but content unchanged – may have reached last page or pagination is broken.",
                error_code="content_unchanged",
                data={"method": method_used, "newUrl": new_url, "contentChanged": False},
                recoverable=True,
                suggested_next_tools=["extract_list_and_pagination", "generate_crawler_code"],
            )

        return ToolResult(
            success=True,
            data={"method": method_used, "newUrl": new_url, "contentChanged": True},
            summary=f"Successfully turned page via {method_used}; content changed; new URL: {new_url[:100]}",
            suggested_next_tools=["extract_list_and_pagination", "generate_crawler_code"],
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"turn_page_and_verify_change failed: {exc}",
            error_code="turn_page_exception",
            recoverable=True,
            suggested_next_tools=["extract_list_and_pagination", "generate_crawler_code"],
        )


async def _get_content_fingerprint(page) -> str:
    """Get a hash of the main content area for change detection."""
    try:
        text = await page.evaluate("""
            () => {
                const selectors = [
                    'main', '.content', '.main', '#content', '.list',
                    'table tbody', '.rightListContent', 'article'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 50) {
                        return el.innerText.trim().substring(0, 500);
                    }
                }
                return document.body?.innerText?.substring(0, 800) || '';
            }
        """)
        return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
    except Exception:
        return ""


def _find_fallback_next_url(ctx: ToolContext) -> str:
    """Try to find a next-page URL from previously extracted pagination data."""
    try:
        le = ctx.enhanced_analysis.get("list_extract", {})
        next_info = le.get("pagination_next")
        if isinstance(next_info, dict):
            url = next_info.get("url", "")
            if url:
                return url
    except Exception:
        pass
    return ""


# =====================================================================
# 4. probe_detail_page
# =====================================================================

# 即找点击进入新闻链接后新闻正文所在的容器位置
# 用于发现候选；最终由“所有发现的容器+信息”供大模型选择，此处仅作候选来源
_DETAIL_CONTENT_SELECTORS = [
    ".TRS_Editor",
    "#TRS_AUTOA498095",
    ".article-content",
    ".article_content",
    ".article.content-container",
    ".content-container",
    ".article__body",
    ".article-body",
    ".news_content",
    ".detail_content",
    ".detail-content",
    ".post-content",
    ".entry-content",
    ".xl_content",
    "article",
    "[role='article']",
    "#content",
    ".content",
    "main",
    ".main-content",
    ".main_content",
    ".body-content",
    ".text",
    ".news_text",
    ".detail_text",
    ".cont_detail",
]

_DETAIL_TITLE_SELECTORS = [
    "h1",
    ".article-title",
    ".news-title",
    ".detail-title",
    ".title",
    "#title",
    "h2.title",
]


async def tool_probe_detail_page(ctx: ToolContext, url: str = "") -> ToolResult:
    """
    Open a detail/article page in a NEW tab, scan for the content container
    and title element, then close the tab (zero side-effects on main page).

    If `url` is empty, automatically picks the first item URL from the
    previous `extract_list_and_pagination` result.

    Returns:
      - contentSelector: CSS selector that matched the article body
      - contentTagName: actual tag name (e.g. "td", "div")
      - sampleContentHtml: truncated raw HTML of the content container
      - titleSelector / titleTagName: for the detail page title
      - structureHint: one-liner DOM description for generate_crawler_code
    """
    try:
        if not ctx.browser.page:
            return ToolResult(
                success=False, error="No browser page",
                summary="No browser page available",
                error_code="no_browser", recoverable=True,
                suggested_next_tools=["open_page"],
            )

        import asyncio

        target_url = (url or "").strip()

        if not target_url:
            items_data = ctx.enhanced_analysis.get("_last_list_items", [])
            for item in items_data:
                u = (item.get("url") or "").strip()
                if u and u.startswith("http"):
                    target_url = u
                    break

        if not target_url:
            return ToolResult(
                success=False,
                error="No detail URL provided and no items from previous extract_list_and_pagination",
                summary="Provide a detail page URL or run extract_list_and_pagination first",
                error_code="no_detail_url",
                recoverable=True,
                suggested_next_tools=["extract_list_and_pagination"],
            )

        browser_ctx = ctx.browser.page.context
        detail_page = await browser_ctx.new_page()

        try:

            is_pdf_response = False
            try:
                response = await detail_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                # Handle "Download is starting" error which is thrown by Playwright when navigation is aborted due to download
                if "Download is starting" in str(e) or "net::ERR_ABORTED" in str(e):
                    response = None
                    is_pdf_response = True
                else:
                    raise e
            
            # --- Check if response is a file (e.g. PDF) ---
            content_type = ""
            if not is_pdf_response:
                try:
                    if response:
                        headers = response.headers
                        content_type = (headers.get("content-type") or "").lower()
                except Exception:
                    pass

                is_pdf_response = "application/pdf" in content_type or target_url.lower().split('?')[0].endswith(".pdf")
            
            if is_pdf_response:
                summary = "Detail page is a PDF file (Content-Type: application/pdf)"
                payload = {
                    "url": target_url,
                    "contentSelector": "",
                    "contentTagName": "",
                    "contentTextLength": 0,
                    "sampleContentHtml": "",
                    "contentCandidates": [{
                        "selector": "body",
                        "textLength": 0,
                        "linkCount": 0,
                        "linkDensity": 0,
                        "textPreview": f"[PDF Document] {target_url}",
                        "isFileContainer": True,
                        "fileExt": "pdf"
                    }],
                    "titleSelector": "",
                    "titleTagName": "",
                    "titleText": "PDF Document",
                    "structureHint": f"Direct PDF File: {target_url} (Do not parse HTML, download directly)",
                    "isDirectFile": True,
                    "fileType": "pdf"
                }
                ctx.log(f"[TOOL] probe_detail_page: {summary}")

                # 写入 enhanced_analysis（追加到列表，支持多次 probe）
                if "detail_probes" not in ctx.enhanced_analysis:
                    ctx.enhanced_analysis["detail_probes"] = []
                ctx.enhanced_analysis["detail_probes"].append({
                    "url": target_url,
                    "isDirectFile": True,
                    "fileType": "pdf",
                    "contentSelector": "",
                    "contentCandidates": payload["contentCandidates"],
                    "structureHint": payload["structureHint"],
                })
                # 兼容旧字段
                ctx.enhanced_analysis["detail_probe"] = ctx.enhanced_analysis["detail_probes"][-1]

                await detail_page.close()
                return ToolResult(success=True, data=payload, summary=summary, suggested_next_tools=["generate_crawler_code"])

            try:
                await detail_page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await asyncio.sleep(2)

            detail_html = await detail_page.content()
            soup = BeautifulSoup(detail_html, "html.parser")

            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            # --- 收集所有可能的正文容器候选（供大模型选择）---
            def _selector_for_el(el: Tag) -> str:
                if el.get("id"):
                    return f"#{el['id']}"
                if el.get("class"):
                    return f"{el.name}.{'.'.join(el['class'])}"
                return el.name

            def _text_len_links(el: Tag) -> Tuple[int, int]:
                text = el.get_text(strip=True)
                return len(text), len(el.find_all("a"))

            seen_selectors: set = set()
            content_candidates: List[Dict[str, Any]] = []

            # 常见文件扩展名
            FILE_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".rar", ".7z"}

            def _icon_payload_for(el: Tag) -> Optional[Dict[str, Any]]:
                """Compute icon-likelihood for a single candidate.

                We feed the *element itself* as a one-element list to
                ``collect_node_stats_from_bs4`` so the aggregator's
                signals (icon class hit, child tag ratio, short text,
                aria) collapse to "is this single container icon-y?".
                Geometry signals are unavailable in BS4 mode and the
                helper handles that automatically.
                """
                try:
                    stats = aggregate_node_stats(collect_node_stats_from_bs4([el]))
                    ilk = compute_icon_likelihood(stats)
                    return {
                        "score": ilk.score,
                        "verdict": ilk.verdict,
                        "evidence": list(ilk.evidence),
                    }
                except Exception:
                    return None

            for sel in _DETAIL_CONTENT_SELECTORS:
                try:
                    elements = soup.select(sel)
                except Exception:
                    continue
                for el in elements:
                    text_len, link_count = _text_len_links(el)
                    if text_len < 50:
                        continue
                    selector = _selector_for_el(el)
                    if selector in seen_selectors:
                        continue
                    seen_selectors.add(selector)
                    link_density = link_count / (text_len + 1)
                    text_preview = (el.get_text(strip=True) or "")[:80].replace("\n", " ")
                    cand = {
                        "selector": selector,
                        "textLength": text_len,
                        "linkCount": link_count,
                        "linkDensity": round(link_density, 4),
                        "textPreview": text_preview,
                    }
                    icon_info = _icon_payload_for(el)
                    if icon_info is not None:
                        cand["iconLikelihood"] = icon_info
                    content_candidates.append(cand)

            # Fallback: 正文最长、链接密度低的块
            for div in soup.find_all(["div", "td", "section", "article", "main"]):
                cls_str = " ".join(div.get("class", []))
                id_str = div.get("id", "")
                if any(kw in (cls_str + id_str).lower() for kw in ("nav", "footer", "header", "menu", "sidebar", "list", "page")):
                    continue
                text_len, link_count = _text_len_links(div)
                if text_len < 100:
                    continue
                link_density = link_count / (text_len + 1)
                if link_density > 0.1:
                    continue
                selector = _selector_for_el(div)
                if selector in seen_selectors:
                    continue
                seen_selectors.add(selector)
                cand = {
                    "selector": selector,
                    "textLength": text_len,
                    "linkCount": link_count,
                    "linkDensity": round(link_density, 4),
                    "textPreview": (div.get_text(strip=True) or "")[:80].replace("\n", " "),
                }
                icon_info = _icon_payload_for(div)
                if icon_info is not None:
                    cand["iconLikelihood"] = icon_info
                content_candidates.append(cand)

            # --- 兜底：搜索显著的文件下载链接 ---
            # 如果正文区域很短或者未找到，尝试寻找是否直接提供了 PDF/表格下载
            # 这种情况下，下载链接所在的容器可能就是“正文”
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip().lower()
                # 检查是否是文件链接
                if any(href.endswith(ext) for ext in FILE_EXTS):
                    # 找到一个文件链接，查看其容器
                    container = a.parent
                    if not container or container.name in ['body', 'html']:
                        container = a
                    
                    # 排除导航/页脚等噪音区域
                    cont_cls = " ".join(container.get("class", []) or []).lower()
                    cont_id = str(container.get("id", "")).lower()
                    if any(kw in (cont_cls + cont_id) for kw in ("nav", "footer", "header", "menu", "sidebar", "breadcrumb")):
                        continue

                    # 获取容器文本长度（文件下载容器通常文本较短，不能用 <50 过滤）
                    text = container.get_text(strip=True)
                    text_len = len(text)
                    
                    # 只有当容器不过大（避免选中整个列表页）时才采纳
                    if text_len < 2000:
                        selector = _selector_for_el(container)
                        if selector in seen_selectors:
                            continue
                        seen_selectors.add(selector)
                        
                        # 标记为文件容器
                        content_candidates.append({
                            "selector": selector,
                            "textLength": text_len, # 即使很短也没关系
                            "linkCount": 1,
                            "linkDensity": 0.0, # 忽略密度检查
                            "textPreview": f"[FILE] {text[:80]}",
                            "isFileContainer": True, # 特殊标记，排序时优先
                            "fileExt": href.split('.')[-1]
                        })

            # 按“是否为文件容器优先、正文长度优先、链接密度低优先”排序
            # isFileContainer=True 的给予极大加权，确保如果没找到长文，文件链接能排前面
            # 但如果已经找到了很长的正文（textLength > 500），文件链接排在次席作为补充
            #
            # NEW: 同时根据 iconLikelihood.verdict 做软惩罚。
            #   - icon_container：扣 5000，几乎一定沉底
            #   - ambiguous：扣 1500，仅在没有更好选择时才会被推荐
            # 我们 *不* 直接 drop 这些候选，因为：
            #   (1) BS4 缺少 geometry 信号，scorer 偶尔会误判；
            #   (2) 留在列表里 LLM 也能看到 evidence，自己判断；
            #   (3) 真的没找到正文时，ambiguous 仍可作为最后兜底。
            def _candidate_score(c):
                is_file = c.get("isFileContainer", False)
                length = c["textLength"]
                base_score = 2000 if is_file else 0
                ilk = c.get("iconLikelihood") or {}
                verdict = ilk.get("verdict") or "content"
                if verdict == "icon_container":
                    base_score -= 5000
                elif verdict == "ambiguous":
                    base_score -= 1500
                return (length + base_score, -c["linkDensity"])

            content_candidates.sort(key=_candidate_score, reverse=True)
            content_candidates = content_candidates[:10]

            recommended = content_candidates[0] if content_candidates else None
            content_selector = (recommended["selector"] if recommended else "")
            content_tag_name = content_selector.split(".")[0] if content_selector else ""
            content_text_len = (recommended["textLength"] if recommended else 0)
            sample_content_html = ""
            if recommended and content_selector:
                try:
                    el = soup.select_one(content_selector)
                    if el:
                        raw = str(el)
                        sample_content_html = raw[:1200] + ("..." if len(raw) > 1200 else "")
                except Exception:
                    pass

            # --- Scan for title ---
            title_selector = ""
            title_tag_name = ""
            title_text = ""
            for sel in _DETAIL_TITLE_SELECTORS:
                try:
                    el = soup.select_one(sel)
                except Exception:
                    continue
                if el:
                    t = el.get_text(strip=True)
                    if t and len(t) > 3:
                        title_selector = sel
                        title_tag_name = el.name
                        if el.get("class"):
                            title_tag_name = f"{el.name}.{'.'.join(el['class'])}"
                        title_text = t[:200]
                        break

            # Build structureHint（只说明候选数量，由大模型自己选）
            hint_parts = []
            if content_candidates:
                hint_parts.append(f"{len(content_candidates)} body container candidates for LLM to choose from")
            else:
                hint_parts.append("no content container found – use longest-text fallback")
            if title_selector:
                hint_parts.append(f"title in <{title_tag_name}> (selector: '{title_selector}')")
            structure_hint = "; ".join(hint_parts)

            payload = {
                "url": target_url,
                "contentSelector": content_selector,
                "contentTagName": content_tag_name,
                "contentTextLength": content_text_len,
                "sampleContentHtml": sample_content_html,
                "contentCandidates": content_candidates,
                "titleSelector": title_selector,
                "titleTagName": title_tag_name,
                "titleText": title_text,
                "structureHint": structure_hint,
            }

            probe_entry = {
                "url": target_url,
                "isDirectFile": False,
                "contentSelector": content_selector,
                "contentTagName": content_tag_name,
                "contentCandidates": content_candidates,
                "titleSelector": title_selector,
                "structureHint": structure_hint,
            }
            if "detail_probes" not in ctx.enhanced_analysis:
                ctx.enhanced_analysis["detail_probes"] = []
            ctx.enhanced_analysis["detail_probes"].append(probe_entry)
            ctx.enhanced_analysis["detail_probe"] = probe_entry

            summary = f"Detail page probed: {structure_hint}"
            ctx.log(f"[TOOL] probe_detail_page: {summary}")
            try:
                ctx.verified_selectors = merge_from_probe_detail(
                    ctx.verified_selectors, payload
                )
            except Exception as merge_exc:
                ctx.log(f"[TOOL] verified_selectors merge (probe_detail) failed: {merge_exc}")

            return ToolResult(
                success=bool(content_selector),
                data=payload,
                summary=summary,
                error_code=None if content_selector else "no_content_container",
                suggested_next_tools=["generate_crawler_code", "validate_code"],
            )

        finally:
            try:
                await detail_page.close()
            except Exception:
                pass

    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"probe_detail_page failed: {exc}",
            error_code="probe_detail_failed",
            recoverable=True,
            suggested_next_tools=["generate_crawler_code"],
        )


# =====================================================================
# tool_verify_selector – LLM-driven selector verification
# =====================================================================

async def tool_verify_selector(ctx: ToolContext, selector: str, description: str = "") -> ToolResult:
    """
    Test a CSS selector against the CURRENT live page using Playwright.

    Returns match count, visible count, and a preview of matched elements
    so the LLM can judge whether this selector targets the correct elements.

    This is a read-only tool – it does NOT mutate page state.
    """
    selector = (selector or "").strip()
    if not selector:
        return ToolResult(
            success=False,
            error="selector parameter is required",
            summary="No selector provided",
            error_code="missing_param",
            recoverable=True,
        )

    if not ctx.browser.page:
        return ToolResult(
            success=False,
            error="No browser page available",
            summary="No browser page – call open_page first",
            error_code="no_browser",
            recoverable=True,
            suggested_next_tools=["open_page"],
        )

    try:
        # The JS now also collects lightweight per-node "semantic stats"
        # (capped at first 50 nodes to keep payload small) so the
        # Python side can compute an icon-likelihood verdict and surface
        # it both in the tool summary log and in the JSON returned to
        # the LLM. Without this signal an icon-list selector that
        # happens to wrap 25 short text labels looks identical to a
        # real news list.
        js_code = """
        (sel) => {
            const els = document.querySelectorAll(sel);
            let visible = 0;
            const previews = [];
            const nodeStats = [];
            const STATS_CAP = 50;
            els.forEach((el, idx) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const isVisible = style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.height > 0;
                if (isVisible) visible++;
                if (previews.length < 3) {
                    const classes = el.className
                        ? (typeof el.className === 'string' ? el.className : '')
                        : '';
                    const text = (el.textContent || '').trim().substring(0, 120);
                    const tag = el.tagName.toLowerCase();
                    const link = el.querySelector('a');
                    const href = link ? link.href : '';
                    previews.push({
                        tag: tag,
                        classes: classes,
                        text: text,
                        href: href,
                        visible: isVisible,
                        index: idx
                    });
                }
                if (nodeStats.length < STATS_CAP) {
                    const childTags = {};
                    for (let i = 0; i < el.children.length; i++) {
                        const cn = el.children[i].tagName.toLowerCase();
                        childTags[cn] = (childTags[cn] || 0) + 1;
                    }
                    let svgNoText = false;
                    for (let i = 0; i < el.children.length; i++) {
                        const ch = el.children[i];
                        if (ch.tagName.toLowerCase() === 'svg' &&
                            ch.querySelector('text') === null) {
                            svgNoText = true;
                            break;
                        }
                    }
                    const cls = el.className
                        ? (typeof el.className === 'string' ? el.className : '')
                        : '';
                    nodeStats.push({
                        class_str: cls,
                        child_tags: childTags,
                        text_len: (el.textContent || '').trim().length,
                        width: rect.width,
                        height: rect.height,
                        has_aria_hidden: el.getAttribute('aria-hidden') === 'true',
                        has_svg_no_text: svgNoText
                    });
                }
            });
            return {
                total: els.length,
                visible: visible,
                previews: previews,
                nodeStats: nodeStats
            };
        }
        """
        result = await ctx.browser.page.evaluate(js_code, selector)

        total = result.get("total", 0)
        visible = result.get("visible", 0)
        previews = result.get("previews", [])
        node_stats_raw = result.get("nodeStats", []) or []

        # Multi-signal "is this an icon container?" scoring. Helper is
        # pure Python so it never raises on partial input; we still
        # guard with try/except to keep verify_selector itself bullet-
        # proof — if scoring blows up the tool must still return its
        # original count/preview payload.
        icon_payload: Optional[Dict[str, Any]] = None
        try:
            agg = aggregate_node_stats(node_stats_raw)
            ilk = compute_icon_likelihood(agg)
            icon_payload = {
                "score": ilk.score,
                "verdict": ilk.verdict,
                "evidence": list(ilk.evidence),
                "sampledNodes": agg.node_count,
                "medianTextLen": int(agg.median_text_len),
                "iconTagRatio": round(agg.icon_tag_ratio, 3),
                "ariaHiddenRatio": round(agg.aria_hidden_ratio, 3),
            }
        except Exception as score_exc:  # pragma: no cover - defensive
            ctx.log(f"[TOOL] verify_selector icon-likelihood scoring failed: {score_exc}")

        desc_label = f" ({description})" if description else ""
        summary = (
            f"selector '{selector}'{desc_label}: "
            f"{total} total, {visible} visible"
        )
        # Surface icon verdicts in the log line so that even without
        # reading JSON the user/LLM sees the warning. We only emit when
        # the verdict isn't plain "content" to keep logs quiet for the
        # 99% of selectors that are obviously fine.
        if icon_payload and icon_payload["verdict"] != "content":
            summary += (
                f" | icon_likelihood={icon_payload['score']:.2f}"
                f" verdict={icon_payload['verdict']}"
            )
        ctx.log(f"[TOOL] verify_selector: {summary}")

        verify_payload = {
            "selector": selector,
            "description": description,
            "totalMatches": total,
            "visibleMatches": visible,
            "previews": previews,
        }
        if icon_payload is not None:
            verify_payload["iconLikelihood"] = icon_payload
        try:
            ctx.verified_selectors = merge_from_verify_selector(
                ctx.verified_selectors, verify_payload
            )
        except Exception as merge_exc:
            ctx.log(f"[TOOL] verified_selectors merge (verify_selector) failed: {merge_exc}")

        return ToolResult(
            success=True,
            data=verify_payload,
            summary=summary,
            suggested_next_tools=["generate_crawler_code", "verify_selector"],
        )

    except Exception as exc:
        return ToolResult(
            success=False,
            error=f"verify_selector failed: {exc}",
            summary=f"Failed to evaluate selector '{selector}': {exc}",
            error_code="evaluate_failed",
            recoverable=True,
            suggested_next_tools=["verify_selector"],
        )


# =====================================================================
# __all__
# =====================================================================

__all__ = [
    "tool_extract_list_and_pagination",
    "tool_capture_api_and_infer_params",
    "tool_turn_page_and_verify_change",
    "tool_probe_detail_page",
    "tool_verify_selector",
]
