"""Tests for the unified LLM factory in agents/llm.py."""

from __future__ import annotations

import pytest


def test_detect_provider_openai_default():
    from agents.llm import detect_provider

    assert detect_provider("gpt-4o", "https://api.openai.com/v1") == "openai"
    assert detect_provider("qwen-max", "https://dashscope.aliyuncs.com/compatible-mode/v1") == "openai"


def test_detect_provider_gemini():
    from agents.llm import detect_provider

    assert detect_provider("gemini-2.0-flash", None) == "google_genai"
    assert detect_provider("anything", "https://generativelanguage.googleapis.com/v1") == "google_genai"


def test_detect_provider_anthropic():
    from agents.llm import detect_provider

    assert detect_provider("claude-3-5-sonnet", None) == "anthropic"
    assert detect_provider("anything", "https://api.anthropic.com/v1") == "anthropic"


class _Cfg:
    qwen_model = "gpt-4o-mini"
    qwen_base_url = "https://api.openai.com/v1"

    @property
    def qwen_api_key(self):
        return "sk-test"


def test_build_chat_model_openai_constructs_without_network():
    """Model construction must not make any network calls."""
    from agents.llm import build_chat_model

    model = build_chat_model(_Cfg(), temperature=0.5)
    # Any LangChain BaseChatModel has bind_tools; we just confirm the
    # object has the expected interface.
    assert hasattr(model, "invoke")
    assert hasattr(model, "bind_tools")


def test_build_chat_model_claude_construction():
    from agents.llm import build_chat_model

    class ClaudeCfg:
        qwen_model = "claude-3-5-sonnet-20241022"
        qwen_base_url = "https://api.anthropic.com"

        @property
        def qwen_api_key(self):
            return "sk-anthropic-test"

    model = build_chat_model(ClaudeCfg())
    assert hasattr(model, "invoke")


@pytest.mark.skipif(
    False, reason="always try; will raise if langchain-google-genai not installed"
)
def test_build_chat_model_gemini_construction():
    from agents.llm import build_chat_model

    class GeminiCfg:
        qwen_model = "gemini-2.0-flash"
        qwen_base_url = None

        @property
        def qwen_api_key(self):
            return "ai-studio-test"

    model = build_chat_model(GeminiCfg())
    assert hasattr(model, "invoke")
