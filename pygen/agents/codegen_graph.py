"""Code-generation subgraph — replaces the imperative body of
``LLMAgent.generate_crawler_script`` with an explicit LangGraph pipeline.

Pipeline:

```
build_prompt -> llm_generate -> static_validate -> post_process
                                                       |
                                                       v
                                                   (pass?) -- yes --> END
                                                       |
                                                       no
                                                       v
                                                   llm_repair ---> static_validate
```

Design notes:

* The LLM is invoked through ``agents.llm.build_chat_model`` — the single
  unified entry point — so the 3-provider hand-rolled dispatchers in
  ``LLMAgent._call_llm`` are **no longer used by this code path**.
  (They stay on the legacy engine until the cleanup pass.)

* Prompt-assembly helpers (``_build_system_prompt``, ``_build_user_prompt``,
  ``_extract_api_info``, ``_summarize_*``, ``_extract_code_from_response``,
  ``_check_context_issues``, ``_build_repair_prompt``) stay in ``llm_agent.py``
  — those are pure prompt-engineering utilities, not orchestration.

* The repair step here runs **one round at a time**. The graph edge
  ``llm_repair -> static_validate`` handles the loop, bounded by
  ``max_repair_attempts``. This is what replaces the imperative
  ``for attempt in range(max_repair_attempts)`` loop in
  ``LLMAgent._validate_and_repair``.
"""

from __future__ import annotations

import operator
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from typing_extensions import Annotated

try:
    from post_processor import apply_conditional_post_processing
    from prompts import load as load_prompt
    from verified_selectors import render_for_prompt as render_verified_selectors
except ImportError:  # pragma: no cover
    from ..post_processor import apply_conditional_post_processing  # type: ignore
    from ..prompts import load as load_prompt  # type: ignore
    from ..verified_selectors import render_for_prompt as render_verified_selectors  # type: ignore

from .llm import build_chat_model


# ---------------------------------------------------------------------------
# Sub-state: the codegen subgraph runs with its own TypedDict to keep the
# many intermediate values (prompts, issues, logs) out of AgentState.
# ---------------------------------------------------------------------------


class CodegenState(TypedDict, total=False):
    # ---- inputs ----
    page_url: str
    page_html: str
    page_structure: Dict[str, Any]
    network_requests: Dict[str, List[Dict[str, Any]]]
    user_requirements: str
    start_date: str
    end_date: str
    enhanced_analysis: Dict[str, Any]
    # Structured ledger of selectors verified live by the planner. ``None`` /
    # empty means no probes ran successfully; the build_prompt node will then
    # skip the "MUST USE" section entirely instead of rendering an empty header.
    verified_selectors: Optional[Dict[str, Any]]
    attachments: List[Any]
    run_mode: str
    crawl_mode: str
    task_id: Optional[str]

    # ---- budgets ----
    max_repair_attempts: int
    enable_auto_repair: bool

    # ---- prompt-assembly scratch ----
    system_prompt: str
    user_prompt: str
    api_info: Any
    structure_summary: Any
    enhanced_summary: str

    # ---- llm outputs ----
    script: str
    raw_llm_response: str

    # ---- validation / repair loop ----
    repair_attempts: int
    current_issues: List[Any]
    context_checks: Dict[str, bool]
    # Append across multiple repair rounds instead of overwriting.
    injection_log: Annotated[List[str], operator.add]
    repair_log: Annotated[List[str], operator.add]
    finished: bool
    error: Optional[str]


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


LLM_AGENT_CONFIG_KEY = "llm_agent"
PYGEN_CONFIG_KEY = "pygen_config"
LOG_CALLBACK_CONFIG_KEY = "log_callback"


def _cfg(config: RunnableConfig) -> Dict[str, Any]:
    return (config or {}).get("configurable") or {}


def _get_llm_agent(config: RunnableConfig):
    return _cfg(config).get(LLM_AGENT_CONFIG_KEY)


def _get_pygen_config(config: RunnableConfig):
    return _cfg(config).get(PYGEN_CONFIG_KEY)


def _log(config: RunnableConfig, msg: str) -> None:
    cb = _cfg(config).get(LOG_CALLBACK_CONFIG_KEY)
    if callable(cb):
        try:
            cb(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def build_prompt_node(state: CodegenState, config: RunnableConfig) -> Dict[str, Any]:
    """Assemble system / user prompts using the existing LLMAgent helpers."""
    llm_agent = _get_llm_agent(config)
    if not llm_agent:
        return {"error": "llm_agent not configured in RunnableConfig.configurable"}

    page_structure = state.get("page_structure") or {}
    network_requests = state.get("network_requests") or {}
    enhanced_analysis = state.get("enhanced_analysis") or {}

    api_info = llm_agent._extract_api_info(network_requests, enhanced_analysis)
    structure_summary = llm_agent._summarize_structure(page_structure)
    enhanced_summary = (
        llm_agent._summarize_enhanced_analysis(enhanced_analysis)
        if enhanced_analysis
        else ""
    )

    system_prompt = llm_agent._build_system_prompt(
        run_mode=state.get("run_mode", "enterprise_report"),
        crawl_mode=state.get("crawl_mode", "single_page"),
    )

    verified_selectors = state.get("verified_selectors")
    try:
        verified_selectors_section = render_verified_selectors(verified_selectors)
    except Exception as exc:
        # Bookkeeping bug must never break codegen — log and continue without
        # the strict-selector section.
        _log(config, f"[CODEGEN] verified_selectors render failed: {exc}")
        verified_selectors_section = ""

    user_prompt_kwargs = {
        "page_url": state.get("page_url", ""),
        "page_html": state.get("page_html", ""),
        "structure_summary": structure_summary,
        "api_info": api_info,
        "user_requirements": state.get("user_requirements", ""),
        "start_date": state.get("start_date", ""),
        "end_date": state.get("end_date", ""),
        "enhanced_summary": enhanced_summary,
    }
    # Pass the new section if the LLMAgent supports it; otherwise prepend by
    # hand so older deployments keep working without the kwarg.
    try:
        user_prompt = llm_agent._build_user_prompt(
            verified_selectors_section=verified_selectors_section,
            **user_prompt_kwargs,
        )
    except TypeError:
        user_prompt = llm_agent._build_user_prompt(**user_prompt_kwargs)
        if verified_selectors_section:
            user_prompt = verified_selectors_section + "\n" + user_prompt

    attachments = state.get("attachments") or []
    if attachments:
        try:
            hint = load_prompt("codegen/shared/attachment_hint.md").strip()
            user_prompt = hint + "\n\n" + user_prompt
        except Exception:
            pass

    return {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "api_info": api_info,
        "structure_summary": structure_summary,
        "enhanced_summary": enhanced_summary,
    }


async def llm_generate_node(state: CodegenState, config: RunnableConfig) -> Dict[str, Any]:
    """Call the LLM (through LangChain init_chat_model) to draft the crawler script."""
    pygen_cfg = _get_pygen_config(config)
    llm_agent = _get_llm_agent(config)
    if not pygen_cfg or not llm_agent:
        return {"error": "pygen_config or llm_agent missing in configurable"}

    system_prompt = state.get("system_prompt", "")
    user_prompt = state.get("user_prompt", "")
    attachments = state.get("attachments") or []

    # Attachments (multimodal) still use LLMAgent._call_llm because the
    # legacy path has careful base64/multimodal packing; LangChain's chat
    # model API takes a different shape. Falling back to LLMAgent for this
    # is safe and keeps behavior identical. For text-only prompts we use
    # init_chat_model() directly.
    if attachments:
        import asyncio

        try:
            response = await asyncio.to_thread(
                llm_agent._call_llm,
                system_prompt,
                user_prompt,
                attachments,
                0.2,
            )
            script = llm_agent._extract_code_from_response(response)
            return {"raw_llm_response": response, "script": script}
        except Exception as exc:
            _log(config, f"[CODEGEN] llm_generate (multimodal) failed: {exc}")
            return {"error": f"llm_call_failed: {exc}"}

    try:
        chat_model = build_chat_model(pygen_cfg, temperature=0.2)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        ai_msg = await chat_model.ainvoke(messages)
        response = ai_msg.content if hasattr(ai_msg, "content") else str(ai_msg)
        if isinstance(response, list):
            # Claude / Anthropic can return list of parts
            response = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in response
            )
        script = llm_agent._extract_code_from_response(response)
        return {"raw_llm_response": response, "script": script}
    except Exception as exc:
        _log(config, f"[CODEGEN] llm_generate failed: {exc}")
        return {"error": f"llm_call_failed: {exc}"}


def static_validate_node(state: CodegenState, config: RunnableConfig) -> Dict[str, Any]:
    """Run the static validator + context-aware heuristics."""
    llm_agent = _get_llm_agent(config)
    if not llm_agent:
        return {"error": "llm_agent not configured"}

    script = state.get("script", "")
    page_structure = state.get("page_structure") or {}
    issues = llm_agent.code_validator.validate(script, page_structure=page_structure)

    network_requests = state.get("network_requests") or {}
    api_requests = network_requests.get("api_requests") or []
    page_spa = (page_structure or {}).get("spaHints") or {}
    context_checks = {
        "needs_rendered_dom_dates": bool(
            (page_structure or {}).get("dateItemSamples")
            or page_spa.get("hasHashRoute")
            or page_spa.get("hasAppRoot")
        ),
        "has_api_requests": bool(api_requests),
        "needs_news_attachments": bool(
            state.get("run_mode") == "news_sentiment"
            and any(
                probe.get("attachmentCandidates")
                for probe in ((state.get("enhanced_analysis") or {}).get("detail_probes") or [])
                if isinstance(probe, dict)
            )
        ),
    }
    context_issues = llm_agent._check_context_issues(script, context_checks)

    return {
        "current_issues": list(issues) + list(context_issues),
        "context_checks": context_checks,
    }


def post_process_node(state: CodegenState, config: RunnableConfig) -> Dict[str, Any]:
    """Apply conditional post-processing injections based on detected issues."""
    script = state.get("script", "")
    issues = state.get("current_issues") or []
    page_structure = state.get("page_structure") or {}
    try:
        new_script, injection_log = apply_conditional_post_processing(
            script_code=script, issues=issues, page_structure=page_structure
        )
    except Exception as exc:
        _log(config, f"[CODEGEN] post_process failed: {exc}")
        return {"error": f"post_process_failed: {exc}"}

    if injection_log:
        _log(config, f"[CODEGEN] post_process injections: {len(injection_log)}")
    return {"script": new_script, "injection_log": injection_log or []}


async def llm_repair_node(state: CodegenState, config: RunnableConfig) -> Dict[str, Any]:
    """Run one repair round: ask the LLM to fix the current validation errors."""
    llm_agent = _get_llm_agent(config)
    pygen_cfg = _get_pygen_config(config)
    if not llm_agent or not pygen_cfg:
        return {"finished": True, "error": "llm_agent or pygen_config missing"}

    attempts = int(state.get("repair_attempts", 0)) + 1
    script = state.get("script", "")
    issues = state.get("current_issues") or []
    page_structure = state.get("page_structure") or {}
    system_prompt = state.get("system_prompt", "")
    user_prompt = state.get("user_prompt", "")

    repair_prompt = llm_agent._build_repair_prompt(issues, script, page_structure)

    import asyncio

    try:
        response = await asyncio.to_thread(
            llm_agent._call_llm,
            system_prompt,
            user_prompt + "\n\n" + repair_prompt,
            None,
            0.1,
        )
        new_script = llm_agent._extract_code_from_response(response)
    except Exception as exc:
        _log(config, f"[CODEGEN] repair round {attempts} failed: {exc}")
        return {
            "repair_attempts": attempts,
            "repair_log": [f"Round {attempts}: repair call failed: {exc}"],
            "finished": True,  # stop looping on transport error
        }

    log_line = f"Round {attempts}: "
    if new_script and new_script.strip() and new_script != script:
        log_line += "LLM returned a new script; re-validating."
        _log(config, f"[CODEGEN] {log_line}")
        return {
            "script": new_script,
            "repair_attempts": attempts,
            "repair_log": [log_line],
            "finished": False,
        }

    log_line += "LLM repair produced no change; stopping."
    _log(config, f"[CODEGEN] {log_line}")
    return {
        "repair_attempts": attempts,
        "repair_log": [log_line],
        "finished": True,
    }


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def _after_post_process(state: CodegenState) -> str:
    """Decide whether to repair or finish after the validate+post_process stage."""
    if not state.get("enable_auto_repair", True):
        return "done"
    if state.get("finished"):
        return "done"
    attempts = int(state.get("repair_attempts", 0))
    max_attempts = int(state.get("max_repair_attempts", 2))
    if attempts >= max_attempts:
        return "done"
    issues = state.get("current_issues") or []
    # IssueSeverity is an enum with .value; dataclasses also expose .severity attribute
    has_errors = False
    for i in issues:
        sev = getattr(getattr(i, "severity", None), "value", None) or getattr(i, "severity", None)
        if sev == "error":
            has_errors = True
            break
    return "repair" if has_errors else "done"


def _after_generate(state: CodegenState) -> str:
    """If generation itself failed we shortcut to END with the error preserved."""
    if state.get("error"):
        return "done"
    if not state.get("enable_auto_repair", True):
        return "done"
    return "validate"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_codegen_graph():
    """Compile the codegen subgraph.

    Required ``RunnableConfig['configurable']`` keys:

    * ``llm_agent`` — a pygen ``LLMAgent`` instance (used for prompt
      assembly, code extraction, static validator, and legacy multimodal
      calls). Will be slimmed in a later cleanup pass but remains
      functional today.
    * ``pygen_config`` — a ``pygen.config.Config`` instance (used by
      ``build_chat_model`` to pick provider + api key + base url).
    * ``log_callback`` *(optional)* — ``Callable[[str], None]`` forwarding
      to the task log.
    """
    g = StateGraph(CodegenState)
    g.add_node("build_prompt", build_prompt_node)
    g.add_node("llm_generate", llm_generate_node)
    g.add_node("static_validate", static_validate_node)
    g.add_node("post_process", post_process_node)
    g.add_node("llm_repair", llm_repair_node)

    g.set_entry_point("build_prompt")
    g.add_edge("build_prompt", "llm_generate")
    g.add_conditional_edges(
        "llm_generate",
        _after_generate,
        {"validate": "static_validate", "done": END},
    )
    g.add_edge("static_validate", "post_process")
    g.add_conditional_edges(
        "post_process",
        _after_post_process,
        {"repair": "llm_repair", "done": END},
    )
    # After a repair round, re-run static_validate (post_process on the
    # repaired code is usually idempotent, but we skip it on the repair
    # side to keep the loop cheap).
    g.add_edge("llm_repair", "static_validate")

    return g.compile()


# ---------------------------------------------------------------------------
# Convenience: async invoke returning just the final script string.
# ---------------------------------------------------------------------------


async def run_codegen(
    *,
    llm_agent,
    pygen_config,
    log_callback=None,
    page_url: str,
    page_html: str,
    page_structure: Dict[str, Any],
    network_requests: Dict[str, List[Dict[str, Any]]],
    user_requirements: str = "",
    start_date: str = "",
    end_date: str = "",
    enhanced_analysis: Optional[Dict[str, Any]] = None,
    verified_selectors: Optional[Dict[str, Any]] = None,
    attachments: Optional[List[Any]] = None,
    run_mode: str = "enterprise_report",
    crawl_mode: str = "single_page",
    task_id: Optional[str] = None,
    enable_auto_repair: bool = True,
    max_repair_attempts: int = 2,
) -> Dict[str, Any]:
    """Run the codegen subgraph and return a dict with the final script and logs.

    Returned keys: ``script``, ``repair_log``, ``injection_log``, ``error``.
    """
    graph = build_codegen_graph()
    initial: CodegenState = CodegenState(
        page_url=page_url,
        page_html=page_html,
        page_structure=page_structure or {},
        network_requests=network_requests or {},
        user_requirements=user_requirements or "",
        start_date=start_date,
        end_date=end_date,
        enhanced_analysis=enhanced_analysis or {},
        verified_selectors=verified_selectors,
        attachments=list(attachments or []),
        run_mode=run_mode,
        crawl_mode=crawl_mode,
        task_id=task_id,
        max_repair_attempts=max(0, int(max_repair_attempts)),
        enable_auto_repair=bool(enable_auto_repair),
        script="",
        raw_llm_response="",
        repair_attempts=0,
        current_issues=[],
        context_checks={},
        injection_log=[],
        repair_log=[],
        finished=False,
        error=None,
    )
    config: RunnableConfig = {
        "configurable": {
            LLM_AGENT_CONFIG_KEY: llm_agent,
            PYGEN_CONFIG_KEY: pygen_config,
            LOG_CALLBACK_CONFIG_KEY: log_callback,
        }
    }
    final = await graph.ainvoke(initial, config=config)
    return {
        "script": final.get("script", ""),
        "repair_log": final.get("repair_log", []) or [],
        "injection_log": final.get("injection_log", []) or [],
        "error": final.get("error"),
    }


__all__ = [
    "CodegenState",
    "build_codegen_graph",
    "run_codegen",
]
