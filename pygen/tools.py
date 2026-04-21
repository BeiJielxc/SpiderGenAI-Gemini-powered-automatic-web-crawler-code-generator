"""
Tool implementations and shared tool models.

This module intentionally contains no registration logic. Tool
registration/dispatch is handled by the LangChain ``@tool`` wrappers in
``agents/tools_lc.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:  # rule-based artifact summarizers (zero-LLM, optional)
    from summarizers import (
        summarize_analyze_page,
        summarize_html,
        summarize_json_payload,
        enrich_summary_via_llm,
        is_weak_summary,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    try:
        from .summarizers import (  # type: ignore
            summarize_analyze_page,
            summarize_html,
            summarize_json_payload,
            enrich_summary_via_llm,
            is_weak_summary,
        )
    except ImportError:
        summarize_html = None  # type: ignore
        summarize_json_payload = None  # type: ignore
        summarize_analyze_page = None  # type: ignore
        enrich_summary_via_llm = None  # type: ignore
        is_weak_summary = None  # type: ignore

# PageCache (Module A) — process-wide singleton populated by api.py /
# runner.py. Imported lazily-style: every cache access goes through
# ``get_default_page_cache()`` so unit tests / standalone scripts that
# never configure it continue to behave exactly as before.
try:
    from page_cache import get_default_page_cache
except ImportError:  # pragma: no cover - package-style import fallback
    try:
        from .page_cache import get_default_page_cache  # type: ignore
    except ImportError:
        get_default_page_cache = lambda: None  # type: ignore

DEFAULT_ALLOWED_PACKAGES = [
    "playwright",
    "playwright-stealth",
    "requests",
    "httpx",
    "beautifulsoup4",
    "lxml",
    "pandas",
    "numpy",
    "pyyaml",
    "fastapi",
    "uvicorn",
]


@dataclass
class ToolResult:
    """Normalized tool execution result."""

    success: bool
    data: Any = None
    error: Optional[str] = None
    summary: str = ""
    error_code: Optional[str] = None
    retryable: bool = False
    recoverable: bool = True
    suggested_next_tools: List[str] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: Optional[int] = None
    confidence: Optional[float] = None


class ToolContext:
    """Shared mutable state for tool execution and planner decisions."""

    def __init__(
        self,
        browser,
        config,
        llm_agent,
        url: str,
        run_mode: str,
        start_date: str,
        end_date: str,
        extra_requirements: str = "",
        task_id: str = "",
        log_callback=None,
        attachments=None,
        artifact_store=None,
        executor_session=None,
        critic=None,
    ):
        self.browser = browser
        self.config = config
        self.llm = llm_agent
        self.url = url
        self.run_mode = run_mode
        self.start_date = start_date
        self.end_date = end_date
        self.extra_requirements = extra_requirements
        self.task_id = task_id
        self.log = log_callback or (lambda msg: None)
        self.attachments = attachments

        # Optional runtime extensions
        self.artifact_store = artifact_store
        self.executor_session = executor_session
        self.critic = critic

        # Collected context
        self.page_info: Optional[Dict[str, Any]] = None
        self.page_html: Optional[str] = None
        self.page_structure: Optional[Dict[str, Any]] = None
        self.network_requests: Optional[Dict[str, Any]] = None
        self.menu_tree: Optional[Dict[str, Any]] = None
        self.date_api_result = None
        self.verified_mapping: Optional[Dict[str, Any]] = None
        # Verified-selector ledger: structured record of selectors proven to
        # work on the live page (populated by extract_list / probe_detail /
        # verify_selector hooks). Travels into codegen as a hard "MUST USE"
        # block so the LLM can't fall back to generic guesses. ``None`` until
        # the first probe writes; helpers in pygen.verified_selectors handle
        # the empty/None case gracefully.
        self.verified_selectors: Optional[Dict[str, Any]] = None
        self.screenshots: List[str] = []
        self.enhanced_analysis: Dict[str, Any] = {}

        # Generation outputs
        self.generated_code: Optional[str] = None
        self.code_generation_strategy: Optional[str] = None


def _safe_data_preview(data: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        text = str(data)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _wrap_summarizer_with_llm_fallback(
    ctx: ToolContext,
    rule_summarizer,
    *,
    kind: str,
):
    """If ``artifacts.small_model.enabled``, return a summarizer that runs
    the rule one first and only invokes the small LLM when the result is
    *weak* (see :func:`is_weak_summary`).

    The returned function has the same shape as the rule summarizer:
    ``payload -> dict``. On any LLM error / disabled config, the rule
    output is returned untouched. The model is built lazily (per call)
    so we don't pay construction cost on every artifact, and so missing
    config keys only fail the fallback, not the artifact write.
    """
    if rule_summarizer is None or enrich_summary_via_llm is None or is_weak_summary is None:
        return rule_summarizer

    cfg = getattr(ctx, "config", None)
    if cfg is None or not bool(getattr(cfg, "artifacts_small_model_enabled", False)):
        return rule_summarizer

    raw_excerpt_chars = int(getattr(cfg, "artifacts_small_model_raw_excerpt_chars", 6000))
    max_tokens = int(getattr(cfg, "artifacts_small_model_max_tokens", 800))
    timeout_sec = int(getattr(cfg, "artifacts_small_model_timeout_sec", 15))

    def _factory():
        try:
            from agents.llm import build_small_model  # local import keeps tools.py light
        except ImportError:  # pragma: no cover
            from .agents.llm import build_small_model  # type: ignore
        return build_small_model(cfg, max_tokens=max_tokens, timeout=timeout_sec)

    def _wrapped(payload):
        try:
            rule_summary = rule_summarizer(payload) or {}
            if not isinstance(rule_summary, dict):
                rule_summary = {"_invalid_summary_type": type(rule_summary).__name__}
        except Exception as exc:
            rule_summary = {"_summary_error": f"{type(exc).__name__}: {exc}"}

        if not is_weak_summary(rule_summary, kind=kind):
            rule_summary.setdefault("_summary_provenance", "rule")
            return rule_summary

        try:
            enriched = enrich_summary_via_llm(
                payload,
                rule_summary,
                kind=kind,
                model_factory=_factory,
                raw_excerpt_chars=raw_excerpt_chars,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                log=getattr(ctx, "log", None),
            )
            return enriched if isinstance(enriched, dict) else rule_summary
        except Exception as exc:
            try:
                ctx.log(f"[SUMMARIZER-LLM] fallback unexpected error: {exc}")
            except Exception:
                pass
            rule_summary.setdefault("_summary_provenance", "rule")
            rule_summary.setdefault("_fallback_skipped", f"unexpected: {type(exc).__name__}")
            return rule_summary

    return _wrapped


def _persist_artifact_if_large(
    ctx: ToolContext,
    payload: Any,
    prefix: str,
    threshold: int = 3500,
    summarizer=None,
    summarizer_kind: str = "html",
) -> Dict[str, Any]:
    """Store large payloads out-of-band when artifact_store is available.

    When ``summarizer`` is provided AND the store supports
    ``put_with_summary``, the returned ``artifact_ref`` will include a
    structured ``summary`` dict (via ``ArtifactRef.to_prompt_dict``) so
    the planner LLM gets candidate signals instead of raw HTML/JSON.

    ``summarizer_kind`` is forwarded to the optional small-model fallback
    (``"html"`` / ``"json"`` / ``"analyze"``) and is otherwise ignored.

    Per-task subdirectories: ``ctx.task_id`` is forwarded to the store so
    artifacts land under ``<root>/<task_id>/`` for easy cleanup and
    isolation across concurrent tasks.
    """
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        serialized = str(payload)

    cfg = getattr(ctx, "config", None)
    cfg_threshold = getattr(cfg, "artifacts_summary_threshold", None)
    if isinstance(cfg_threshold, int) and cfg_threshold > 0:
        threshold = cfg_threshold
    summary_enabled = True
    if cfg is not None:
        summary_enabled = bool(getattr(cfg, "artifacts_enable_summary", True))

    if len(serialized) <= threshold or not ctx.artifact_store:
        return {}

    task_id = getattr(ctx, "task_id", None) or None

    effective_summarizer = summarizer
    if summary_enabled and summarizer is not None:
        effective_summarizer = _wrap_summarizer_with_llm_fallback(
            ctx, summarizer, kind=summarizer_kind
        )

    try:
        store = ctx.artifact_store
        put_with_summary = getattr(store, "put_with_summary", None)
        if summary_enabled and effective_summarizer is not None and callable(put_with_summary):
            ref = put_with_summary(
                payload,
                prefix=prefix,
                summarizer=effective_summarizer,
                task_id=task_id,
            )
        else:
            try:
                ref = store.put_json(payload, prefix=prefix, task_id=task_id)
            except TypeError:
                ref = store.put_json(payload, prefix=prefix)
        return {"artifact_ref": ref.to_prompt_dict()}
    except Exception as exc:
        ctx.log(f"[TOOL] Failed to persist artifact: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Atomic tools
# ---------------------------------------------------------------------------


async def tool_open_page(ctx: ToolContext, url: str = "", wait_until: str = "domcontentloaded") -> ToolResult:
    target = url or ctx.url
    try:
        success, error_msg = await ctx.browser.open(target, wait_until=wait_until)
        if success:
            # Invalidate cached page-derived context after navigation.
            ctx.page_html = None
            ctx.page_structure = None
            ctx.network_requests = None
            try:
                ctx.enhanced_analysis.pop("_last_html_sig", None)
                ctx.enhanced_analysis.pop("_last_analyze_html_sig", None)
            except Exception:
                pass

            # Refresh page_info (best-effort) so downstream tools have correct base URL.
            try:
                ctx.page_info = await ctx.browser.get_page_info()
            except Exception:
                ctx.page_info = None

            ctx.log(f"[TOOL] Page opened: {target}")
            return ToolResult(
                success=True,
                summary=f"Page opened: {target}",
                data={"url": (ctx.page_info or {}).get("url") or target, "title": (ctx.page_info or {}).get("title")},
                suggested_next_tools=["get_page_html", "extract_list_items_from_ctx_html", "analyze_page"],
            )
        return ToolResult(
            success=False,
            error=error_msg,
            summary=f"Failed to open page: {error_msg}",
            error_code="open_page_failed",
            retryable=True,
            suggested_next_tools=["take_screenshot", "get_network_requests"],
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Error opening page: {exc}",
            error_code="open_page_exception",
            retryable=True,
            suggested_next_tools=["take_screenshot"],
        )


async def tool_scroll_page(ctx: ToolContext, times: int = 3) -> ToolResult:
    try:
        await ctx.browser.scroll_page(times=times)
        ctx.log(f"[TOOL] Page scrolled: {times} times")
        return ToolResult(success=True, summary=f"Scrolled page {times} times")
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Failed to scroll page: {exc}",
            error_code="scroll_failed",
            retryable=True,
            suggested_next_tools=["analyze_page", "take_screenshot"],
        )


async def tool_get_page_info(ctx: ToolContext) -> ToolResult:
    try:
        info = await ctx.browser.get_page_info()
        ctx.page_info = info
        summary = f"Title: {info.get('title', 'N/A')}, URL: {info.get('url', 'N/A')}"
        ctx.log(f"[TOOL] Page info: {summary}")
        return ToolResult(success=True, data=info, summary=summary)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"Failed to get page info: {exc}", error_code="page_info_failed")


async def tool_get_page_html(ctx: ToolContext) -> ToolResult:
    try:
        html = await ctx.browser.get_full_html()
        ctx.page_html = html
        sig = hashlib.md5(html.encode("utf-8", "ignore")).hexdigest()
        last_sig = (ctx.enhanced_analysis or {}).get("_last_html_sig")
        try:
            ctx.enhanced_analysis["_last_html_sig"] = sig
        except Exception:
            pass

        # Module A: write-through to the URL-keyed PageCache so a future
        # rerun's pre-validator (and ``tool_get_page_html`` on the same
        # URL) can short-circuit the browser. Use the resolved URL from
        # page_info — it reflects the post-redirect address; falling
        # back to ctx.url only when page_info hasn't been populated yet.
        try:
            cache = get_default_page_cache()
            if cache is not None:
                cache_url = (ctx.page_info or {}).get("url") or ctx.url or ""
                if cache_url:
                    cache.put_html(cache_url, html)
        except Exception as cache_exc:
            # Cache failures are NEVER fatal — the live HTML capture
            # already succeeded and is what the planner actually needs.
            ctx.log(f"[PAGE_CACHE] put_html failed (non-fatal): {cache_exc}")

        data = {
            "length": len(html),
            "preview": html[:2000] + "..." if len(html) > 2000 else html,
        }
        _html_summarizer = (
            (lambda payload: summarize_html(payload, url=(ctx.page_info or {}).get("url") or ctx.url or ""))
            if summarize_html is not None
            else None
        )
        artifacts = _persist_artifact_if_large(
            ctx,
            {"html": html},
            prefix="page_html",
            summarizer=_html_summarizer,
            summarizer_kind="html",
        )
        if last_sig == sig:
            summary = f"[REPLAN_REQUIRED] HTML unchanged ({len(html):,} chars); avoid repeating get_page_html/analyze_page"
            ctx.log(f"[TOOL] {summary}")
            return ToolResult(
                success=True,
                data=data,
                summary=summary,
                artifacts=artifacts,
                suggested_next_tools=["extract_list_items_from_ctx_html", "generate_crawler_code"],
                confidence=0.7,
            )

        ctx.log(f"[TOOL] HTML captured: {len(html):,} chars")
        return ToolResult(success=True, data=data, summary=f"HTML captured: {len(html):,} chars", artifacts=artifacts)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"Failed to get HTML: {exc}", error_code="html_failed")


async def tool_invalidate_page_cache(
    ctx: ToolContext,
    *,
    url: str = "",
    domain: str = "",
) -> ToolResult:
    """Drop cached HTML rows so the next ``open_page`` / ``get_page_html``
    fetches the live page instead of replaying a stale snapshot.

    Escape hatch for the planner: if the LLM suspects the cache row is
    out of date (e.g. site reskin not yet picked up by fingerprint
    drift), it can call this to force a fresh fetch.

    Pass exactly one of ``url`` (drop a single row) or ``domain``
    (drop every row under a host). Passing both is fine — both apply.
    Passing neither is a no-op (we refuse to nuke the entire cache
    from a single tool call).
    """
    cache = get_default_page_cache()
    if cache is None:
        return ToolResult(
            success=True,
            summary="page cache not configured — nothing to invalidate",
            data={"removed": 0},
        )
    target_url = (url or "").strip() or None
    target_domain = (domain or "").strip().lower() or None
    if not target_url and not target_domain:
        return ToolResult(
            success=False,
            error="must specify url or domain",
            summary="invalidate_page_cache requires url or domain",
            error_code="missing_param",
            recoverable=True,
        )
    try:
        removed = cache.invalidate(url=target_url, domain=target_domain)
        ctx.log(
            f"[TOOL] invalidate_page_cache: dropped {removed} row(s) "
            f"(url={target_url or '-'}, domain={target_domain or '-'})"
        )
        return ToolResult(
            success=True,
            summary=f"invalidated {removed} cache row(s)",
            data={"removed": removed, "url": target_url, "domain": target_domain},
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"invalidate_page_cache failed: {exc}",
            error_code="invalidate_failed",
        )


async def tool_extract_list_items_from_ctx_html(
    ctx: ToolContext,
    *,
    max_items: int = 80,
    refresh_html: bool = False,
    base_url: str = "",
    list_selector: str = ".rightListContent",
    title_selector: str = ".rightListImport a",
    date_selector: str = ".rightListTime",
) -> ToolResult:
    """
    Parse list-like pages directly from ctx.page_html and extract list items + pagination hints.

    This is designed to avoid LLMs copying full HTML into `run_python_snippet` and to reduce
    redundant `get_page_html` / `analyze_page` calls when the DOM/HTML is already available.
    """
    try:
        if refresh_html or not (ctx.page_html and ctx.page_html.strip()):
            # Refresh from live browser if possible (keeps tool self-sufficient).
            html = await ctx.browser.get_full_html()
            ctx.page_html = html
        else:
            html = ctx.page_html

        if not html or not html.strip():
            return ToolResult(
                success=False,
                error="No page HTML available in context",
                summary="No page HTML available in context",
                error_code="no_ctx_html",
                recoverable=True,
                suggested_next_tools=["get_page_html", "analyze_page"],
            )

        effective_base = (base_url or (ctx.page_info or {}).get("url") or ctx.url or "").strip()
        soup = BeautifulSoup(html, "html.parser")

        items: List[Dict[str, Any]] = []
        parsed_dates: List[str] = []

        def _normalize_date(s: str) -> str:
            s = (s or "").strip()
            if not s:
                return ""
            s = s.replace("/", "-").replace(".", "-")
            m = re.search(r"(\d{4}-\d{1,2}-\d{1,2})", s)
            if not m:
                return ""
            y, mo, d = m.group(1).split("-")
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

        for node in soup.select(list_selector)[: max(0, int(max_items))]:
            a = node.select_one(title_selector) or node.select_one("a")
            t = node.select_one(date_selector)
            title = (a.get_text(strip=True) if a else "").strip()
            href = (a.get("href") if a else "") or ""
            date_text = (t.get_text(strip=True) if t else "").strip()
            iso_date = _normalize_date(date_text)
            abs_url = urljoin(effective_base, href) if href else ""
            if iso_date:
                parsed_dates.append(iso_date)
            items.append(
                {
                    "title": title,
                    "url": abs_url or href,
                    "dateText": date_text,
                    "date": iso_date,
                    "rawHref": href,
                }
            )

        def _pick_pagination(el) -> Optional[Dict[str, Any]]:
            if not el:
                return None
            return {
                "tag": getattr(el, "name", None),
                "class": " ".join(el.get("class", [])) if hasattr(el, "get") else None,
                "text": (el.get_text(strip=True) if hasattr(el, "get_text") else "").strip(),
                "href": el.get("href") if hasattr(el, "get") else None,
                "onclick": el.get("onclick") if hasattr(el, "get") else None,
            }

        def _extract_go_page_target(onclick: Optional[str]) -> str:
            if not onclick:
                return ""
            m = re.search(r"goPageApp\(['\"]([^'\"]+)['\"]\)", onclick)
            return m.group(1) if m else ""

        next_el = soup.select_one(".pageNext, a[rel='next'], a.next, button.next")
        prev_el = soup.select_one(".pagePrev, a[rel='prev'], a.prev, button.prev")
        next_meta = _pick_pagination(next_el)
        prev_meta = _pick_pagination(prev_el)

        if next_meta and not next_meta.get("href"):
            target = _extract_go_page_target(next_meta.get("onclick"))
            if target:
                next_meta["target"] = target
                next_meta["url"] = urljoin(effective_base, target)
        if prev_meta and not prev_meta.get("href"):
            target = _extract_go_page_target(prev_meta.get("onclick"))
            if target:
                prev_meta["target"] = target
                prev_meta["url"] = urljoin(effective_base, target)

        page_nums: List[Dict[str, Any]] = []
        for a in soup.select("a.pageNum"):
            text = a.get_text(strip=True)
            href = a.get("href")
            onclick = a.get("onclick")
            target = _extract_go_page_target(onclick)
            page_nums.append(
                {
                    "text": text,
                    "href": href,
                    "onclick": onclick,
                    "target": target or None,
                    "url": urljoin(effective_base, href) if href else (urljoin(effective_base, target) if target else None),
                    "class": " ".join(a.get("class", [])),
                }
            )

        go_input = soup.select_one("input#goPage, input[name='goPage']")
        pagination = {
            "next": next_meta,
            "prev": prev_meta,
            "pageNums": page_nums[:200],
            "goPageInput": {"id": go_input.get("id"), "name": go_input.get("name")} if go_input else None,
        }

        payload = {
            "baseUrl": effective_base,
            "selectors": {
                "list": list_selector,
                "title": title_selector,
                "date": date_selector,
            },
            "items": items,
            "pagination": pagination,
            "counts": {"items": len(items), "pageNums": len(page_nums)},
        }

        # Date-range hints for planner (helps early-stop decisions).
        if parsed_dates:
            min_date = min(parsed_dates)
            max_date = max(parsed_dates)
        else:
            min_date = ""
            max_date = ""

        sd = (ctx.start_date or "").strip()
        ed = (ctx.end_date or "").strip()
        has_in_range = False
        has_older_than_start = False
        has_newer_than_end = False
        if parsed_dates and sd and ed:
            has_in_range = any(sd <= d <= ed for d in parsed_dates)
            has_older_than_start = any(d < sd for d in parsed_dates)
            has_newer_than_end = any(d > ed for d in parsed_dates)

        payload["dateHints"] = {
            "minDate": min_date,
            "maxDate": max_date,
            "startDate": sd,
            "endDate": ed,
            "hasInRange": has_in_range,
            "hasOlderThanStart": has_older_than_start,
            "hasNewerThanEnd": has_newer_than_end,
            "suggestStopPaging": bool(has_in_range and has_older_than_start),
        }

        # Keep a compact copy for later strategy prompts.
        try:
            ctx.enhanced_analysis["list_extract"] = {
                "counts": payload["counts"],
                "selectors": payload["selectors"],
                "next": (payload["pagination"] or {}).get("next"),
            }
        except Exception:
            pass

        return ToolResult(
            success=True,
            data=payload,
            summary=(
                f"Extracted {len(items)} items and {len(page_nums)} pageNum links from ctx.page_html"
                + (f" (date range on page: {min_date}..{max_date})" if min_date and max_date else "")
            ),
            suggested_next_tools=(
                ["generate_crawler_code", "validate_code"]
                if payload.get("dateHints", {}).get("suggestStopPaging")
                else ["generate_crawler_code", "open_page", "click_next_page"]
            ),
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Failed to extract list items from ctx.page_html: {exc}",
            error_code="extract_list_failed",
            recoverable=True,
            suggested_next_tools=["get_page_html", "analyze_page"],
        )


async def tool_analyze_page(ctx: ToolContext) -> ToolResult:
    try:
        page_info = await ctx.browser.get_page_info()
        page_structure = await ctx.browser.analyze_page_structure()
        network_requests = ctx.browser.get_captured_requests()
        page_html = await ctx.browser.get_full_html()

        ctx.page_info = page_info
        ctx.page_structure = page_structure
        ctx.network_requests = network_requests
        ctx.page_html = page_html

        try:
            sig = hashlib.md5(page_html.encode("utf-8", "ignore")).hexdigest()
            ctx.enhanced_analysis["_last_analyze_html_sig"] = sig
        except Exception:
            pass

        tables = page_structure.get("tables", [])
        lists = page_structure.get("lists", [])
        links = page_structure.get("links", {})
        api_reqs = network_requests.get("api_requests", [])

        summary = (
            f"Title: {page_info.get('title', 'N/A')}; "
            f"HTML: {len(page_html):,} chars; "
            f"Tables: {len(tables)}; Lists: {len(lists)}; "
            f"PDF links: {len(links.get('pdfLinks', []))}; "
            f"APIs: {len(api_reqs)}"
        )

        data = {
            "page_info": page_info,
            "tables_count": len(tables),
            "lists_count": len(lists),
            "pdf_links_count": len(links.get("pdfLinks", [])),
            "api_requests_count": len(api_reqs),
            "api_requests_preview": [
                {"url": r.get("url", "")[:120], "method": r.get("method", "")}
                for r in api_reqs[:5]
            ],
        }
        artifacts = _persist_artifact_if_large(
            ctx,
            {
                "page_info": page_info,
                "page_structure": page_structure,
                "network_requests": network_requests,
            },
            prefix="analyze_page",
            summarizer=summarize_analyze_page if summarize_analyze_page is not None else None,
            summarizer_kind="analyze",
        )

        ctx.log(f"[TOOL] Page analysis complete: {summary}")
        return ToolResult(success=True, data=data, summary=summary, artifacts=artifacts)
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Failed to analyze page: {exc}",
            error_code="analyze_failed",
            suggested_next_tools=["take_screenshot", "get_network_requests"],
        )


async def tool_take_screenshot(ctx: ToolContext) -> ToolResult:
    try:
        b64 = await ctx.browser.take_screenshot_base64()
        if b64:
            ctx.screenshots.append(b64)
            data = {"base64": b64}
            artifacts = _persist_artifact_if_large(ctx, data, prefix="screenshot")
            ctx.log("[TOOL] Screenshot captured")
            return ToolResult(success=True, data=data, summary="Screenshot captured", artifacts=artifacts)
        return ToolResult(success=False, error="Screenshot returned empty", summary="Screenshot returned empty", error_code="screenshot_empty")
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"Screenshot failed: {exc}", error_code="screenshot_failed")


async def tool_get_network_requests(ctx: ToolContext) -> ToolResult:
    try:
        reqs = ctx.browser.get_captured_requests()
        ctx.network_requests = reqs
        api_reqs = reqs.get("api_requests", [])
        data = {
            "api_requests": [
                {
                    "url": r.get("url", "")[:200],
                    "method": r.get("method", ""),
                    "resource_type": r.get("resourceType", ""),
                    "status": r.get("response_status") or r.get("status", ""),
                    "response_preview": (r.get("response_body") or r.get("responseBody") or r.get("response_preview") or "")[:500],
                }
                for r in api_reqs[:10]
            ],
            "total_requests": len(reqs.get("all_requests", [])),
        }
        artifacts = _persist_artifact_if_large(
            ctx,
            reqs,
            prefix="network_requests",
            summarizer=summarize_json_payload if summarize_json_payload is not None else None,
            summarizer_kind="json",
        )
        summary = f"Captured {len(api_reqs)} API requests"
        ctx.log(f"[TOOL] {summary}")
        return ToolResult(success=True, data=data, summary=summary, artifacts=artifacts)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"Network capture failed: {exc}", error_code="network_capture_failed")


async def tool_wait_for_network_idle(ctx: ToolContext, timeout: float = 5.0, idle_time: float = 0.5) -> ToolResult:
    try:
        ok = await ctx.browser.wait_for_network_idle(timeout=timeout, idle_time=idle_time)
        summary = f"Network idle detected={ok} (timeout={timeout}s, idle_time={idle_time}s)"
        return ToolResult(success=bool(ok), data={"idle": bool(ok)}, summary=summary, error_code=None if ok else "network_not_idle")
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"wait_for_network_idle failed: {exc}", error_code="network_idle_failed")


async def tool_get_intercepted_apis(ctx: ToolContext) -> ToolResult:
    try:
        apis = await ctx.browser.get_intercepted_apis()
        artifacts = _persist_artifact_if_large(ctx, apis, prefix="intercepted_apis")
        summary = f"Intercepted API candidates: {len(apis) if isinstance(apis, list) else 0}"
        return ToolResult(
            success=True,
            data={"count": len(apis) if isinstance(apis, list) else 0, "preview": (apis or [])[:10]},
            summary=summary,
            artifacts=artifacts,
        )
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"get_intercepted_apis failed: {exc}", error_code="intercepted_apis_failed")


async def tool_detect_data_status(ctx: ToolContext) -> ToolResult:
    try:
        status = await ctx.browser.detect_data_status()
        artifacts = _persist_artifact_if_large(ctx, status, prefix="data_status")
        return ToolResult(success=True, data=status, summary="Data status detection complete", artifacts=artifacts)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"detect_data_status failed: {exc}", error_code="data_status_failed")


async def tool_capture_api_with_interactions(ctx: ToolContext, max_interactions: int = 5, force: bool = False) -> ToolResult:
    try:
        captured = await ctx.browser.capture_api_with_interactions(max_interactions=max_interactions, force=force)
        artifacts = _persist_artifact_if_large(ctx, captured, prefix="capture_api_interactions")
        count = len((captured or {}).get("api_requests", [])) if isinstance(captured, dict) else 0
        summary = f"Captured APIs with interactions: {count}"
        return ToolResult(success=True, data={"api_count": count, "preview": (captured or {})}, summary=summary, artifacts=artifacts)
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"capture_api_with_interactions failed: {exc}",
            error_code="capture_api_interactions_failed",
        )


async def tool_enhanced_page_analysis(ctx: ToolContext) -> ToolResult:
    try:
        analysis = await ctx.browser.enhanced_page_analysis()
        artifacts = _persist_artifact_if_large(ctx, analysis, prefix="enhanced_page_analysis")
        ctx.enhanced_analysis["browser_enhanced_page_analysis"] = analysis
        return ToolResult(success=True, data=analysis, summary="Enhanced page analysis complete", artifacts=artifacts)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"enhanced_page_analysis failed: {exc}", error_code="enhanced_page_analysis_failed")


async def tool_click_next_page(ctx: ToolContext) -> ToolResult:
    try:
        moved = await ctx.browser.click_next_page()
        return ToolResult(
            success=bool(moved),
            data={"moved_to_next_page": bool(moved)},
            summary="Moved to next page" if moved else "No next page available",
            error_code=None if moved else "next_page_not_available",
            recoverable=True,
        )
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"click_next_page failed: {exc}", error_code="next_page_failed")


async def tool_analyze_api_parameters(ctx: ToolContext) -> ToolResult:
    try:
        captured = ctx.browser.get_captured_requests()
        analyzed = ctx.browser.analyze_api_parameters(captured)
        artifacts = _persist_artifact_if_large(ctx, analyzed, prefix="analyze_api_parameters")
        return ToolResult(success=True, data=analyzed, summary="API parameter analysis complete", artifacts=artifacts)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"analyze_api_parameters failed: {exc}", error_code="analyze_api_parameters_failed")


async def tool_build_verified_category_mapping(ctx: ToolContext) -> ToolResult:
    try:
        captured = ctx.browser.get_captured_requests()
        mapping = ctx.browser.build_verified_category_mapping(captured)
        ctx.verified_mapping = mapping
        ctx.enhanced_analysis["verified_category_mapping_v2"] = mapping
        return ToolResult(success=True, data=mapping, summary="Verified category mapping built")
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"build_verified_category_mapping failed: {exc}",
            error_code="build_verified_mapping_failed",
        )


# ---------------------------------------------------------------------------
# High-level tools
# ---------------------------------------------------------------------------


async def tool_get_site_menu_tree(ctx: ToolContext, max_depth: int = 3) -> ToolResult:
    try:
        menu_tree = await ctx.browser.enumerate_menu_tree(max_depth=max_depth)
        ctx.menu_tree = menu_tree
        leaf_paths = menu_tree.get("leaf_paths", [])
        ctx.enhanced_analysis["menu_tree"] = menu_tree

        data = {
            "leaf_count": len(leaf_paths),
            "leaf_paths": leaf_paths[:100],  # Increased limit to avoid missing categories
            "has_menu": len(leaf_paths) > 0,
            "truncated": len(leaf_paths) > 100,
        }
        summary = f"Menu tree: {len(leaf_paths)} leaf nodes"
        if leaf_paths:
            preview = ", ".join(leaf_paths[:3])
            summary += f" (e.g. {preview}...)"
        ctx.log(f"[TOOL] {summary}")
        return ToolResult(success=True, data=data, summary=summary)
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Menu tree extraction failed: {exc}",
            error_code="menu_tree_failed",
            suggested_next_tools=["analyze_page", "take_screenshot"],
        )


async def tool_probe_navigation(ctx: ToolContext, paths: Optional[List[str]] = None) -> ToolResult:
    try:
        if not ctx.menu_tree:
            ctx.menu_tree = await ctx.browser.enumerate_menu_tree(max_depth=3)

        target_paths = paths or ctx.menu_tree.get("leaf_paths", [])
        if not target_paths:
            return ToolResult(
                success=False,
                error="No navigation paths to probe",
                summary="No navigation paths to probe",
                error_code="no_navigation_paths",
                recoverable=True,
                suggested_next_tools=["get_site_menu_tree", "analyze_page"],
            )

        mapping = await ctx.browser.capture_mapping_for_leaf_paths(target_paths)
        ctx.verified_mapping = mapping
        ctx.enhanced_analysis["verified_category_mapping"] = mapping

        filters_count = len((mapping or {}).get("menu_to_filters", {}))
        urls_count = len((mapping or {}).get("menu_to_urls", {}))
        summary = f"Probed {len(target_paths)} paths: {filters_count} filter mappings, {urls_count} URL mappings"
        ctx.log(f"[TOOL] {summary}")
        return ToolResult(
            success=True,
            data={
                "probed_count": len(target_paths),
                "filter_mappings": filters_count,
                "url_mappings": urls_count,
                "menu_to_filters": (mapping or {}).get("menu_to_filters", {}),
                "menu_to_urls": (mapping or {}).get("menu_to_urls", {}),
            },
            summary=summary,
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Navigation probing failed: {exc}",
            error_code="probe_navigation_failed",
            suggested_next_tools=["get_site_menu_tree", "smart_date_api_scan", "analyze_page"],
        )


async def tool_smart_date_api_scan(ctx: ToolContext) -> ToolResult:
    try:
        try:
            from date_api_extractor import DateAPIExtractor
        except ImportError:
            from .date_api_extractor import DateAPIExtractor  # type: ignore

        if not ctx.network_requests:
            ctx.network_requests = ctx.browser.get_captured_requests()

        extractor = DateAPIExtractor(ctx.browser, ctx.llm)

        def log_cb(msg):
            ctx.log(f"[DATE-API] {msg}")

        result = await extractor.extract_with_three_layers(
            ctx.network_requests,
            ctx.start_date,
            ctx.end_date,
            log_callback=log_cb,
        )
        ctx.date_api_result = result

        if result.success and result.best_candidate:
            candidate = result.best_candidate
            layer_name = {
                0: "global_var_scan",
                1: "api_direct",
                2: "dom_automation",
                3: "llm_vision",
            }.get(result.layer, "unknown")

            code_snippet = extractor.generate_api_code_snippet(candidate, ctx.start_date, ctx.end_date)
            replay_url, _ = extractor.build_replay_url(candidate, ctx.start_date, ctx.end_date)

            ctx.enhanced_analysis["date_api_extraction"] = {
                "success": True,
                "layer": result.layer,
                "layer_name": layer_name,
                "api_url": candidate.url,
                "method": candidate.method,
                "base_params": candidate.params,
                "date_params": candidate.date_params,
                "data_count": result.data_count,
                "code_snippet": code_snippet,
                "replayed_url": replay_url,
                "replayed_data": result.replayed_data,
            }

            summary = (
                f"SUCCESS (Layer {result.layer}: {layer_name}): "
                f"API={candidate.url[:80]}, "
                f"date_params={list(candidate.date_params.keys())}, "
                f"data_count={result.data_count}"
            )
            ctx.log(f"[TOOL] Date API scan success: {summary}")
            return ToolResult(
                success=True,
                data={
                    "success": True,
                    "layer": result.layer,
                    "layer_name": layer_name,
                    "api_url": candidate.url[:200],
                    "method": candidate.method,
                    "date_params": candidate.date_params,
                    "data_count": result.data_count,
                    "code_snippet": code_snippet[:500],
                },
                summary=summary,
            )

        errors: Dict[str, str] = {}
        for i, label in [(0, "global_var"), (1, "api_direct"), (2, "dom_detect"), (3, "llm_vision")]:
            layer_result = getattr(result, f"layer{i}_result", None)
            if layer_result:
                errors[label] = layer_result.get("error", "unknown")

        ctx.enhanced_analysis["date_api_extraction"] = {
            "success": False,
            "layer_errors": errors,
            "candidates_count": len(result.candidates),
        }

        summary = f"FAILED: all date-api layers failed. errors={errors}"
        ctx.log(f"[TOOL] Date API scan failed: {summary}")
        return ToolResult(
            success=False,
            data={"success": False, "layer_errors": errors, "candidates_count": len(result.candidates)},
            summary=summary,
            error_code="date_api_scan_failed",
            recoverable=True,
            suggested_next_tools=["take_screenshot", "analyze_page", "probe_navigation"],
        )
    except Exception as exc:
        ctx.log(f"[TOOL] Date API scan exception: {exc}")
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Date API scan error: {exc}",
            error_code="date_api_scan_exception",
            recoverable=True,
            suggested_next_tools=["take_screenshot", "analyze_page"],
        )


async def tool_generate_crawler_code(ctx: ToolContext, strategy: str = "") -> ToolResult:
    try:
        if not ctx.page_html:
            ctx.page_html = await ctx.browser.get_full_html()
        if not ctx.page_structure:
            ctx.page_structure = await ctx.browser.analyze_page_structure()
        if not ctx.network_requests:
            ctx.network_requests = ctx.browser.get_captured_requests()

        requirements = ctx.extra_requirements or ""
        if strategy:
            requirements += f"\n\n[Agent Strategy]: {strategy}"

        ctx.log(f"[TOOL] Generating crawler code (model={ctx.config.qwen_model})")

        script_code = ctx.llm.generate_crawler_script(
            page_url=ctx.url,
            page_html=ctx.page_html,
            page_structure=ctx.page_structure,
            network_requests=ctx.network_requests,
            user_requirements=requirements if requirements.strip() else None,
            start_date=ctx.start_date,
            end_date=ctx.end_date,
            enhanced_analysis=ctx.enhanced_analysis or None,
            attachments=ctx.attachments,
            run_mode=ctx.run_mode,
            task_id=ctx.task_id,
        )

        if not isinstance(script_code, str) or not script_code.strip():
            return ToolResult(
                success=False,
                error="LLM returned empty code",
                summary="LLM returned empty code",
                error_code="empty_code",
                recoverable=True,
                suggested_next_tools=["analyze_page", "get_network_requests"],
            )

        ctx.generated_code = script_code
        ctx.code_generation_strategy = strategy

        lines = script_code.count("\n") + 1
        summary = f"Code generated: {lines} lines, {len(script_code):,} chars"
        artifacts = _persist_artifact_if_large(ctx, {"code": script_code}, prefix="generated_code")
        ctx.log(f"[TOOL] {summary}")
        return ToolResult(
            success=True,
            data={
                "lines": lines,
                "chars": len(script_code),
                "preview": script_code[:800] + "..." if len(script_code) > 800 else script_code,
            },
            summary=summary,
            artifacts=artifacts,
        )
    except Exception as exc:
        ctx.log(f"[TOOL] Code generation failed: {exc}")
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Code generation failed: {exc}",
            error_code="code_generation_failed",
            recoverable=True,
            suggested_next_tools=["analyze_page", "get_network_requests", "smart_date_api_scan"],
        )


async def tool_validate_code(ctx: ToolContext, code: str = "") -> ToolResult:
    try:
        try:
            from validator import StaticCodeValidator
        except ImportError:
            from .validator import StaticCodeValidator  # type: ignore

        target_code = code or ctx.generated_code
        if not target_code:
            return ToolResult(success=False, error="No code to validate", summary="No code to validate", error_code="no_code")

        validator = StaticCodeValidator()
        issues = validator.validate(target_code)
        errors = [it for it in issues if it.severity.value == "error"]
        warnings = [it for it in issues if it.severity.value == "warning"]

        data = {
            "errors": len(errors),
            "warnings": len(warnings),
            "error_details": [
                {"code": it.code, "message": it.message, "line": getattr(it, "line_number", None)}
                for it in errors[:5]
            ],
        }

        if errors:
            summary = f"Validation FAILED: {len(errors)} errors, {len(warnings)} warnings"
            ctx.log(f"[TOOL] {summary}")
            return ToolResult(
                success=False,
                data=data,
                summary=summary,
                error_code="validation_failed",
                recoverable=True,
                suggested_next_tools=["generate_crawler_code", "critic_validate"],
            )

        summary = f"Validation PASSED ({len(warnings)} warnings)"
        ctx.log(f"[TOOL] {summary}")
        return ToolResult(success=True, data=data, summary=summary)
    except ImportError:
        return ToolResult(success=True, summary="Validator not available, skipped", confidence=0.5)
    except Exception as exc:
        return ToolResult(success=False, error=str(exc), summary=f"Validation error: {exc}", error_code="validation_exception")


# ---------------------------------------------------------------------------
# Sandbox / critic tools
# ---------------------------------------------------------------------------


async def tool_run_python_snippet(ctx: ToolContext, code: str, timeout_sec: int = 120) -> ToolResult:
    if not code or not code.strip():
        return ToolResult(success=False, error="Code is empty", summary="Code is empty", error_code="empty_code_input")

    if "[HTML_CONTENT_PLACEHOLDER]" in code or "HTML_CONTENT_PLACEHOLDER" in code:
        return ToolResult(
            success=False,
            error="[HTML_CONTENT_PLACEHOLDER] is not supported. Use extract_list_and_pagination to parse page HTML.",
            summary="[REPLAN_REQUIRED] Placeholder not supported – use extract_list_and_pagination instead",
            error_code="placeholder_not_supported",
            recoverable=True,
            suggested_next_tools=["extract_list_and_pagination", "capture_api_and_infer_params"],
        )

    if "ctx." in code or "ctx[" in code:
        return ToolResult(
            success=False,
            error="run_python_snippet runs in a sandbox and cannot access ctx. Use extract_list_and_pagination or capture_api_and_infer_params.",
            summary="[REPLAN_REQUIRED] ctx is not accessible in sandbox – use high-level tools instead",
            error_code="ctx_not_accessible",
            recoverable=True,
            suggested_next_tools=["extract_list_and_pagination", "capture_api_and_infer_params"],
        )

    if not ctx.executor_session:
        return ToolResult(
            success=False,
            error="Executor session is not configured",
            summary="Executor session unavailable",
            error_code="executor_unavailable",
            recoverable=True,
            suggested_next_tools=["generate_crawler_code"],
        )

    try:
        result = await ctx.executor_session.run_python(code=code, timeout_sec=timeout_sec)

        if hasattr(result, "to_dict"):
            result_payload = result.to_dict()
            success = bool(getattr(result, "success", False))
            stdout = getattr(result, "stdout", "")
            stderr = getattr(result, "stderr", "")
            error = getattr(result, "error", None)
        elif isinstance(result, dict):
            result_payload = result
            success = bool(result.get("success", False))
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            error = result.get("error")
        else:
            result_payload = {"raw": str(result)}
            success = False
            stdout = ""
            stderr = ""
            error = "Unexpected executor response"

        artifacts: Dict[str, Any] = {}
        if ctx.artifact_store and (len(stdout) > 4000 or len(stderr) > 4000):
            try:
                ref = ctx.artifact_store.put_json(result_payload, prefix="executor_python")
                artifacts["artifact_ref"] = ref.to_prompt_dict()
            except Exception as exc:
                ctx.log(f"[TOOL] Failed to store executor artifact: {exc}")

        summary = "Python snippet executed successfully" if success else "Python snippet execution failed"
        return ToolResult(
            success=success,
            data={
                "stdout_preview": stdout[:1200],
                "stderr_preview": stderr[:1200],
                "return_payload_preview": _safe_data_preview(result_payload, 1500),
            },
            error=error,
            summary=summary,
            error_code=None if success else "executor_python_failed",
            recoverable=True,
            suggested_next_tools=[] if success else ["run_python_snippet", "generate_crawler_code"],
            artifacts=artifacts,
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Executor error: {exc}",
            error_code="executor_exception",
            recoverable=True,
            suggested_next_tools=["generate_crawler_code"],
        )


def _extract_allowed_packages(ctx: ToolContext) -> tuple[List[str], bool]:
    """
    Resolve package policy from config.
    Returns (allowlist, allow_any).
    """
    try:
        cfg_obj = getattr(ctx, "config", None)
        cfg_raw = getattr(cfg_obj, "config", None)
        if isinstance(cfg_raw, dict):
            sandbox_cfg = cfg_raw.get("sandbox", {})
            allow_any = bool(sandbox_cfg.get("allow_any_package", False))
            allowed = sandbox_cfg.get("allowed_packages")
            if isinstance(allowed, list):
                return ([str(item).strip() for item in allowed if str(item).strip()], allow_any)
            return (DEFAULT_ALLOWED_PACKAGES[:], allow_any)
    except Exception:
        pass
    return (DEFAULT_ALLOWED_PACKAGES[:], False)


def _is_package_allowed(package_name: str, allowlist: List[str], allow_any: bool = False) -> bool:
    if allow_any:
        return True
    normalized = package_name.strip().lower()
    base = normalized.split("==")[0].split(">=")[0].split("<=")[0].split("[")[0].strip()
    return any(base == item.lower() or base.startswith(item.lower() + "-") for item in allowlist)


async def tool_install_python_packages(ctx: ToolContext, packages: List[str], timeout_sec: int = 900) -> ToolResult:
    if not ctx.executor_session:
        return ToolResult(
            success=False,
            error="Executor session is not configured",
            summary="Executor session unavailable",
            error_code="executor_unavailable",
            recoverable=True,
            suggested_next_tools=["generate_crawler_code"],
        )

    cleaned = [pkg.strip() for pkg in (packages or []) if isinstance(pkg, str) and pkg.strip()]
    if not cleaned:
        return ToolResult(
            success=False,
            error="No packages provided",
            summary="No packages provided",
            error_code="no_packages",
            recoverable=True,
        )

    allowlist, allow_any = _extract_allowed_packages(ctx)
    rejected = [pkg for pkg in cleaned if not _is_package_allowed(pkg, allowlist, allow_any=allow_any)]
    if rejected:
        return ToolResult(
            success=False,
            error=f"Packages not allowed by sandbox policy: {rejected}",
            summary=f"Rejected packages by allowlist: {rejected}",
            error_code="package_not_allowed",
            recoverable=True,
            suggested_next_tools=["run_python_snippet"],
            data={"allowlist": allowlist, "allow_any_package": allow_any, "rejected": rejected},
        )

    try:
        req_status = await ctx.executor_session.check_python_requirements(cleaned)
        to_install = list((req_status.get("missing", []) or [])) + list((req_status.get("conflicts", []) or []))
        already_satisfied = list(req_status.get("satisfied", []) or [])

        if not to_install:
            return ToolResult(
                success=True,
                summary="All requested packages are already satisfied. Installation skipped.",
                data={
                    "requested": cleaned,
                    "installed": [],
                    "skipped_as_satisfied": already_satisfied,
                    "requirement_status": req_status,
                },
            )

        result = await ctx.executor_session.install_python_packages(to_install, timeout_sec=timeout_sec)
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        success = bool(payload.get("success", False))
        return ToolResult(
            success=success,
            data={
                "requested": cleaned,
                "installed": to_install,
                "skipped_as_satisfied": already_satisfied,
                "requirement_status": req_status,
                "stdout_preview": (payload.get("stdout") or "")[:1600],
                "stderr_preview": (payload.get("stderr") or "")[:1600],
                "exit_code": payload.get("exit_code", 0),
            },
            error=payload.get("error"),
            summary="Package install completed" if success else "Package install failed",
            error_code=None if success else "package_install_failed",
            recoverable=True,
            suggested_next_tools=["run_python_snippet"] if success else ["install_python_packages", "generate_crawler_code"],
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Package install exception: {exc}",
            error_code="package_install_exception",
            recoverable=True,
            suggested_next_tools=["generate_crawler_code"],
        )


async def tool_critic_validate(ctx: ToolContext, objective: str = "", min_items: int = 1) -> ToolResult:
    if not ctx.critic:
        return ToolResult(
            success=False,
            error="Critic is not configured",
            summary="Critic unavailable",
            error_code="critic_unavailable",
            recoverable=True,
            suggested_next_tools=["validate_code"],
        )

    code = ctx.generated_code or ""
    if not code.strip():
        return ToolResult(
            success=False,
            error="No generated code to validate",
            summary="No generated code to validate",
            error_code="critic_no_code",
            recoverable=True,
            suggested_next_tools=["generate_crawler_code"],
        )

    try:
        if getattr(ctx.critic, "artifact_store", None) is None and ctx.artifact_store is not None:
            try:
                ctx.critic.artifact_store = ctx.artifact_store
            except Exception:
                pass

        if hasattr(ctx.critic, "evaluate_generated_code_async"):
            verdict = await ctx.critic.evaluate_generated_code_async(
                code=code,
                run_mode=ctx.run_mode,
                objective=objective or ctx.extra_requirements,
                min_items=min_items,
                target_url=ctx.url,
                max_retries=3,
                executor_session=ctx.executor_session,
                enhanced_analysis=getattr(ctx, "enhanced_analysis", None),
            )
        else:
            verdict = ctx.critic.evaluate_generated_code(
                code=code,
                run_mode=ctx.run_mode,
                objective=objective or ctx.extra_requirements,
                min_items=min_items,
            )

        passed = bool(getattr(verdict, "passed", False)) if not isinstance(verdict, dict) else bool(verdict.get("passed", False))
        payload = verdict.to_dict() if hasattr(verdict, "to_dict") else verdict

        if isinstance(payload, dict):
            maybe_repaired = (payload.get("details") or {}).get("repaired_code")
            if isinstance(maybe_repaired, str) and maybe_repaired.strip():
                ctx.generated_code = maybe_repaired

        summary = payload.get("summary", "Critic completed") if isinstance(payload, dict) else "Critic completed"
        return ToolResult(
            success=passed,
            data=payload,
            summary=summary,
            error_code=None if passed else "critic_failed",
            recoverable=True,
            suggested_next_tools=[] if passed else ["analyze_page", "generate_crawler_code", "validate_code"],
            confidence=(payload.get("confidence") if isinstance(payload, dict) else None),
        )
    except Exception as exc:
        return ToolResult(
            success=False,
            error=str(exc),
            summary=f"Critic error: {exc}",
            error_code="critic_exception",
            recoverable=True,
            suggested_next_tools=["validate_code", "generate_crawler_code"],
        )


__all__ = [
    "ToolContext",
    "ToolResult",
    "tool_open_page",
    "tool_scroll_page",
    "tool_get_page_info",
    "tool_get_page_html",
    "tool_extract_list_items_from_ctx_html",
    "tool_analyze_page",
    "tool_take_screenshot",
    "tool_get_network_requests",
    "tool_wait_for_network_idle",
    "tool_get_intercepted_apis",
    "tool_detect_data_status",
    "tool_capture_api_with_interactions",
    "tool_enhanced_page_analysis",
    "tool_click_next_page",
    "tool_analyze_api_parameters",
    "tool_build_verified_category_mapping",
    "tool_get_site_menu_tree",
    "tool_probe_navigation",
    "tool_smart_date_api_scan",
    "tool_generate_crawler_code",
    "tool_validate_code",
    "tool_run_python_snippet",
    "tool_install_python_packages",
    "tool_critic_validate",
]
