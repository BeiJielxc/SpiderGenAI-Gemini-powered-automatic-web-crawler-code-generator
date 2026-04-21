"""Tests for the critic subgraph routing and node wiring.

We stub ``Critic._run_rule_round`` and ``Critic._llm_repair_once`` so the
graph exercises all edge cases deterministically without touching the
sandbox or the LLM.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest


def _fresh_state(generated_code="print('x')", rounds=0):
    from agents.state import initial_state

    s = initial_state(
        url="https://example.com",
        run_mode="enterprise_report",
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    s["generated_code"] = generated_code
    s["critic_rounds"] = rounds
    return s


@pytest.mark.asyncio
async def test_critic_passes_round_1():
    from agents.critic_graph import build_critic_graph, CRITIC_CONFIG_KEY

    critic = AsyncMock()
    critic._run_rule_round.return_value = {
        "passed": True,
        "confidence": 0.9,
        "primary_cause": "none",
        "backup_cause": "none",
        "uncertain": False,
        "issues": [],
        "recommendations": [],
        "summary_payload": {"round": 1, "passed": True, "primary_cause": "none",
                            "backup_cause": "none", "classifier_confidence": 0.9,
                            "record_count": 5, "static_error_count": 0,
                            "uncertain": False},
        "evidence": [],
        "runtime_result": {},
    }

    graph = build_critic_graph()
    state = _fresh_state()
    config = {"configurable": {CRITIC_CONFIG_KEY: critic}}

    final = await graph.ainvoke(state, config=config)

    assert final["critic_verdict"]["passed"] is True
    assert final["critic_rounds"] == 1
    # repair should never have been called
    critic._llm_repair_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_critic_repairs_then_passes_on_round_2():
    from agents.critic_graph import build_critic_graph, CRITIC_CONFIG_KEY

    critic = AsyncMock()
    # Round 1 -> fail, Round 2 -> pass (after repair produces new code)
    round_outputs = [
        {  # round 1
            "passed": False, "confidence": 0.4,
            "primary_cause": "missing_output", "backup_cause": "selector_brittleness",
            "uncertain": False, "issues": [], "recommendations": ["add json dump"],
            "summary_payload": {"round": 1, "passed": False,
                                "primary_cause": "missing_output",
                                "backup_cause": "selector_brittleness",
                                "classifier_confidence": 0.4,
                                "record_count": 0, "static_error_count": 1,
                                "uncertain": False},
            "evidence": [], "runtime_result": {},
        },
        {  # round 2
            "passed": True, "confidence": 0.85,
            "primary_cause": "none", "backup_cause": "none",
            "uncertain": False, "issues": [], "recommendations": [],
            "summary_payload": {"round": 2, "passed": True, "primary_cause": "none",
                                "backup_cause": "none", "classifier_confidence": 0.85,
                                "record_count": 5, "static_error_count": 0,
                                "uncertain": False},
            "evidence": [], "runtime_result": {},
        },
    ]

    async def fake_round(**kwargs):
        return round_outputs.pop(0)

    critic._run_rule_round.side_effect = fake_round
    critic._llm_repair_once = AsyncMock(return_value="print('fixed')")
    critic.llm_agent = object()  # truthy so repair runs

    graph = build_critic_graph()
    config = {"configurable": {CRITIC_CONFIG_KEY: critic}}

    final = await graph.ainvoke(_fresh_state(), config=config)

    assert final["critic_verdict"]["passed"] is True
    assert final["critic_rounds"] == 2
    assert final["generated_code"] == "print('fixed')"
    assert final["critic_repaired_code"] == "print('fixed')"


@pytest.mark.asyncio
async def test_critic_exhausts_rounds_and_stops():
    from agents.critic_graph import build_critic_graph, CRITIC_CONFIG_KEY, MAX_CRITIC_ROUNDS

    critic = AsyncMock()

    def _fail_payload(idx):
        return {
            "passed": False, "confidence": 0.3,
            "primary_cause": "network_error", "backup_cause": "unknown",
            "uncertain": False, "issues": [], "recommendations": [],
            "summary_payload": {"round": idx, "passed": False,
                                "primary_cause": "network_error",
                                "backup_cause": "unknown",
                                "classifier_confidence": 0.3,
                                "record_count": 0, "static_error_count": 0,
                                "uncertain": False},
            "evidence": [], "runtime_result": {},
        }

    calls = iter(_fail_payload(i) for i in range(1, MAX_CRITIC_ROUNDS + 1))

    async def fake_round(**kwargs):
        return next(calls)

    critic._run_rule_round.side_effect = fake_round
    critic._llm_repair_once = AsyncMock(return_value="print('still broken')")
    critic.llm_agent = object()

    graph = build_critic_graph()
    config = {"configurable": {CRITIC_CONFIG_KEY: critic}}

    final = await graph.ainvoke(_fresh_state(), config=config)

    assert final["critic_verdict"]["passed"] is False
    assert final["critic_rounds"] == MAX_CRITIC_ROUNDS
    # Repair is called MAX-1 times (after each failing round except the last one which is terminal)
    assert critic._llm_repair_once.await_count == MAX_CRITIC_ROUNDS - 1


@pytest.mark.asyncio
async def test_empty_code_short_circuits_immediately():
    from agents.critic_graph import build_critic_graph, CRITIC_CONFIG_KEY

    critic = AsyncMock()
    graph = build_critic_graph()
    config = {"configurable": {CRITIC_CONFIG_KEY: critic}}

    state = _fresh_state(generated_code="")
    final = await graph.ainvoke(state, config=config)

    assert final["critic_verdict"]["passed"] is False
    assert final["critic_verdict"]["details"]["stopped_reason"] == "empty_code"
    critic._run_rule_round.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_critic_instance_gives_clean_failure():
    from agents.critic_graph import build_critic_graph

    graph = build_critic_graph()
    # No critic in configurable
    final = await graph.ainvoke(_fresh_state(), config={"configurable": {}})

    assert final["critic_verdict"]["passed"] is False
    summary = final["critic_verdict"]["summary"].lower()
    assert "critic" in summary and "not" in summary
