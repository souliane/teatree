"""The :class:`TokenUsage` value object: derived cache/cost props and summing.

Pure value-object math — the price-table-free cache observability the benchmark
cost lane is built on. The Anthropic cache multipliers (uncached 1.00x, 5-min
write 1.25x, read 0.10x) are fixed, so ``effective_billed_input`` mirrors the
API's own billed-input regressor.
"""

import pytest

from teatree.eval.models import TokenUsage


class TestTokenUsageDefaults:
    def test_all_fields_default_to_zero(self) -> None:
        usage = TokenUsage()
        assert (usage.input, usage.cache_creation, usage.cache_read, usage.output) == (0, 0, 0, 0)

    def test_zero_usage_has_zero_derived_props(self) -> None:
        usage = TokenUsage()
        assert usage.total_input == 0
        assert usage.cache_hit_rate == pytest.approx(0.0)
        assert usage.cold_write_tokens == 0
        assert usage.effective_billed_input == pytest.approx(0.0)


class TestDerivedProps:
    def test_total_input_sums_the_three_input_classes(self) -> None:
        usage = TokenUsage(input=100, cache_creation=200, cache_read=700)
        assert usage.total_input == 1000

    def test_cache_hit_rate_is_read_over_total_input(self) -> None:
        usage = TokenUsage(input=100, cache_creation=200, cache_read=700)
        assert usage.cache_hit_rate == pytest.approx(0.7)

    def test_cache_hit_rate_guards_zero_input(self) -> None:
        assert TokenUsage(output=50).cache_hit_rate == pytest.approx(0.0)

    def test_cold_write_tokens_is_cache_creation(self) -> None:
        assert TokenUsage(cache_creation=320).cold_write_tokens == 320

    def test_effective_billed_input_uses_fixed_anthropic_multipliers(self) -> None:
        usage = TokenUsage(input=100, cache_creation=200, cache_read=700)
        expected = 100 * 1.00 + 200 * 1.25 + 700 * 0.10
        assert usage.effective_billed_input == pytest.approx(expected)


class TestAdd:
    def test_add_sums_every_field(self) -> None:
        total = TokenUsage(input=1, cache_creation=2, cache_read=3, output=4) + TokenUsage(
            input=10, cache_creation=20, cache_read=30, output=40
        )
        assert total == TokenUsage(input=11, cache_creation=22, cache_read=33, output=44)

    def test_add_is_a_token_usage(self) -> None:
        total = TokenUsage(input=1) + TokenUsage(input=1)
        assert isinstance(total, TokenUsage)
        assert total.input == 2
