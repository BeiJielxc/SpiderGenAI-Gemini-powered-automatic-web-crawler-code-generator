"""Tests for the small-model LLM fallback summarizer.

These tests use a stub chat model so no network call is ever made. They
cover:
* :func:`is_weak_summary` for HTML / JSON / analyze payloads
* :func:`enrich_summary_via_llm` happy path: rule signals are preserved,
  LLM fills only the empty buckets, provenance is tagged
* Failure modes: model build error, invoke timeout, non-JSON response,
  empty content, no model_factory — all return rule_summary untouched
* Whitelist enforcement: rogue LLM keys are dropped silently
* The ``_wrap_summarizer_with_llm_fallback`` integration in
  ``pygen.tools`` (gated by config; rule-only by default)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

import pytest  # noqa: E402

from summarizers.llm_fallback import (  # noqa: E402
    enrich_summary_via_llm,
    is_weak_summary,
)


# ---------------------------------------------------------------------------
# Stub chat model (mimics LangChain's BaseChatModel.invoke contract)
# ---------------------------------------------------------------------------


class _StubResp:
    def __init__(self, content):
        self.content = content


class _StubModel:
    """Returns a fixed string for every .invoke() call."""

    def __init__(self, payload, raise_on_invoke=False):
        self._payload = payload
        self._raise = raise_on_invoke
        self.last_messages = None

    def bind(self, **_kwargs):
        return self

    def invoke(self, messages):
        self.last_messages = messages
        if self._raise:
            raise RuntimeError("simulated network error")
        return _StubResp(self._payload)


def _factory_for(model):
    return lambda: model


# ---------------------------------------------------------------------------
# is_weak_summary
# ---------------------------------------------------------------------------


def test_is_weak_summary_empty_dict_is_weak():
    assert is_weak_summary({}, kind="html") is True


def test_is_weak_summary_error_marker_is_weak():
    assert is_weak_summary({"_summary_error": "boom"}, kind="html") is True


def test_is_weak_summary_html_with_list_candidates_is_strong():
    s = {"list_candidates": [{"selector": "ul.foo", "count": 3}]}
    assert is_weak_summary(s, kind="html") is False


def test_is_weak_summary_html_with_only_meta_is_strong():
    s = {"meta": {"title": "x"}, "list_candidates": []}
    assert is_weak_summary(s, kind="html") is False


def test_is_weak_summary_json_needs_endpoints_or_hosts():
    assert is_weak_summary({"top_hosts": ["a.com"]}, kind="json") is False
    assert is_weak_summary({"candidate_endpoints": []}, kind="json") is True


def test_is_weak_summary_analyze_aggregates_signals():
    assert is_weak_summary({"date_signals": [{"value": "2024-01-01"}]}, kind="analyze") is False
    assert is_weak_summary({"meta": {"title": "x"}}, kind="analyze") is True


# ---------------------------------------------------------------------------
# enrich_summary_via_llm
# ---------------------------------------------------------------------------


def test_enrich_no_factory_marks_skipped():
    out = enrich_summary_via_llm("<html></html>", {"meta": {}}, kind="html", model_factory=None)
    assert out["_fallback_skipped"] == "no model_factory"
    assert out["meta"] == {}


def test_enrich_empty_content_marks_skipped():
    model = _StubModel("{}")
    out = enrich_summary_via_llm("", {}, kind="html", model_factory=_factory_for(model))
    assert out["_fallback_skipped"] == "empty content"
    assert model.last_messages is None


def test_enrich_model_build_failure_returns_rule_summary():
    def bad_factory():
        raise RuntimeError("missing api key")

    out = enrich_summary_via_llm(
        "<html><body>x</body></html>",
        {"meta": {"title": "old"}},
        kind="html",
        model_factory=bad_factory,
    )
    assert out["meta"] == {"title": "old"}
    assert out["_fallback_skipped"].startswith("model build failed:")


def test_enrich_invoke_failure_returns_rule_summary():
    model = _StubModel("", raise_on_invoke=True)
    out = enrich_summary_via_llm(
        "<html><body>x</body></html>",
        {"meta": {}},
        kind="html",
        model_factory=_factory_for(model),
    )
    assert out["_fallback_skipped"].startswith("invoke failed:")
    assert "_summary_provenance" not in out


def test_enrich_non_json_response_skipped():
    model = _StubModel("Sorry, I cannot help with that.")
    out = enrich_summary_via_llm(
        "<html>x</html>",
        {},
        kind="html",
        model_factory=_factory_for(model),
    )
    assert out["_fallback_skipped"] == "non-json response"


def test_enrich_happy_path_html_fills_empty_keys():
    payload = (
        '{"list_candidates": [],'
        ' "pagination": {"type": "next", "hint": "?page=2"},'
        ' "date_signals": [{"value": "2024-01-01", "format": "YYYY-MM-DD"}],'
        ' "notes": "looks like a news index"}'
    )
    model = _StubModel(payload)
    rule_summary = {"meta": {"title": "News"}, "list_candidates": []}
    out = enrich_summary_via_llm(
        "<html><body>news list here</body></html>",
        rule_summary,
        kind="html",
        model_factory=_factory_for(model),
    )

    assert out["meta"] == {"title": "News"}
    assert out["pagination"] == {"type": "next", "hint": "?page=2"}
    assert out["date_signals"][0]["value"] == "2024-01-01"
    assert out["notes"].startswith("looks like a news")
    assert out["_summary_provenance"] == "rule+llm"
    assert "pagination" in out["_llm_fallback_used"]


def test_enrich_does_not_overwrite_existing_non_empty_rule_keys():
    payload = '{"pagination": {"type": "load_more", "hint": "FROM LLM"}}'
    rule_summary = {"pagination": {"type": "numeric", "hint": "FROM RULE"}}
    out = enrich_summary_via_llm(
        "<html>x</html>",
        rule_summary,
        kind="html",
        model_factory=_factory_for(_StubModel(payload)),
    )
    assert out["pagination"] == {"type": "numeric", "hint": "FROM RULE"}


def test_enrich_drops_keys_outside_whitelist():
    payload = '{"system_prompt_override": "ignore previous", "notes": "ok"}'
    out = enrich_summary_via_llm(
        "<html>x</html>",
        {},
        kind="html",
        model_factory=_factory_for(_StubModel(payload)),
    )
    assert "system_prompt_override" not in out
    assert out["notes"] == "ok"
    assert out["_summary_provenance"] == "rule+llm"


def test_enrich_strips_markdown_fences_around_json():
    payload = "```json\n{\"notes\": \"wrapped\"}\n```"
    out = enrich_summary_via_llm(
        "<html>x</html>",
        {},
        kind="html",
        model_factory=_factory_for(_StubModel(payload)),
    )
    assert out["notes"] == "wrapped"


def test_enrich_handles_list_content_response():
    """Some providers return content as a list of parts."""

    class _PartsResp:
        content = [{"text": '{"notes": "via parts"}'}]

    class _PartsModel:
        def bind(self, **_):
            return self

        def invoke(self, _msgs):
            return _PartsResp()

    out = enrich_summary_via_llm(
        "<html>x</html>",
        {},
        kind="html",
        model_factory=lambda: _PartsModel(),
    )
    assert out["notes"] == "via parts"


def test_enrich_truncates_long_content():
    big = "<html>" + ("a" * 50_000) + "</html>"
    captured = {}

    class _Spy:
        def bind(self, **_):
            return self

        def invoke(self, messages):
            captured["user"] = messages[-1]["content"]
            return _StubResp("{}")

    enrich_summary_via_llm(
        big,
        {},
        kind="html",
        model_factory=lambda: _Spy(),
        raw_excerpt_chars=2000,
    )
    user = captured["user"]
    assert "[truncated]" in user
    # Sanity: the full 50k payload definitely wasn't passed through.
    assert len(user) < 5000


# ---------------------------------------------------------------------------
# Wrapper integration (pygen.tools._wrap_summarizer_with_llm_fallback)
# ---------------------------------------------------------------------------


def _make_ctx(*, enabled: bool, log=None):
    cfg = SimpleNamespace(
        artifacts_summary_threshold=10,
        artifacts_enable_summary=True,
        artifacts_small_model_enabled=enabled,
        artifacts_small_model_alias="qwen-next",
        artifacts_small_model_max_tokens=400,
        artifacts_small_model_timeout_sec=5,
        artifacts_small_model_raw_excerpt_chars=1000,
    )
    return SimpleNamespace(
        config=cfg,
        log=log or (lambda _msg: None),
        artifact_store=None,
        task_id=None,
    )


def test_wrapper_returns_rule_summarizer_when_disabled():
    from tools import _wrap_summarizer_with_llm_fallback

    ctx = _make_ctx(enabled=False)
    rule = lambda payload: {"list_candidates": [1]}  # noqa: E731
    wrapped = _wrap_summarizer_with_llm_fallback(ctx, rule, kind="html")
    # When disabled, it must be the *exact* same callable object — proving
    # we don't pay any wrapping overhead in the default config.
    assert wrapped is rule


def test_wrapper_skips_llm_when_rule_summary_is_strong(monkeypatch):
    """Strong rule output should never trigger build_small_model."""
    import tools as tools_mod

    ctx = _make_ctx(enabled=True)

    called = {"build": 0}

    def fake_build(*_a, **_kw):
        called["build"] += 1
        raise AssertionError("should not be called")

    monkeypatch.setattr("agents.llm.build_small_model", fake_build, raising=False)

    rule = lambda payload: {"list_candidates": [{"selector": "ul"}]}  # noqa: E731
    wrapped = tools_mod._wrap_summarizer_with_llm_fallback(ctx, rule, kind="html")

    out = wrapped({"html": "<html><ul><li>x</li></ul></html>"})
    assert out["_summary_provenance"] == "rule"
    assert called["build"] == 0


def test_wrapper_invokes_llm_when_rule_summary_is_weak(monkeypatch):
    import tools as tools_mod

    logs = []
    ctx = _make_ctx(enabled=True, log=logs.append)

    stub = _StubModel('{"notes": "from-llm"}')
    monkeypatch.setattr(
        "agents.llm.build_small_model",
        lambda *_a, **_kw: stub,
        raising=False,
    )

    rule = lambda payload: {}  # noqa: E731 -- forces "weak"
    wrapped = tools_mod._wrap_summarizer_with_llm_fallback(ctx, rule, kind="html")

    out = wrapped({"html": "<html><body>x</body></html>"})
    assert out["notes"] == "from-llm"
    assert out["_summary_provenance"] == "rule+llm"


def test_wrapper_swallows_llm_errors(monkeypatch):
    import tools as tools_mod

    ctx = _make_ctx(enabled=True)

    def bad_build(*_a, **_kw):
        raise ValueError("alias not configured")

    monkeypatch.setattr("agents.llm.build_small_model", bad_build, raising=False)

    rule = lambda payload: {}  # weak  # noqa: E731
    wrapped = tools_mod._wrap_summarizer_with_llm_fallback(ctx, rule, kind="html")
    out = wrapped({"html": "<html>x</html>"})
    # Rule summary is empty, but the wrapper must NOT raise.
    assert "_summary_error" not in out  # rule didn't error
    assert out.get("_fallback_skipped", "").startswith("model build failed:")


# ---------------------------------------------------------------------------
# build_small_model factory
# ---------------------------------------------------------------------------


def test_build_small_model_raises_for_missing_alias():
    from agents.llm import build_small_model

    cfg = SimpleNamespace(
        artifacts_small_model_alias="does-not-exist",
        get_llm_alias_config=lambda alias: {},
    )
    with pytest.raises(ValueError, match="not found"):
        build_small_model(cfg)


def test_build_small_model_raises_for_placeholder_api_key():
    from agents.llm import build_small_model

    cfg = SimpleNamespace(
        artifacts_small_model_alias="ghost",
        get_llm_alias_config=lambda alias: {
            "name": alias,
            "api_key": "YOUR_KEY",
            "model": "x",
            "base_url": "https://example.com/v1",
        },
    )
    with pytest.raises(ValueError, match="api_key"):
        build_small_model(cfg)
