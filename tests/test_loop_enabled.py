"""``loop_enabled_by_name`` — the layer-neutral mini-loop enable primitive.

The shared resolver that lets a lower layer (the review-claim chokepoint in
``teatree.loop``, which must not import ``teatree.loops``) reach the same env
enable verdict the orchestrator applies. It is a DB-free platform leaf: it
resolves the ``T3_LOOPS_DISABLED`` env kill-switch only (the #2359 pre-Django
hard kill-switch), and the DB ``LoopState`` tier is layered ON TOP by the caller.
The legacy ``[loops]`` toml fallback was removed in #2702, so there is no toml
read here at all — only env → default.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from teatree.loop_enabled import loop_enabled_by_name


@contextmanager
def _env(value: str | None) -> Iterator[None]:
    previous = os.environ.get("T3_LOOPS_DISABLED")
    if value is None:
        os.environ.pop("T3_LOOPS_DISABLED", None)
    else:
        os.environ["T3_LOOPS_DISABLED"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("T3_LOOPS_DISABLED", None)
        else:
            os.environ["T3_LOOPS_DISABLED"] = previous


class TestEnvKillSwitch:
    def test_named_in_env_disabled_is_false(self) -> None:
        with _env("review,ship"):
            assert loop_enabled_by_name("review") is False

    def test_all_sentinel_disables_every_loop(self) -> None:
        with _env("ALL"):
            assert loop_enabled_by_name("review") is False

    def test_always_on_loop_ignores_named_env_disable(self) -> None:
        with _env("review"):
            assert loop_enabled_by_name("review", always_on=True) is True

    def test_always_on_loop_ignores_all_sentinel(self) -> None:
        with _env("all"):
            assert loop_enabled_by_name("review", always_on=True) is True


class TestDefaultsEnabled:
    def test_no_env_resolves_enabled(self) -> None:
        with _env(None):
            assert loop_enabled_by_name("review") is True

    def test_empty_env_resolves_enabled(self) -> None:
        with _env("   "):
            assert loop_enabled_by_name("review") is True

    def test_other_named_loop_does_not_disable(self) -> None:
        with _env("ship"):
            assert loop_enabled_by_name("review") is True


@pytest.mark.parametrize("disabled", ["review", "REVIEW", " review , ship "])
def test_case_and_whitespace_tolerant_env(disabled: str) -> None:
    # The named entry matches verbatim after stripping; only the ``all``
    # sentinel is case-insensitive. ``REVIEW`` therefore does NOT disable
    # ``review`` (mirrors LoopsConfig._env_disabled_names).
    with _env(disabled):
        expected_disabled = "review" in {p.strip() for p in disabled.split(",")}
        assert loop_enabled_by_name("review") is (not expected_disabled)
