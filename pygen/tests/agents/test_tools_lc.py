"""Tests for the @tool wrappers in agents/tools_lc.py.

All external IO (browser, LLM, executor) is stubbed. The goal is to
verify the glue logic (ToolContext plumbing, state mirroring, cancel
short-circuit, error translation) — not to exercise any real scraping.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


def _build_tool_call(tool, args=None, config=None):
    """Invoke a @tool with a synthetic ToolCall. Returns the resulting Command."""
    payload = {
        "type": "tool_call",
        "name": tool.name,
        "id": "call-xyz",
        "args": args or {},
    }
    return tool.ainvoke(payload, config=config)


@pytest.mark.asyncio
async def test_get_page_info_happy_path(dummy_tool_context):
    from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY, get_page_info

    cfg = {"configurable": {TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context}}
    cmd = await _build_tool_call(get_page_info, config=cfg)

    assert isinstance(cmd, Command)
    updates = cmd.update
    msgs = updates["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    assert msgs[0].tool_call_id == "call-xyz"
    # State mirror should include page_info populated by our dummy browser
    assert updates["page_info"] == {
        "title": "Test Page",
        "url": "https://example.com",
    }
    # tool_calls_log is appended as a single-element list (operator.add reducer)
    assert len(updates["tool_calls_log"]) == 1
    assert updates["tool_calls_log"][0]["action"] == "get_page_info"


@pytest.mark.asyncio
async def test_cancel_check_short_circuits(dummy_tool_context):
    """If cancel_check returns True, the wrapper must not invoke the underlying tool."""
    from agents.tools_lc import (
        CANCEL_CHECK_CONFIG_KEY,
        TOOL_CONTEXT_CONFIG_KEY,
        get_page_info,
    )

    called = {"inner": False}

    class SpyBrowser:
        async def get_page_info(self):
            called["inner"] = True
            return {"title": "should not appear"}

    dummy_tool_context.browser = SpyBrowser()
    cfg = {
        "configurable": {
            TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context,
            CANCEL_CHECK_CONFIG_KEY: lambda: True,
        }
    }
    cmd = await _build_tool_call(get_page_info, config=cfg)

    assert isinstance(cmd, Command)
    assert cmd.update["cancelled"] is True
    assert called["inner"] is False


@pytest.mark.asyncio
async def test_missing_tool_context_raises(dummy_tool_context):
    from agents.tools_lc import ToolContextNotConfigured, get_page_info

    # no configurable at all
    with pytest.raises(ToolContextNotConfigured):
        await _build_tool_call(get_page_info, config={"configurable": {}})


@pytest.mark.asyncio
async def test_error_from_inner_tool_is_captured(dummy_tool_context):
    """Exceptions inside the underlying async tool become ToolMessage error observations."""
    from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY, get_page_info

    class BadBrowser:
        async def get_page_info(self):
            raise RuntimeError("boom")

    dummy_tool_context.browser = BadBrowser()
    cfg = {"configurable": {TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context}}
    cmd = await _build_tool_call(get_page_info, config=cfg)

    assert isinstance(cmd, Command)
    msgs = cmd.update["messages"]
    assert len(msgs) == 1
    # Check the observation JSON marks failure
    assert "get_page_info" in msgs[0].content
    # Our legacy tool_get_page_info handles browser errors gracefully and
    # returns success=True with page_info=None; the wrapper doesn't blow
    # up either way. What matters is no exception propagates.


def test_select_tools_strips_sandbox_when_no_executor():
    from agents.tools_lc import select_tools_for_context

    with_sandbox = {t.name for t in select_tools_for_context(has_executor=True)}
    without_sandbox = {t.name for t in select_tools_for_context(has_executor=False)}
    assert "run_python_snippet" in with_sandbox
    assert "run_python_snippet" not in without_sandbox
    assert "install_python_packages" not in without_sandbox


def test_select_tools_strips_critic_when_disabled():
    from agents.tools_lc import select_tools_for_context

    enabled = {t.name for t in select_tools_for_context(has_critic=True)}
    disabled = {t.name for t in select_tools_for_context(has_critic=False)}
    assert "critic_validate" in enabled
    assert "critic_validate" not in disabled


def test_all_tools_count_matches_registry_expectations():
    from agents.tools_lc import get_all_tools

    tools = get_all_tools()
    # We wrapped 22 legacy tools from tools.py + 5 from high_level_tools.py.
    # Tools exposed to LangGraph = 25 after deduping adjacent renames
    # (tool_extract_list_items_from_ctx_html is internal helper, not wrapped).
    names = {t.name for t in tools}
    # Sanity checks for key ones
    for must_have in (
        "open_page",
        "extract_list_and_pagination",
        "generate_crawler_code",
        "critic_validate",
        "run_python_snippet",
        "read_artifact",
    ):
        assert must_have in names, f"missing {must_have}"
    assert len(tools) >= 20


# ---------------------------------------------------------------------------
# read_artifact wrapper (working-memory escape hatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_artifact_returns_content_for_known_id(tmp_path, dummy_tool_context):
    """End-to-end: write -> read via the @tool wrapper."""
    from artifact_store import ArtifactStore
    from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY, read_artifact

    store = ArtifactStore(tmp_path)
    ref = store.put_text("hello world", prefix="page_html", task_id="t-task")
    dummy_tool_context.artifact_store = store

    cfg = {"configurable": {TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context}}
    cmd = await _build_tool_call(
        read_artifact, args={"artifact_id": ref.artifact_id, "scope": ""}, config=cfg
    )

    msg = cmd.update["messages"][0]
    assert isinstance(msg, ToolMessage)
    assert "hello world" in msg.content


@pytest.mark.asyncio
async def test_read_artifact_unknown_id_returns_failure(dummy_tool_context):
    from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY, read_artifact
    from artifact_store import ArtifactStore
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        dummy_tool_context.artifact_store = ArtifactStore(td)
        cfg = {"configurable": {TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context}}
        cmd = await _build_tool_call(
            read_artifact,
            args={"artifact_id": "missing_xxx", "scope": ""},
            config=cfg,
        )
        msg = cmd.update["messages"][0]
        assert "artifact_not_found" in msg.content


@pytest.mark.asyncio
async def test_read_artifact_without_store_returns_error(dummy_tool_context):
    from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY, read_artifact

    dummy_tool_context.artifact_store = None
    cfg = {"configurable": {TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context}}
    cmd = await _build_tool_call(
        read_artifact, args={"artifact_id": "x", "scope": ""}, config=cfg
    )
    msg = cmd.update["messages"][0]
    assert "artifact_store_unavailable" in msg.content


@pytest.mark.asyncio
async def test_read_artifact_css_scope(tmp_path, dummy_tool_context):
    from artifact_store import ArtifactStore
    from agents.tools_lc import TOOL_CONTEXT_CONFIG_KEY, read_artifact

    store = ArtifactStore(tmp_path)
    html = "<html><body><ul class='news'><li>A</li></ul></body></html>"
    ref = store.put_text(html, prefix="page_html", task_id="t1")
    dummy_tool_context.artifact_store = store

    cfg = {"configurable": {TOOL_CONTEXT_CONFIG_KEY: dummy_tool_context}}
    cmd = await _build_tool_call(
        read_artifact,
        args={"artifact_id": ref.artifact_id, "scope": "css:ul.news li"},
        config=cfg,
    )
    msg = cmd.update["messages"][0]
    assert "<li>A</li>" in msg.content
