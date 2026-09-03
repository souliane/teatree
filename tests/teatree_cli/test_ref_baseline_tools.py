"""``t3 tool ratchet-prune`` — the CLI surface for the #4451 reference-ratchet repair."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.ref_baseline import RATCHETS

runner = CliRunner()

_STALE_PIN = ("src/teatree/loop/scanners/self_update_ci.py", "teatree.loop.scanners.pr_sweep.GhPrApiClient")
_NEW_PIN = ("src/teatree/quality/ref_baseline.py", "teatree.absent.symbol")

_CLEAN = {name: frozenset() for name in RATCHETS}
_ONE_STALE = {**_CLEAN, "python_prose": frozenset({_STALE_PIN})}
_ONE_NEW = {**_CLEAN, "python_prose": frozenset({_NEW_PIN})}


def _run(args: list[str], *, stale: dict, new: dict, pruned: dict | None = None):
    with (
        patch("teatree.quality.ref_baseline.stale_entries", return_value=stale),
        patch("teatree.quality.ref_baseline.new_entries", return_value=new),
        patch("teatree.quality.ref_baseline.prune", return_value=pruned if pruned is not None else stale) as prune,
    ):
        result = runner.invoke(app, ["tool", "ratchet-prune", *args])
    return result, prune


class TestCheckMode:
    def test_a_clean_tree_exits_zero(self) -> None:
        result, _ = _run([], stale=_CLEAN, new=_CLEAN)
        assert result.exit_code == 0
        assert "both reference ratchets are clean" in result.output

    def test_a_stale_pin_exits_non_zero_and_names_it(self) -> None:
        result, _ = _run([], stale=_ONE_STALE, new=_CLEAN)
        assert result.exit_code == 1
        assert "1 stale pin(s)" in result.output
        assert _STALE_PIN[1] in result.output
        assert "--write" in result.output

    def test_an_unpinned_unresolved_reference_exits_non_zero(self) -> None:
        result, _ = _run([], stale=_CLEAN, new=_ONE_NEW)
        assert result.exit_code == 1
        assert "no pin covers" in result.output
        assert "never auto-banked" in result.output

    def test_check_mode_never_writes(self) -> None:
        _, prune = _run([], stale=_ONE_STALE, new=_CLEAN)
        prune.assert_not_called()


class TestWriteMode:
    def test_it_prunes_and_exits_zero_when_only_stale_pins_were_dirty(self) -> None:
        result, prune = _run(["--write"], stale=_ONE_STALE, new=_CLEAN, pruned=_ONE_STALE)
        assert prune.call_args.kwargs == {"write": True}
        assert result.exit_code == 0, "a repaired stale half must not keep failing"
        assert "Deleted 1 stale pin(s)" in result.output

    def test_it_still_fails_on_the_half_it_cannot_repair(self) -> None:
        result, _ = _run(["--write"], stale=_CLEAN, new=_ONE_NEW, pruned=_CLEAN)
        assert result.exit_code == 1, "an unresolved reference no pin covers is never auto-banked"
        assert "no pin covers" in result.output

    def test_a_clean_tree_reports_no_change(self) -> None:
        result, _ = _run(["--write"], stale=_CLEAN, new=_CLEAN, pruned=_CLEAN)
        assert result.exit_code == 0
        assert "unchanged" in result.output


class TestAgainstTheLiveTree:
    def test_the_command_runs_unmocked_and_reports_this_tree_clean(self) -> None:
        # The mocked cases pin the CLI's own logic; this one pins that the wiring
        # reaches the real scanners and the shipped baseline agrees with them.
        result = runner.invoke(app, ["tool", "ratchet-prune", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == {"written": False, "stale": [], "new": []}


class TestJsonMode:
    def test_it_emits_both_halves(self) -> None:
        result, _ = _run(["--json"], stale=_ONE_STALE, new=_ONE_NEW)
        payload = json.loads(result.output)
        assert payload["written"] is False
        assert payload["stale"] == [{"ratchet": "python_prose", "path": _STALE_PIN[0], "ref": _STALE_PIN[1]}]
        assert payload["new"] == [{"ratchet": "python_prose", "path": _NEW_PIN[0], "ref": _NEW_PIN[1]}]
