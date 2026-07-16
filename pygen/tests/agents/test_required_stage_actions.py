"""Required stage actions must not depend on optional ReAct tool selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _context(**overrides):
    values = {
        "task_id": "task-1",
        "url": "https://example.test/news/",
        "run_mode": "news",
        "start_date": "",
        "end_date": "",
        "extra_requirements": "",
        "page_info": {"url": "https://example.test/news/", "title": "News"},
        "page_html": "<html>" + ("x" * 2000) + "</html>",
        "page_structure": {"lists": [{"count": 5}], "links": {}},
        "network_requests": {},
        "date_api_result": None,
        "verified_mapping": None,
        "verified_selectors": None,
        "menu_tree": None,
        "enhanced_analysis": {},
        "screenshots": [],
        "generated_code": None,
        "code_generation_strategy": None,
        "log": lambda _message: None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(ctx):
    return {"configurable": {"tool_context": ctx}}


@pytest.mark.asyncio
async def test_required_selector_runs_extract_when_react_skips_tool(monkeypatch):
    from agents import tools_lc
    from tools import ToolResult

    ctx = _context()
    called = []

    async def fake_extract(live_ctx):
        called.append("extract")
        live_ctx.verified_selectors = {
            "list": {"container": "ul.news > li", "title_link": "a.title"}
        }
        return ToolResult(success=True, summary="verified five rows")

    monkeypatch.setattr(tools_lc, "tool_extract_list_and_pagination", fake_extract)
    output = await tools_lc.run_required_selector({}, _config(ctx))

    assert called == ["extract"]
    assert output["verified_selectors"]["list"]["title_link"] == "a.title"
    assert output["tool_calls_log"][0]["action"] == "extract_list_and_pagination"


@pytest.mark.asyncio
async def test_required_selector_does_not_repeat_verified_bundle(monkeypatch):
    from agents import tools_lc

    ctx = _context(verified_selectors={
        "list": {"container": "ul.news > li", "title_link": "a.title"}
    })

    async def unexpected(_ctx):
        raise AssertionError("verified selector bundle should be reused")

    monkeypatch.setattr(tools_lc, "tool_extract_list_and_pagination", unexpected)
    assert await tools_lc.run_required_selector({}, _config(ctx)) == {}


@pytest.mark.asyncio
async def test_required_site_profile_restores_requested_page(monkeypatch):
    from agents import tools_lc
    from tools import ToolResult

    ctx = _context(
        page_info={"url": "https://other.test/", "title": "Other"},
        page_html="old",
        page_structure={"lists": []},
    )

    async def fake_open(live_ctx, url="", wait_until="domcontentloaded"):
        live_ctx.page_info = {"url": url, "title": "News"}
        live_ctx.page_html = None
        live_ctx.page_structure = None
        return ToolResult(success=True, summary="restored")

    async def fake_analyze(live_ctx):
        live_ctx.page_html = "<html>fresh</html>" * 20
        live_ctx.page_structure = {"lists": [{"count": 5}]}
        return ToolResult(success=True, summary="analyzed")

    monkeypatch.setattr(tools_lc, "tool_open_page", fake_open)
    monkeypatch.setattr(tools_lc, "tool_analyze_page", fake_analyze)
    output = await tools_lc.run_required_site_profile({}, _config(ctx))

    assert ctx.page_info["url"] == ctx.url
    assert [entry["action"] for entry in output["tool_calls_log"]] == [
        "open_page", "analyze_page"
    ]


@pytest.mark.asyncio
async def test_required_api_probe_is_attempted_once(monkeypatch):
    from agents import tools_lc
    from tools import ToolResult

    ctx = _context()
    calls = 0

    async def fake_capture(live_ctx):
        nonlocal calls
        calls += 1
        live_ctx.enhanced_analysis["captured_data_api"] = {
            "bestApi": {"url": "https://example.test/api/news"}
        }
        return ToolResult(success=True, summary="data API verified")

    monkeypatch.setattr(tools_lc, "tool_capture_api_and_infer_params", fake_capture)
    first = await tools_lc.run_required_api_discovery({}, _config(ctx))
    second = await tools_lc.run_required_api_discovery({}, _config(ctx))

    assert calls == 1
    assert first["enhanced_analysis"]["captured_data_api"]["bestApi"]["url"]
    assert second == {}
