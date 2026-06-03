"""``loop_enabled_by_name`` — the layer-neutral mini-loop enable primitive.

The shared resolver that lets a lower layer (the review-claim chokepoint in
``teatree.loop``, which must not import ``teatree.loops``) reach the same
env → per-loop → global enable verdict the orchestrator applies. Resolution
order and fail-safe-to-enabled behaviour are pinned here.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestEnvKillSwitch:
    def test_named_in_env_disabled_is_false(self, tmp_path: Path) -> None:
        with _env("review,ship"):
            assert loop_enabled_by_name("review", path=tmp_path / "missing.toml") is False

    def test_all_sentinel_disables_every_loop(self, tmp_path: Path) -> None:
        with _env("ALL"):
            assert loop_enabled_by_name("review", path=tmp_path / "missing.toml") is False

    def test_always_on_loop_ignores_named_env_disable(self, tmp_path: Path) -> None:
        with _env("review"):
            assert loop_enabled_by_name("review", always_on=True, path=tmp_path / "missing.toml") is True


class TestTomlLayers:
    def test_per_loop_override_wins(self, tmp_path: Path) -> None:
        toml = _write(tmp_path / "t.toml", "[loops]\nenabled = true\n[loops.review]\nenabled = false\n")
        with _env(None):
            assert loop_enabled_by_name("review", path=toml) is False

    def test_global_disabled_applies_without_per_loop(self, tmp_path: Path) -> None:
        toml = _write(tmp_path / "t.toml", "[loops]\nenabled = false\n")
        with _env(None):
            assert loop_enabled_by_name("review", path=toml) is False

    def test_default_enabled_when_no_loops_table(self, tmp_path: Path) -> None:
        toml = _write(tmp_path / "t.toml", "[teatree]\nmode = 'auto'\n")
        with _env(None):
            assert loop_enabled_by_name("review", path=toml) is True


class TestFailSafe:
    def test_missing_file_resolves_enabled(self, tmp_path: Path) -> None:
        with _env(None):
            assert loop_enabled_by_name("review", path=tmp_path / "nope.toml") is True

    def test_malformed_toml_resolves_enabled(self, tmp_path: Path) -> None:
        toml = _write(tmp_path / "bad.toml", "[loops\nenabled = ")
        with _env(None):
            assert loop_enabled_by_name("review", path=toml) is True


@pytest.mark.parametrize("disabled", ["review", "REVIEW", " review , ship "])
def test_case_and_whitespace_tolerant_env(disabled: str, tmp_path: Path) -> None:
    # The named entry matches verbatim after stripping; only the ``all``
    # sentinel is case-insensitive. ``REVIEW`` therefore does NOT disable
    # ``review`` (mirrors LoopsConfig._env_disabled_names).
    with _env(disabled):
        expected_disabled = "review" in {p.strip() for p in disabled.split(",")}
        assert loop_enabled_by_name("review", path=tmp_path / "missing.toml") is (not expected_disabled)
