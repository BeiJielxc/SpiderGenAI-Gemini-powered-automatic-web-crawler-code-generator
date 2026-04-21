"""LangChain ``@tool`` wrappers over the existing PyGen async tool implementations.

Design goals:

* **Zero business-logic rewrite.** Every tool here is a thin adapter that
  forwards to the existing ``pygen.tools`` / ``pygen.high_level_tools``
  async functions. The heavy lifting (browser automation, API inference,
  sandbox execution, etc.) stays untouched.

* **Shared mutable ToolContext.** We keep exactly **one** ``ToolContext``
  instance per graph run, stored in the LangGraph ``RunnableConfig``
  under ``configurable['tool_context']``. All wrappers mutate the same
  context so inter-tool coupling (e.g. ``generate_crawler_code`` reading
  ``ctx.page_html`` set earlier by ``open_page``) keeps working exactly
  like in the legacy ``AgentPlanner`` path.

* **State mirroring.** After each tool call we mirror a curated subset of
  the mutated ``ToolContext`` fields into the ``AgentState`` so downstream
  graph nodes (critic, finalizer, api.py) can read them without touching
  the mutable object.

* **Native ``tool_calls`` protocol.** Each wrapper returns a
  ``langgraph.types.Command`` carrying a ``ToolMessage`` with the injected
  ``tool_call_id`` — this is the standard pattern for tools that want to
  update graph state alongside producing an LLM-visible message.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from typing_extensions import Annotated

try:
    from tools import (
        ToolContext,
        ToolResult,
        tool_analyze_page,
        tool_build_verified_category_mapping,
        tool_critic_validate,
        tool_detect_data_status,
        tool_enhanced_page_analysis,
        tool_generate_crawler_code,
        tool_get_intercepted_apis,
        tool_get_network_requests,
        tool_get_page_html,
        tool_get_page_info,
        tool_get_site_menu_tree,
        tool_install_python_packages,
        tool_invalidate_page_cache,
        tool_open_page,
        tool_probe_navigation,
        tool_run_python_snippet,
        tool_scroll_page,
        tool_smart_date_api_scan,
        tool_take_screenshot,
        tool_validate_code,
        tool_wait_for_network_idle,
    )
    from high_level_tools import (
        tool_capture_api_and_infer_params,
        tool_extract_list_and_pagination,
        tool_probe_detail_page,
        tool_turn_page_and_verify_change,
        tool_verify_selector,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from ..tools import (  # type: ignore
        ToolContext,
        ToolResult,
        tool_analyze_page,
        tool_build_verified_category_mapping,
        tool_critic_validate,
        tool_detect_data_status,
        tool_enhanced_page_analysis,
        tool_generate_crawler_code,
        tool_get_intercepted_apis,
        tool_get_network_requests,
        tool_get_page_html,
        tool_get_page_info,
        tool_get_site_menu_tree,
        tool_install_python_packages,
        tool_invalidate_page_cache,
        tool_open_page,
        tool_probe_navigation,
        tool_run_python_snippet,
        tool_scroll_page,
        tool_smart_date_api_scan,
        tool_take_screenshot,
        tool_validate_code,
        tool_wait_for_network_idle,
    )
    from ..high_level_tools import (  # type: ignore
        tool_capture_api_and_infer_params,
        tool_extract_list_and_pagination,
        tool_probe_detail_page,
        tool_turn_page_and_verify_change,
        tool_verify_selector,
    )


# ---------------------------------------------------------------------------
# Runtime-handle plumbing
# ---------------------------------------------------------------------------


TOOL_CONTEXT_CONFIG_KEY = "tool_context"
CANCEL_CHECK_CONFIG_KEY = "cancel_check"


class ToolContextNotConfigured(RuntimeError):
    """Raised when a LangGraph tool is invoked without a ``ToolContext`` in
    ``config['configurable']``."""


class ToolRunCancelled(RuntimeError):
    """Raised when the user cancelled the task mid tool execution."""


def _get_ctx(config: RunnableConfig) -> ToolContext:
    configurable = (config or {}).get("configurable") or {}
    ctx = configurable.get(TOOL_CONTEXT_CONFIG_KEY)
    if ctx is None:
        raise ToolContextNotConfigured(
            "No tool_context in RunnableConfig['configurable']. "
            "Graph callers must populate it via runner.run_agent()."
        )
    return ctx


# ---------------------------------------------------------------------------
# Observation formatting & state mirroring
# ---------------------------------------------------------------------------


_MAX_OBS_LEN = 3000


def _format_observation_content(tool_name: str, result: ToolResult) -> str:
    """Produce a compact JSON blob the LLM can reason over.

    Mirrors the legacy ``AgentPlanner._format_observation`` schema so the
    model sees the same shape of tool output, ensuring prompt behavior
    stays stable across engines.
    """
    payload: Dict[str, Any] = {
        "action": tool_name,
        "success": result.success,
        "summary": result.summary,
        "error": result.error,
        "error_code": result.error_code,
        "retryable": result.retryable,
        "recoverable": result.recoverable,
        "suggested_next_tools": result.suggested_next_tools,
        "confidence": result.confidence,
    }
    if result.artifacts:
        payload["artifacts"] = result.artifacts

    if result.data is not None:
        try:
            data_text = json.dumps(result.data, ensure_ascii=False, default=str)
        except Exception:
            data_text = str(result.data)
        if len(data_text) > _MAX_OBS_LEN:
            payload["data_preview"] = data_text[:_MAX_OBS_LEN] + "... (truncated)"
        else:
            payload["data"] = result.data

    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _mirror_state_update(
    ctx: ToolContext,
    tool_name: str,
    tool_input: Dict[str, Any],
    result: ToolResult,
) -> Dict[str, Any]:
    """Build the partial ``AgentState`` update to accompany a ToolMessage.

    Only mirrors fields actually consumed by other graph nodes or
    surfaced in the final ``PlannerResult`` — keeps state small and
    JSON-serializable.
    """
    updates: Dict[str, Any] = {
        "generated_code": ctx.generated_code,
        "code_strategy": ctx.code_generation_strategy,
        "verified_mapping": ctx.verified_mapping,
        "verified_selectors": ctx.verified_selectors,
        "menu_tree": ctx.menu_tree,
        "page_info": ctx.page_info,
        "page_html_len": len(ctx.page_html) if ctx.page_html else None,
        "page_structure": ctx.page_structure,
        "network_requests": ctx.network_requests,
        "date_api_result": _serialize_date_api(ctx.date_api_result),
        "enhanced_analysis": _shallow_serializable_dict(ctx.enhanced_analysis),
        "screenshots_count": len(ctx.screenshots),
        "tool_calls_log": [
            {
                "action": tool_name,
                "action_input": tool_input,
                "success": result.success,
                "summary": result.summary,
                "error_code": result.error_code,
                "suggested_next_tools": result.suggested_next_tools,
            }
        ],
    }
    return updates


def _serialize_date_api(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        return dict(value.__dict__)
    except Exception:
        return {"repr": repr(value)}


def _shallow_serializable_dict(raw: Any) -> Dict[str, Any]:
    """Best-effort pass to strip non-serializable entries (keeps debug-friendly)."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(k, str) and k.startswith("_"):
            continue  # skip cache markers like _last_html_sig
        try:
            json.dumps(v, default=str)
            out[k] = v
        except Exception:
            out[k] = {"__unserialized__": type(v).__name__}
    return out


async def _run_wrapper(
    *,
    tool_name: str,
    tool_call_id: str,
    config: RunnableConfig,
    tool_input: Dict[str, Any],
    invoker,
) -> Command:
    """Shared body executed by each ``@tool`` wrapper."""
    ctx = _get_ctx(config)

    # Short-circuit on user cancellation so we don't launch long-running
    # browser / sandbox ops after the task was aborted.
    cancel_check = ((config or {}).get("configurable") or {}).get(CANCEL_CHECK_CONFIG_KEY)
    if callable(cancel_check):
        try:
            if cancel_check():
                return Command(
                    update={
                        "cancelled": True,
                        "messages": [
                            ToolMessage(
                                content=json.dumps(
                                    {"action": tool_name, "success": False,
                                     "summary": "Task cancelled by user",
                                     "error_code": "cancelled"}
                                ),
                                tool_call_id=tool_call_id,
                                name=tool_name,
                            )
                        ],
                    }
                )
        except Exception:
            pass

    try:
        result = await invoker(ctx)
    except TypeError as exc:
        result = ToolResult(
            success=False,
            error=f"Invalid parameters for {tool_name}: {exc}",
            summary=f"Invalid parameters for {tool_name}",
            error_code="invalid_params",
            recoverable=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        import traceback

        result = ToolResult(
            success=False,
            error=f"{tool_name} failed: {exc}\n{traceback.format_exc()}",
            summary=f"{tool_name} raised exception",
            error_code="tool_execution_exception",
            recoverable=True,
        )

    if not isinstance(result, ToolResult):
        result = ToolResult(
            success=False,
            error="Tool returned invalid result type",
            summary="invalid_tool_result",
            error_code="invalid_tool_result",
        )

    updates = _mirror_state_update(ctx, tool_name, tool_input, result)
    updates["messages"] = [
        ToolMessage(
            content=_format_observation_content(tool_name, result),
            tool_call_id=tool_call_id,
            name=tool_name,
        )
    ]
    return Command(update=updates)


# ---------------------------------------------------------------------------
# Atomic tools
# ---------------------------------------------------------------------------


@tool
async def open_page(
    url: str = "",
    wait_until: str = "domcontentloaded",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Open a target URL. Usually the first step. Empty url uses the task URL.
    wait_until: 'domcontentloaded' | 'networkidle'."""
    return await _run_wrapper(
        tool_name="open_page",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"url": url, "wait_until": wait_until},
        invoker=lambda ctx: tool_open_page(ctx, url=url, wait_until=wait_until),
    )


@tool
async def scroll_page(
    times: int = 3,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Scroll the current page `times` times to trigger lazy loading."""
    return await _run_wrapper(
        tool_name="scroll_page",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"times": times},
        invoker=lambda ctx: tool_scroll_page(ctx, times=times),
    )


@tool
async def get_page_info(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Get basic info of the current page (title / URL / etc.)."""
    return await _run_wrapper(
        tool_name="get_page_info",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_get_page_info,
    )


@tool
async def get_page_html(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Capture the full HTML of the current page for later code generation."""
    return await _run_wrapper(
        tool_name="get_page_html",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_get_page_html,
    )


@tool
async def analyze_page(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Analyze the current page's structure, links, and captured API signals."""
    return await _run_wrapper(
        tool_name="analyze_page",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_analyze_page,
    )


@tool
async def take_screenshot(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Take a screenshot of the current page (returns base64)."""
    return await _run_wrapper(
        tool_name="take_screenshot",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_take_screenshot,
    )


@tool
async def get_network_requests(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Return captured XHR/fetch/API requests for the current page."""
    return await _run_wrapper(
        tool_name="get_network_requests",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_get_network_requests,
    )


@tool
async def wait_for_network_idle(
    timeout: float = 5.0,
    idle_time: float = 0.5,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Wait until network becomes idle after interactions."""
    return await _run_wrapper(
        tool_name="wait_for_network_idle",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"timeout": timeout, "idle_time": idle_time},
        invoker=lambda ctx: tool_wait_for_network_idle(
            ctx, timeout=timeout, idle_time=idle_time
        ),
    )


@tool
async def get_intercepted_apis(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Return APIs captured by the browser-level Playwright interceptor."""
    return await _run_wrapper(
        tool_name="get_intercepted_apis",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_get_intercepted_apis,
    )


@tool
async def detect_data_status(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Detect whether the page has data / is empty / is loading / errored."""
    return await _run_wrapper(
        tool_name="detect_data_status",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_detect_data_status,
    )


@tool
async def enhanced_page_analysis(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Run the browser-native enhanced analysis and aggregate rich page signals."""
    return await _run_wrapper(
        tool_name="enhanced_page_analysis",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_enhanced_page_analysis,
    )


# ---------------------------------------------------------------------------
# High-level tools
# ---------------------------------------------------------------------------


@tool
async def extract_list_and_pagination(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Auto-discover the news/data list + CSS selectors + pagination + date hints.
    First exploration step after open_page. No selectors needed."""
    return await _run_wrapper(
        tool_name="extract_list_and_pagination",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_extract_list_and_pagination,
    )


@tool
async def capture_api_and_infer_params(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Sniff XHR/Fetch APIs by interacting with the page and infer which params
    control page/category/date. Use when extract_list_and_pagination finds no list."""
    return await _run_wrapper(
        tool_name="capture_api_and_infer_params",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_capture_api_and_infer_params,
    )


@tool
async def turn_page_and_verify_change(
    next_url: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Navigate to next page and verify content actually changed.
    next_url: Optional direct URL; empty to auto-detect next button."""
    return await _run_wrapper(
        tool_name="turn_page_and_verify_change",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"next_url": next_url},
        invoker=lambda ctx: tool_turn_page_and_verify_change(ctx, next_url=next_url),
    )


@tool
async def probe_detail_page(
    url: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Open one detail/article page in a new tab, scan its content container
    and title element, close the tab. Call before generate_crawler_code.
    url: empty to auto-pick from previous extract_list_and_pagination."""
    return await _run_wrapper(
        tool_name="probe_detail_page",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"url": url},
        invoker=lambda ctx: tool_probe_detail_page(ctx, url=url),
    )


@tool
async def verify_selector(
    selector: str,
    description: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Test a CSS selector against the live current page. Read-only.
    Returns totalMatches, visibleMatches, element previews."""
    return await _run_wrapper(
        tool_name="verify_selector",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"selector": selector, "description": description},
        invoker=lambda ctx: tool_verify_selector(
            ctx, selector=selector, description=description
        ),
    )


@tool
async def build_verified_category_mapping(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Build a verified category mapping from captured API evidence."""
    return await _run_wrapper(
        tool_name="build_verified_category_mapping",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_build_verified_category_mapping,
    )


@tool
async def get_site_menu_tree(
    max_depth: int = 3,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Extract hierarchical site menu tree for multi-category exploration.
    Skip this for single-page tasks."""
    return await _run_wrapper(
        tool_name="get_site_menu_tree",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"max_depth": max_depth},
        invoker=lambda ctx: tool_get_site_menu_tree(ctx, max_depth=max_depth),
    )


@tool
async def probe_navigation(
    paths: Optional[List[str]] = None,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Click selected menu paths to capture API/filter mappings.
    paths: list of valid leaf paths returned by get_site_menu_tree; empty = all leaves."""
    return await _run_wrapper(
        tool_name="probe_navigation",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"paths": paths or []},
        invoker=lambda ctx: tool_probe_navigation(ctx, paths=paths),
    )


@tool
async def smart_date_api_scan(
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """High-level 4-layer date API detector (JS globals -> network -> DOM auto -> screenshot+LLM)."""
    return await _run_wrapper(
        tool_name="smart_date_api_scan",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={},
        invoker=tool_smart_date_api_scan,
    )


# ---------------------------------------------------------------------------
# Code generation & quality gates
# ---------------------------------------------------------------------------


CODEGEN_GRAPH_CONFIG_KEY = "use_codegen_graph"


async def _generate_via_codegen_graph(ctx, strategy: str) -> ToolResult:
    """Generate crawler code by invoking the LangGraph codegen subgraph.

    Falls back to raising if dependencies are missing; the caller will
    catch and translate to a ToolResult error.
    """
    from .codegen_graph import run_codegen

    if not ctx.page_html:
        ctx.page_html = await ctx.browser.get_full_html()
    if not ctx.page_structure:
        ctx.page_structure = await ctx.browser.analyze_page_structure()
    if not ctx.network_requests:
        ctx.network_requests = ctx.browser.get_captured_requests()

    requirements = ctx.extra_requirements or ""
    if strategy:
        requirements += f"\n\n[Agent Strategy]: {strategy}"

    ctx.log(f"[TOOL] Generating crawler code via codegen graph (model={ctx.config.qwen_model})")

    result = await run_codegen(
        llm_agent=ctx.llm,
        pygen_config=ctx.config,
        log_callback=ctx.log,
        page_url=ctx.url,
        page_html=ctx.page_html,
        page_structure=ctx.page_structure,
        network_requests=ctx.network_requests,
        user_requirements=requirements,
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        enhanced_analysis=ctx.enhanced_analysis or {},
        verified_selectors=ctx.verified_selectors,
        attachments=ctx.attachments,
        run_mode=ctx.run_mode,
        task_id=ctx.task_id,
        enable_auto_repair=getattr(ctx.llm, "enable_auto_repair", True),
        max_repair_attempts=int(getattr(ctx.llm, "max_repair_attempts", 2)),
    )

    script_code = result.get("script") or ""
    if result.get("error") or not script_code.strip():
        return ToolResult(
            success=False,
            error=str(result.get("error") or "LLM returned empty code"),
            summary="Code generation via codegen graph failed",
            error_code="empty_code" if not script_code else "code_generation_failed",
            recoverable=True,
            suggested_next_tools=["analyze_page", "get_network_requests", "smart_date_api_scan"],
        )

    ctx.generated_code = script_code
    ctx.code_generation_strategy = strategy

    lines = script_code.count("\n") + 1
    summary = f"Code generated (via graph): {lines} lines, {len(script_code):,} chars"
    ctx.log(f"[TOOL] {summary}")
    return ToolResult(
        success=True,
        data={
            "lines": lines,
            "chars": len(script_code),
            "preview": script_code[:800] + "..." if len(script_code) > 800 else script_code,
            "repair_log": result.get("repair_log") or [],
            "injection_log": result.get("injection_log") or [],
        },
        summary=summary,
    )


@tool
async def generate_crawler_code(
    strategy: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Generate a crawler script from collected context and the selected strategy."""
    configurable = (config or {}).get("configurable") or {}
    use_graph = bool(configurable.get(CODEGEN_GRAPH_CONFIG_KEY, True))

    if use_graph:
        invoker = lambda ctx: _generate_via_codegen_graph(ctx, strategy)
    else:
        invoker = lambda ctx: tool_generate_crawler_code(ctx, strategy=strategy)

    return await _run_wrapper(
        tool_name="generate_crawler_code",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"strategy": strategy, "via": "codegen_graph" if use_graph else "legacy"},
        invoker=invoker,
    )


@tool
async def validate_code(
    code: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Run static validation on the generated crawler code."""
    return await _run_wrapper(
        tool_name="validate_code",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"code_len": len(code) if code else 0},
        invoker=lambda ctx: tool_validate_code(ctx, code=code),
    )


@tool
async def run_python_snippet(
    code: str,
    timeout_sec: int = 120,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Run a Python code snippet inside the persistent sandbox session."""
    return await _run_wrapper(
        tool_name="run_python_snippet",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"code_len": len(code), "timeout_sec": timeout_sec},
        invoker=lambda ctx: tool_run_python_snippet(
            ctx, code=code, timeout_sec=timeout_sec
        ),
    )


@tool
async def install_python_packages(
    packages: List[str],
    timeout_sec: int = 900,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Install Python packages inside the sandbox executor session (policy-gated)."""
    return await _run_wrapper(
        tool_name="install_python_packages",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"packages": packages, "timeout_sec": timeout_sec},
        invoker=lambda ctx: tool_install_python_packages(
            ctx, packages=packages, timeout_sec=timeout_sec
        ),
    )


@tool
async def critic_validate(
    objective: str = "",
    min_items: int = 1,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Run rule-based (and optional LLM-assisted) acceptance validation on the
    already-generated code. Use sparingly; the critic subgraph is usually
    triggered by the ``finish`` signal instead."""
    return await _run_wrapper(
        tool_name="critic_validate",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"objective": objective, "min_items": min_items},
        invoker=lambda ctx: tool_critic_validate(
            ctx, objective=objective, min_items=min_items
        ),
    )


# ---------------------------------------------------------------------------
# Working-memory escape hatch: read_artifact
# ---------------------------------------------------------------------------


async def _tool_read_artifact(
    ctx: ToolContext,
    *,
    artifact_id: str,
    scope: str = "",
) -> ToolResult:
    """Read an artifact previously stored by another tool.

    Implemented in this module (not in ``tools.py``) because ``read_artifact``
    only needs ``ctx.artifact_store`` and has no side effects on the browser
    or sandbox; keeping it close to its registration is clearer.
    """
    store = getattr(ctx, "artifact_store", None)
    if store is None:
        return ToolResult(
            success=False,
            error="No artifact_store configured for this run.",
            summary="artifact_store unavailable",
            error_code="artifact_store_unavailable",
            recoverable=False,
        )

    aid = (artifact_id or "").strip()
    if not aid:
        return ToolResult(
            success=False,
            error="artifact_id is required",
            summary="missing artifact_id",
            error_code="missing_artifact_id",
            recoverable=True,
        )

    read_fn = getattr(store, "read", None)
    try:
        if callable(read_fn):
            content = read_fn(aid, scope=(scope or None))
        else:
            # Backward-compat for stores without scope support
            content = store.read_text(aid)
    except Exception as exc:
        return ToolResult(
            success=False,
            error=f"read_artifact failed: {exc}",
            summary="read_artifact raised",
            error_code="read_artifact_failed",
            recoverable=True,
        )

    if content is None:
        return ToolResult(
            success=False,
            error=f"artifact_id not found: {aid}",
            summary="artifact_not_found",
            error_code="artifact_not_found",
            recoverable=True,
        )

    truncated = len(content) >= 8000
    summary = (
        f"Read {len(content):,} chars from {aid}"
        + (f" (scope={scope})" if scope else "")
        + (" [truncated]" if truncated else "")
    )
    return ToolResult(
        success=True,
        data={
            "artifact_id": aid,
            "scope": scope or None,
            "chars": len(content),
            "content": content,
        },
        summary=summary,
    )


@tool
async def read_artifact(
    artifact_id: str,
    scope: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Read a previously-stored artifact (page HTML, network capture, etc.).

    Use this when the ``summary`` returned with an ``artifact_ref`` lacks a
    field you need, INSTEAD of re-running the original tool.

    scope:
      * '' (empty) -> full content (capped to ~8000 chars)
      * 'head:N'   -> first N characters
      * 'tail:N'   -> last N characters
      * 'css:<selector>'   -> only for HTML; outerHTML of matches
      * 'jsonpath:<path>'  -> only for JSON; e.g. 'jsonpath:api_requests[0].url'
    """
    return await _run_wrapper(
        tool_name="read_artifact",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"artifact_id": artifact_id, "scope": scope},
        invoker=lambda ctx: _tool_read_artifact(ctx, artifact_id=artifact_id, scope=scope),
    )


# ---------------------------------------------------------------------------
# PageCache escape-hatch tool (Module A)
# ---------------------------------------------------------------------------


@tool
async def invalidate_page_cache(
    url: str = "",
    domain: str = "",
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> Command:
    """Drop cached HTML rows so the next page fetch hits the live site.

    Use this when you suspect the cached snapshot for a given URL or
    domain is stale (e.g. you re-opened the page and the structure
    looks different from what `[过往经验]` predicts). Pass ``url``
    for a single page, or ``domain`` to wipe all rows for a host.
    """
    return await _run_wrapper(
        tool_name="invalidate_page_cache",
        tool_call_id=tool_call_id,
        config=config,
        tool_input={"url": url, "domain": domain},
        invoker=lambda ctx: tool_invalidate_page_cache(ctx, url=url, domain=domain),
    )


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


_ALL_TOOLS = [
    open_page,
    scroll_page,
    get_page_info,
    get_page_html,
    analyze_page,
    take_screenshot,
    get_network_requests,
    wait_for_network_idle,
    get_intercepted_apis,
    detect_data_status,
    enhanced_page_analysis,
    extract_list_and_pagination,
    capture_api_and_infer_params,
    turn_page_and_verify_change,
    probe_detail_page,
    verify_selector,
    build_verified_category_mapping,
    get_site_menu_tree,
    probe_navigation,
    smart_date_api_scan,
    generate_crawler_code,
    validate_code,
    run_python_snippet,
    install_python_packages,
    critic_validate,
    read_artifact,
    invalidate_page_cache,
]


# Tools that require a live sandbox/executor. Matches legacy
# ``_has_executor`` availability checks in ``tool_registry.py``.
_SANDBOX_TOOLS = {
    "run_python_snippet",
    "install_python_packages",
}

_CRITIC_TOOLS = {"critic_validate"}


def get_all_tools() -> List:
    """Return the full tool set (for documentation / unit tests)."""
    return list(_ALL_TOOLS)


def select_tools_for_context(
    *,
    has_executor: bool = True,
    has_critic: bool = True,
    run_mode: Optional[str] = None,
) -> List:
    """Return the tool subset that should be bound to the planner LLM for a
    given runtime context.

    * ``has_executor=False`` strips sandbox/executor tools (parity with
      legacy ``availability_check=_has_executor``).
    * ``has_critic=False`` strips critic-tagged tools.
    * ``run_mode`` is currently informational — none of the tools here are
      run-mode-restricted in the legacy registry, but we keep the parameter
      for future specialist agents.
    """
    out: List = []
    for t in _ALL_TOOLS:
        name = getattr(t, "name", "")
        if not has_executor and name in _SANDBOX_TOOLS:
            continue
        if not has_critic and name in _CRITIC_TOOLS:
            continue
        out.append(t)
    return out


__all__ = [
    "TOOL_CONTEXT_CONFIG_KEY",
    "ToolContextNotConfigured",
    "get_all_tools",
    "select_tools_for_context",
    "_ALL_TOOLS",
]
