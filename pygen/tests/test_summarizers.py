"""Tests for the rule-based artifact summarizers."""

from __future__ import annotations

import sys
from pathlib import Path

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from summarizers import (  # noqa: E402
    summarize_analyze_page,
    summarize_html,
    summarize_json_payload,
)


# ---------------------------------------------------------------------------
# HTML summarizer
# ---------------------------------------------------------------------------


_LIST_HTML = """
<html>
  <head>
    <title>News Hub</title>
    <meta name="description" content="Latest news from somewhere">
    <link rel="canonical" href="https://example.com/news">
    <script src="/static/main.js"></script>
  </head>
  <body>
    <header><nav class="menu"><a href="/">Home</a></nav></header>
    <main>
      <ul class="news-list">
        <li class="article"><a href="/n/1">Story 1</a><time>2026-04-01</time></li>
        <li class="article"><a href="/n/2">Story 2</a><time>2026-04-02</time></li>
        <li class="article"><a href="/n/3">Story 3</a><time>2026-04-03</time></li>
        <li class="article"><a href="/n/4">Story 4</a><time>2026-04-04</time></li>
      </ul>
      <div class="pager">
        <a href="?page=1" class="page-num">1</a>
        <a href="?page=2" class="page-num">2</a>
        <a class="next" href="?page=2">Next</a>
      </div>
    </main>
  </body>
</html>
"""


def test_summarize_html_returns_expected_keys():
    out = summarize_html(_LIST_HTML, url="https://example.com/news")
    assert "_summary_error" not in out
    for k in (
        "url", "title", "rendered_size_chars", "page_kind_guess",
        "list_candidates", "pagination_signals", "date_signals",
        "anti_bot_signals", "head_meta", "preview_head", "preview_tail",
        "fallback_hints",
    ):
        assert k in out, f"missing {k}"


def test_summarize_html_finds_list_candidates():
    out = summarize_html(_LIST_HTML, url="https://example.com/news")
    cands = out["list_candidates"]
    assert isinstance(cands, list)
    # We injected a 4-item list, expect at least one candidate
    assert any(c.get("count", 0) >= 3 for c in cands)


def test_summarize_html_picks_up_pagination_signals():
    out = summarize_html(_LIST_HTML, url="https://example.com/news")
    pag = out["pagination_signals"]
    # Either a next_link or page_param should surface
    assert pag.get("next_link") or pag.get("page_param") or (pag.get("page_nums_count") or 0) > 0


def test_summarize_html_extracts_date_signals():
    out = summarize_html(_LIST_HTML, url="https://example.com/news")
    dates = out["date_signals"]
    assert dates, "expected at least one date signal"
    assert any("2026-04" in s for d in dates for s in d.get("samples", []))


def test_summarize_html_detects_antibot():
    blocked = "<html><body>Please enable JavaScript and Cookies to continue. cf-ray</body></html>"
    out = summarize_html(blocked, url="https://example.com/")
    # cloudflare keyword OR js_required keyword should appear
    assert out["anti_bot_signals"], "should flag at least one anti-bot signal"


def test_summarize_html_accepts_dict_wrapper():
    out = summarize_html({"html": _LIST_HTML}, url="https://example.com/")
    assert "_summary_error" not in out
    assert out["title"] == "News Hub"


def test_summarize_html_never_raises_on_garbage():
    out = summarize_html("not really html <<<", url="")
    assert isinstance(out, dict)
    # garbage is still parseable by html.parser, just yields little
    assert "page_kind_guess" in out


def test_summarize_html_returns_fallback_hints_with_selector():
    out = summarize_html(_LIST_HTML, url="https://example.com/")
    hints = out["fallback_hints"]
    assert hints
    # First hint should reference a CSS scope drawn from a candidate
    assert any("css:" in h for h in hints) or any("head:" in h for h in hints)


# ---------------------------------------------------------------------------
# JSON / network_requests summarizer
# ---------------------------------------------------------------------------


_NET_PAYLOAD = {
    "api_requests": [
        {
            "url": "https://api.example.com/news?page=1",
            "method": "GET",
            "response_status": 200,
            "content_type": "application/json; charset=utf-8",
            "response_body": '{"items":[{"id":1,"title":"hi"}],"total":42}',
        },
        {
            "url": "https://api.example.com/news?page=2",
            "method": "GET",
            "response_status": 200,
            "content_type": "application/json",
            "response_body": "[]",
        },
    ],
    "all_requests": [{"url": "https://example.com/main.js"}],
}


def test_summarize_json_payload_basic_signals():
    out = summarize_json_payload(_NET_PAYLOAD)
    assert out["total_api_requests"] == 2
    assert ("api.example.com", 2) in out["top_hosts"]
    assert out["method_distribution"].get("GET") == 2
    assert out["status_distribution"].get("200") == 2
    assert "application/json" in out["content_type_distribution"]


def test_summarize_json_payload_extracts_response_keys():
    out = summarize_json_payload(_NET_PAYLOAD)
    eps = out["candidate_endpoints"]
    assert eps and eps[0]["url"].startswith("https://api.example.com/")
    assert "items" in eps[0]["response_keys_top"]


def test_summarize_json_payload_handles_garbage():
    out = summarize_json_payload("not a dict")
    assert "_summary_error" in out


def test_summarize_json_payload_emits_fallback_hints():
    out = summarize_json_payload(_NET_PAYLOAD)
    assert any("api_requests" in h for h in out["fallback_hints"])


# ---------------------------------------------------------------------------
# Composite analyze_page summarizer
# ---------------------------------------------------------------------------


def test_summarize_analyze_page_aggregates_subsignals():
    payload = {
        "page_info": {"title": "T", "url": "https://example.com/"},
        "page_structure": {
            "tables": [{}, {}],
            "lists": [{}],
            "forms": [],
            "iframes": [],
            "links": {
                "pdfLinks": ["a.pdf", "b.pdf"],
                "internalLinks": ["/x"],
                "externalLinks": [],
            },
        },
        "network_requests": _NET_PAYLOAD,
    }
    out = summarize_analyze_page(payload)
    assert out["page_info"]["title"] == "T"
    assert out["structure_signals"]["tables"] == 2
    assert out["structure_signals"]["pdf_links"] == 2
    assert out["network"]["total_api_requests"] == 2
    assert out["fallback_hints"], "should include at least one hint"


def test_summarize_analyze_page_handles_garbage():
    out = summarize_analyze_page("nope")
    assert "_summary_error" in out
