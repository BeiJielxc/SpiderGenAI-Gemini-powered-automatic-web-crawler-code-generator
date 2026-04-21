"""Rule-based summarizers for JSON-shaped artifacts.

Two summarizers live here:

* ``summarize_json_payload``: for the ``network_requests`` / captured-API
  shape (``{"api_requests": [...], "all_requests": [...]}``).
* ``summarize_analyze_page``: for the ``analyze_page`` composite payload
  (``{"page_info", "page_structure", "network_requests"}``); delegates
  to the html and json summarizers internally.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List
from urllib.parse import urlparse


def summarize_json_payload(content: Any) -> Dict[str, Any]:
    """Summarize a captured-network-requests blob.

    Tolerates both the raw dict produced by ``ctx.browser.get_captured_requests``
    and any other JSON. Extracts:
      * total request counts
      * top hosts / paths
      * status code distribution
      * first 5 candidate API endpoints (URL + method + content_type + size)
      * union of top-level JSON response keys (helps the LLM pick the right
        endpoint without re-fetching)
    """
    try:
        if not isinstance(content, dict):
            return {
                "_summary_error": f"unsupported payload type: {type(content).__name__}",
            }

        api_requests: List[Dict[str, Any]] = list(content.get("api_requests") or [])
        all_requests: List[Dict[str, Any]] = list(content.get("all_requests") or [])

        host_counter: Counter = Counter()
        status_counter: Counter = Counter()
        method_counter: Counter = Counter()
        content_type_counter: Counter = Counter()

        for r in api_requests or all_requests:
            url = r.get("url") or ""
            host = urlparse(url).netloc if url else ""
            if host:
                host_counter[host] += 1
            method = (r.get("method") or "").upper()
            if method:
                method_counter[method] += 1
            status = r.get("response_status") or r.get("status")
            if status is not None:
                status_counter[str(status)] += 1
            ct = (r.get("content_type") or r.get("contentType") or "").split(";")[0].strip()
            if ct:
                content_type_counter[ct] += 1

        endpoints: List[Dict[str, Any]] = []
        seen_urls: set = set()
        for r in api_requests[:50]:
            url = (r.get("url") or "")[:200]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            body_preview = (
                r.get("response_body")
                or r.get("responseBody")
                or r.get("response_preview")
                or ""
            )
            if isinstance(body_preview, (dict, list)):
                body_preview = str(body_preview)
            endpoints.append({
                "url": url,
                "method": (r.get("method") or "").upper(),
                "status": r.get("response_status") or r.get("status"),
                "content_type": (r.get("content_type") or r.get("contentType") or "").split(";")[0],
                "response_chars": len(body_preview) if isinstance(body_preview, str) else 0,
                "response_keys_top": _top_response_keys(body_preview),
            })
            if len(endpoints) >= 5:
                break

        result: Dict[str, Any] = {
            "total_api_requests": len(api_requests),
            "total_all_requests": len(all_requests),
            "top_hosts": host_counter.most_common(5),
            "method_distribution": dict(method_counter.most_common()),
            "status_distribution": dict(status_counter.most_common()),
            "content_type_distribution": dict(content_type_counter.most_common(5)),
            "candidate_endpoints": endpoints,
        }
        result["fallback_hints"] = _fallback_hints(result)
        return result
    except Exception as exc:
        return {"_summary_error": f"{type(exc).__name__}: {exc}"}


def summarize_analyze_page(content: Any) -> Dict[str, Any]:
    """Summarize the composite ``analyze_page`` payload."""
    try:
        if not isinstance(content, dict):
            return {"_summary_error": f"unsupported payload type: {type(content).__name__}"}

        page_info = content.get("page_info") or {}
        page_structure = content.get("page_structure") or {}
        network_requests = content.get("network_requests") or {}

        out: Dict[str, Any] = {
            "page_info": {
                "title": (page_info.get("title") or "")[:200],
                "url": page_info.get("url") or "",
            },
            "structure_signals": {
                "tables": len(page_structure.get("tables") or []),
                "lists": len(page_structure.get("lists") or []),
                "forms": len(page_structure.get("forms") or []),
                "iframes": len(page_structure.get("iframes") or []),
                "pdf_links": len((page_structure.get("links") or {}).get("pdfLinks") or []),
                "internal_links": len((page_structure.get("links") or {}).get("internalLinks") or []),
                "external_links": len((page_structure.get("links") or {}).get("externalLinks") or []),
            },
            "network": summarize_json_payload(network_requests),
        }

        hints: List[str] = []
        api_count = out["network"].get("total_api_requests", 0)
        if api_count:
            hints.append("read_artifact(id, scope='jsonpath:network_requests.api_requests[0]')")
        if out["structure_signals"]["lists"] > 0:
            hints.append("read_artifact(id, scope='jsonpath:page_structure.lists')")
        if out["structure_signals"]["pdf_links"] > 0:
            hints.append("read_artifact(id, scope='jsonpath:page_structure.links.pdfLinks')")
        if not hints:
            hints.append("read_artifact(id)  # inspect full payload")
        out["fallback_hints"] = hints
        return out
    except Exception as exc:
        return {"_summary_error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _top_response_keys(body: Any, limit: int = 8) -> List[str]:
    """If body looks like JSON, return its top-level keys (or array element keys)."""
    if not body:
        return []
    text = body if isinstance(body, str) else None
    if text is None:
        return []
    text = text.strip()
    if not text or text[0] not in "{[":
        return []
    try:
        import json as _json
        obj = _json.loads(text)
    except Exception:
        return []
    if isinstance(obj, dict):
        return list(obj.keys())[:limit]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return list(obj[0].keys())[:limit]
    return []


def _fallback_hints(result: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    if result.get("candidate_endpoints"):
        hints.append("read_artifact(id, scope='jsonpath:api_requests[0]')")
        hints.append("read_artifact(id, scope='jsonpath:api_requests[0].response_body')")
    elif result.get("total_all_requests", 0) > 0:
        hints.append("read_artifact(id, scope='jsonpath:all_requests[0]')")
    else:
        hints.append("read_artifact(id)  # full payload")
    return hints
