"""Unified LLM factory — collapses the three hand-rolled provider clients
(OpenAI-compatible / Gemini / Claude) into a single ``BaseChatModel`` via
``langchain.chat_models.init_chat_model``.

Supported providers (auto-detected from model name / base_url):

* ``openai`` (also covers Qwen / DeepSeek / Moonshot on OpenAI-compatible
  endpoints when a ``base_url`` is provided)
* ``google_genai``  — Gemini via ``langchain-google-genai``
* ``anthropic``     — Claude via ``langchain-anthropic``

This module is import-cheap on purpose: it only imports the specific
provider package when that provider is actually requested.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    # Only needed for type hints; keeping the import at module level is fine
    # because langchain-core is a lightweight dep.
    from langchain_core.language_models.chat_models import BaseChatModel
except Exception:  # pragma: no cover - type-only fallback
    BaseChatModel = Any  # type: ignore[assignment,misc]


_OPENAI_COMPAT_HINTS = (
    "dashscope",
    "openai.com",
    "deepseek",
    "moonshot",
    "siliconflow",
    "together",
)


def detect_provider(model: str, base_url: Optional[str]) -> str:
    """Return ``openai`` / ``google_genai`` / ``anthropic`` for the given model.

    Matches the detection rules used by the legacy hand-rolled planner so
    existing ``config.yaml`` files keep working without edits.
    """
    model_lower = (model or "").lower()
    base_lower = (base_url or "").lower()

    if "gemini" in model_lower or "generativelanguage.googleapis.com" in base_lower:
        return "google_genai"
    if "claude" in model_lower or "anthropic.com" in base_lower:
        return "anthropic"
    return "openai"


def build_chat_model(
    config,
    *,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    **extra: Any,
) -> "BaseChatModel":
    """Construct a LangChain chat model from a pygen ``Config`` instance.

    ``config`` is the project's ``pygen.config.Config``; we reuse the
    back-compat ``qwen_*`` property names so nothing else needs to change.
    ``extra`` is forwarded to ``init_chat_model`` for provider-specific
    kwargs.
    """
    from langchain.chat_models import init_chat_model

    model_name = config.qwen_model
    api_key = config.qwen_api_key
    base_url = getattr(config, "qwen_base_url", None)

    provider = detect_provider(model_name, base_url)

    kwargs: dict[str, Any] = {
        "model": model_name,
        "model_provider": provider,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    if provider == "openai":
        # OpenAI-compatible endpoints (Qwen, DeepSeek, Moonshot, ...) need
        # both base_url and api_key. ``init_chat_model`` forwards unknown
        # kwargs to the underlying ChatOpenAI.
        kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # Kimi-K2.5 demands temperature=1.0 in the legacy path; keep that
        # quirk so behavior parity holds.
        if model_name == "kimi-k2.5":
            kwargs["temperature"] = 1.0
    elif provider == "google_genai":
        kwargs["google_api_key"] = api_key
    elif provider == "anthropic":
        kwargs["api_key"] = api_key

    kwargs.update(extra)
    return init_chat_model(**kwargs)


def build_small_model(
    config,
    *,
    alias: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    **extra: Any,
) -> "BaseChatModel":
    """Build an auxiliary chat model (cheap / fast) for in-process tasks
    such as artifact-summary fallback or future routing helpers.

    Reads its parameters from ``llm.<alias>`` directly, bypassing
    ``llm.active`` so the small model is decoupled from whatever the user
    happens to be running as the main planner. ``alias`` defaults to
    ``config.artifacts_small_model_alias`` (typically ``qwen-next``).

    Raises ``ValueError`` if the alias is missing / unconfigured so the
    caller can decide whether to skip fallback gracefully.
    """
    from langchain.chat_models import init_chat_model

    alias = alias or getattr(config, "artifacts_small_model_alias", "qwen-next")
    sub = config.get_llm_alias_config(alias) if hasattr(config, "get_llm_alias_config") else {}
    if not sub:
        raise ValueError(f"small-model alias not found in llm config: {alias!r}")

    api_key = sub.get("api_key", "")
    if not api_key or str(api_key).startswith("YOUR_"):
        raise ValueError(f"small-model alias {alias!r} has no usable api_key")

    model_name = sub.get("model") or alias
    base_url = sub.get("base_url")
    provider = detect_provider(model_name, base_url)

    kwargs: dict[str, Any] = {
        "model": model_name,
        "model_provider": provider,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout

    if provider == "openai":
        kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if model_name == "kimi-k2.5":
            kwargs["temperature"] = 1.0
    elif provider == "google_genai":
        kwargs["google_api_key"] = api_key
    elif provider == "anthropic":
        kwargs["api_key"] = api_key

    kwargs.update(extra)
    return init_chat_model(**kwargs)


__all__ = ["build_chat_model", "build_small_model", "detect_provider"]
