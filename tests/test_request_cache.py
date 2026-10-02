"""The request-scoped read memo: hit inside a scope, miss outside, cleared on write."""

# test-path: cross-cutting

import pytest
from django.test import TestCase

from teatree.config import AgentHarness, get_effective_settings
from teatree.core.models import ConfigSetting
from teatree.request_cache import cached_per_request, invalidate, request_scope


class RequestScopeMemoTestCase:
    """``cached_per_request`` collapses repeated identical reads to one."""

    @staticmethod
    def _counter():
        calls: list[int] = []

        @cached_per_request
        def read(key: str = "k") -> int:
            calls.append(1)
            return len(calls)

        return read, calls


class TestInsideAScope(RequestScopeMemoTestCase):
    def test_repeated_identical_calls_run_the_body_once(self) -> None:
        read, calls = self._counter()
        with request_scope():
            assert [read(), read(), read()] == [1, 1, 1]
        assert len(calls) == 1

    def test_distinct_arguments_are_distinct_entries(self) -> None:
        read, calls = self._counter()
        with request_scope():
            assert read("a") != read("b")
        assert len(calls) == 2

    def test_a_keyword_and_its_positional_spelling_share_one_entry(self) -> None:
        read, calls = self._counter()
        with request_scope():
            assert read("a") == read(key="a")
        assert len(calls) == 1

    def test_an_unhashable_argument_falls_back_to_running_uncached(self) -> None:
        read, calls = self._counter()
        with request_scope():
            assert read(["unhashable"]) != read(["unhashable"])
        assert len(calls) == 2


class TestOutsideAScope(RequestScopeMemoTestCase):
    def test_every_call_runs_the_body(self) -> None:
        read, calls = self._counter()
        assert [read(), read()] == [1, 2]
        assert len(calls) == 2

    def test_the_scope_does_not_leak_past_its_exit(self) -> None:
        read, calls = self._counter()
        with request_scope():
            read()
        read()
        assert len(calls) == 2

    def test_a_raising_body_still_closes_the_scope(self) -> None:
        read, calls = self._counter()

        def read_then_raise() -> None:
            read()
            raise ZeroDivisionError

        with pytest.raises(ZeroDivisionError), request_scope():
            read_then_raise()
        read()
        assert len(calls) == 2


class TestInvalidation(RequestScopeMemoTestCase):
    def test_invalidate_forces_the_next_read_to_recompute(self) -> None:
        read, calls = self._counter()
        with request_scope():
            assert read() == 1
            invalidate()
            assert read() == 2
        assert len(calls) == 2

    def test_invalidate_outside_a_scope_is_a_no_op(self) -> None:
        invalidate()


class TestARealMemoizedRead(TestCase):
    """The memo applied to a production read — the branch a bare call never enters.

    Every other test here memoizes a synthetic counter. ``get_effective_settings``
    is one of the reads the dashboard actually repeats, and its scope-open
    behaviour (serve the memo, honour ``invalidate``) is what the middleware's
    read-your-own-writes guarantee rests on.
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)

    def test_a_write_inside_the_scope_is_served_stale_until_the_memo_is_dropped(self) -> None:
        with request_scope():
            assert get_effective_settings().agent_harness == AgentHarness.CLAUDE_SDK
            ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
            assert get_effective_settings().agent_harness == AgentHarness.CLAUDE_SDK

            invalidate()
            assert get_effective_settings().agent_harness == AgentHarness.PYDANTIC_AI

    def test_the_same_write_is_visible_immediately_outside_a_scope(self) -> None:
        assert get_effective_settings().agent_harness == AgentHarness.CLAUDE_SDK
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        assert get_effective_settings().agent_harness == AgentHarness.PYDANTIC_AI
