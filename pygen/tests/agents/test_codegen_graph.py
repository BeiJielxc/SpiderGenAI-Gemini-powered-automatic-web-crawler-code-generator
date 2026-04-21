"""Tests for the codegen subgraph (build_prompt -> generate -> validate -> post_process -> repair)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeValidator:
    def __init__(self, issue_sequence):
        self._issues = list(issue_sequence)

    def validate(self, script, page_structure=None):
        if self._issues:
            return self._issues.pop(0)
        return []


class _FakeIssue:
    def __init__(self, severity="error", code="x", message="y"):
        class _Sev:
            def __init__(self, v):
                self.value = v

        self.severity = _Sev(severity)
        self.code = code
        self.message = message


def _fake_llm_agent(issue_sequence=None, code_sequences=None, context_issue_sequence=None):
    """Build a MagicMock LLMAgent compatible with the codegen subgraph."""
    agent = MagicMock()
    agent._extract_api_info.return_value = "API_INFO"
    agent._summarize_structure.return_value = "STRUCT"
    agent._summarize_enhanced_analysis.return_value = "ENHANCED"
    agent._build_system_prompt.return_value = "SYSTEM_PROMPT"
    agent._build_user_prompt.return_value = "USER_PROMPT"
    agent._build_repair_prompt.return_value = "REPAIR_PROMPT"
    agent.code_validator = _FakeValidator(issue_sequence or [])
    agent._check_context_issues = MagicMock(side_effect=context_issue_sequence or [[]])
    # _extract_code_from_response returns the raw text as-is
    agent._extract_code_from_response.side_effect = lambda x: x
    # _call_llm (sync) is used only for the multimodal path and repair
    call_sequence = iter(code_sequences or [])

    def _call_llm(system_prompt, user_prompt, attachments, temperature):
        try:
            return next(call_sequence)
        except StopIteration:
            return ""

    agent._call_llm = MagicMock(side_effect=_call_llm)
    agent.enable_auto_repair = True
    agent.max_repair_attempts = 2
    return agent


class _FakeChatModel:
    """Stand-in for a LangChain chat model."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def ainvoke(self, messages):
        content = self._responses.pop(0)

        class _R:
            pass

        r = _R()
        r.content = content
        return r


class _FakeConfig:
    qwen_model = "gpt-test"
    qwen_base_url = "https://example.com/v1"

    @property
    def qwen_api_key(self):
        return "k"


@pytest.mark.asyncio
async def test_codegen_happy_path_no_repair(monkeypatch):
    from agents import codegen_graph

    fake_model = _FakeChatModel(["def main():\n    pass\n"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)

    agent = _fake_llm_agent(issue_sequence=[[]], context_issue_sequence=[[]])

    # apply_conditional_post_processing — pass-through
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
    )
    assert result["script"].startswith("def main")
    assert result["error"] is None
    assert result["repair_log"] == []


@pytest.mark.asyncio
async def test_codegen_repairs_after_first_validation_failure(monkeypatch):
    from agents import codegen_graph

    # Initial generate returns broken code, repair returns fixed code.
    fake_model = _FakeChatModel(["broken()"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)

    # validator: first call returns 1 error, 2nd call returns no issues
    agent = _fake_llm_agent(
        issue_sequence=[[_FakeIssue()], []],
        context_issue_sequence=[[], []],
        code_sequences=["fixed()"],
    )
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
        max_repair_attempts=2,
    )
    assert result["script"] == "fixed()"
    assert len(result["repair_log"]) == 1
    assert "re-validating" in result["repair_log"][0]


@pytest.mark.asyncio
async def test_codegen_repair_exhausted(monkeypatch):
    from agents import codegen_graph

    fake_model = _FakeChatModel(["broken()"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)

    # always error -> static validator always returns 1 error
    agent = _fake_llm_agent(
        issue_sequence=[[_FakeIssue()], [_FakeIssue()], [_FakeIssue()]],
        context_issue_sequence=[[], [], []],
        code_sequences=["still_broken_1()", "still_broken_2()"],
    )
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
        max_repair_attempts=2,
    )
    # Script should reflect the last repair attempt (or latest non-empty code)
    assert result["script"] in {"still_broken_1()", "still_broken_2()"}
    assert len(result["repair_log"]) == 2


@pytest.mark.asyncio
async def test_codegen_auto_repair_disabled_returns_first_draft(monkeypatch):
    from agents import codegen_graph

    fake_model = _FakeChatModel(["draft()"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)

    agent = _fake_llm_agent(issue_sequence=[[]], context_issue_sequence=[[]])
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
        enable_auto_repair=False,
    )
    assert result["script"] == "draft()"


@pytest.mark.asyncio
async def test_codegen_passes_verified_selectors_into_user_prompt(monkeypatch):
    """build_prompt_node must:
      1. Render the verified_selectors ledger via render_for_prompt.
      2. Pass the rendered text into LLMAgent._build_user_prompt as
         ``verified_selectors_section`` so it lands at the very top of the
         prompt — which is the entire point of the strict-selector contract.
      3. Fall back to text-prefixing if the (legacy) LLMAgent doesn't accept
         the new kwarg, so older deployments don't break.
    """
    from agents import codegen_graph

    fake_model = _FakeChatModel(["def main():\n    pass\n"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    captured = {}

    def _fake_build_user_prompt(**kwargs):
        captured.update(kwargs)
        return "USER_PROMPT_BODY"

    agent = _fake_llm_agent(issue_sequence=[[]], context_issue_sequence=[[]])
    agent._build_user_prompt = MagicMock(side_effect=_fake_build_user_prompt)

    led = {
        "list": {
            "container": "div.foo",
            "title_link": ".heading a",
        },
        "detail": {"content": ".body"},
        "_provenance": {
            "list.container":  {"source": "verify_selector", "total": 6, "visible": 6},
            "list.title_link": {"source": "verify_selector", "total": 6, "visible": 6},
            "detail.content":  {"source": "probe_detail_page"},
        },
        "_ad_hoc_verifications": [],
    }

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
        verified_selectors=led,
    )
    assert result["error"] is None
    assert "verified_selectors_section" in captured, (
        "build_prompt_node should pass the rendered ledger as kwarg"
    )
    section = captured["verified_selectors_section"]
    assert "div.foo" in section
    assert ".heading a" in section
    assert ".body" in section
    assert "强约束" in section


@pytest.mark.asyncio
async def test_codegen_falls_back_when_user_prompt_lacks_kwarg(monkeypatch):
    """When LLMAgent._build_user_prompt doesn't accept the new kwarg (older
    code paths), build_prompt_node must prepend the section to the returned
    text instead of crashing."""
    from agents import codegen_graph

    fake_model = _FakeChatModel(["def main():\n    pass\n"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    seen_user_prompts = []

    async def _capture_invoke(messages):
        for m in messages:
            seen_user_prompts.append(getattr(m, "content", ""))

        class _R:
            content = "def main():\n    pass\n"

        return _R()

    monkeypatch.setattr(
        codegen_graph,
        "build_chat_model",
        lambda cfg, **kw: type("M", (), {"ainvoke": staticmethod(_capture_invoke)})(),
    )

    def _legacy_build_user_prompt(**kwargs):
        if "verified_selectors_section" in kwargs:
            raise TypeError("legacy: unexpected kwarg verified_selectors_section")
        return "LEGACY_BODY"

    agent = _fake_llm_agent(issue_sequence=[[]], context_issue_sequence=[[]])
    agent._build_user_prompt = MagicMock(side_effect=_legacy_build_user_prompt)

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
        verified_selectors={
            "list": {"container": "div.bar"},
            "detail": {},
            "_provenance": {"list.container": {"source": "verify_selector", "total": 1, "visible": 1}},
            "_ad_hoc_verifications": [],
        },
    )
    assert result["error"] is None
    # The user-side message should now contain BOTH the verified-selector
    # section AND the legacy body, because the fallback prepended the section.
    user_msg = next((m for m in seen_user_prompts if "LEGACY_BODY" in m), None)
    assert user_msg is not None
    assert "div.bar" in user_msg


@pytest.mark.asyncio
async def test_codegen_skips_section_when_no_verified_selectors(monkeypatch):
    """Empty / None ledger: no section must be rendered; the call still
    succeeds with the kwarg present-but-empty (so callers can rely on the
    keyword always being passed)."""
    from agents import codegen_graph

    fake_model = _FakeChatModel(["def main():\n    pass\n"])
    monkeypatch.setattr(codegen_graph, "build_chat_model", lambda cfg, **kw: fake_model)
    monkeypatch.setattr(
        codegen_graph,
        "apply_conditional_post_processing",
        lambda script_code, issues, page_structure: (script_code, []),
    )

    captured = {}

    def _fake_build_user_prompt(**kwargs):
        captured.update(kwargs)
        return "USER_PROMPT_BODY"

    agent = _fake_llm_agent(issue_sequence=[[]], context_issue_sequence=[[]])
    agent._build_user_prompt = MagicMock(side_effect=_fake_build_user_prompt)

    result = await codegen_graph.run_codegen(
        llm_agent=agent,
        pygen_config=_FakeConfig(),
        page_url="https://x.test",
        page_html="<html/>",
        page_structure={},
        network_requests={},
        verified_selectors=None,
    )
    assert result["error"] is None
    assert captured.get("verified_selectors_section") == ""


def test_after_post_process_routes_to_repair_on_error():
    from agents.codegen_graph import _after_post_process

    state = {
        "enable_auto_repair": True,
        "finished": False,
        "repair_attempts": 0,
        "max_repair_attempts": 2,
        "current_issues": [_FakeIssue(severity="error")],
    }
    assert _after_post_process(state) == "repair"


def test_after_post_process_routes_to_done_when_attempts_exhausted():
    from agents.codegen_graph import _after_post_process

    state = {
        "enable_auto_repair": True,
        "finished": False,
        "repair_attempts": 5,
        "max_repair_attempts": 2,
        "current_issues": [_FakeIssue(severity="error")],
    }
    assert _after_post_process(state) == "done"


def test_after_post_process_routes_to_done_on_only_warnings():
    from agents.codegen_graph import _after_post_process

    state = {
        "enable_auto_repair": True,
        "finished": False,
        "repair_attempts": 0,
        "max_repair_attempts": 2,
        "current_issues": [_FakeIssue(severity="warning")],
    }
    assert _after_post_process(state) == "done"
