"""The metered suite cap reserves before transport, including judge requests."""

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from teatree.eval.anthropic_api_runner import BudgetedModel
from teatree.eval.cost_observation import ConservativeSuiteBudget


def test_reservation_stops_before_a_second_request() -> None:
    budget = ConservativeSuiteBudget(limit_usd=0.001)
    assert budget.reserve("claude-haiku-4-5", "small", "params", 100)
    assert not budget.reserve("claude-haiku-4-5", "small", "params", 100)
    assert budget.exhausted
    assert budget.reserved_usd <= budget.limit_usd


def test_zero_budget_never_reaches_model() -> None:
    budget = ConservativeSuiteBudget(limit_usd=0.000001)
    agent = Agent(BudgetedModel(TestModel(), budget, "claude-haiku-4-5", 512))

    async def run_agent() -> None:
        await agent.run("hello")

    with pytest.raises(RuntimeError, match="coverage incomplete"):
        asyncio.run(run_agent())
    assert budget.exhausted
    assert budget.reserved_usd == 0


def test_settling_frees_the_unused_reservation_of_each_answered_request() -> None:
    budget = ConservativeSuiteBudget(limit_usd=0.5)
    agent = Agent(BudgetedModel(TestModel(), budget, "claude-haiku-4-5", 64_000))

    async def run_plain_then_streamed() -> None:
        await agent.run("hello")
        async with agent.run_stream("hello") as result:
            await result.get_output()

    asyncio.run(run_plain_then_streamed())
    assert not budget.exhausted
    assert 0 < budget.reserved_usd < 0.01
