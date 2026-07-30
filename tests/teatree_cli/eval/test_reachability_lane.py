"""F12 / #3566: ``t3 eval reachability`` gives the reachability check a live caller.

The pure engine (:mod:`teatree.eval.scenario_reachability`) shipped with no
production importer — no CLI, no CI. This lane is that caller: it walks every
shipped scenario + fixture ``t3 …`` invocation against the LIVE CLI registry.

It is ADVISORY by default (report + exit 0) because the shipped corpus still
carries known-false-positive references (overlay-slot fixture names, prose
fragments); ``--fail-on-unreachable`` flips it to a gate for when those precision
gaps close, mirroring ``t3 eval coverage --fail-on-gap``.
"""

import contextlib
import io

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval import reachability_lane
from teatree.cli.eval.reachability_lane import reachability, validate_shipped_scenario_reachability
from teatree.eval.scenario_reachability import ReachabilityReport, UnreachableCommand


def _unreachable_report() -> ReachabilityReport:
    return ReachabilityReport(
        unreachable=(UnreachableCommand(source="s.yaml", command="t3 teatree ticket frobnicate"),),
        checked=3,
    )


class TestLiveWiring:
    def test_the_lane_builds_the_registry_and_checks_the_real_corpus(self) -> None:
        report = reachability_lane.validate_shipped_scenario_reachability()
        # Not vacuous — the shipped scenarios + fixtures cite real `t3 …` runs.
        assert report.checked > 0

    def test_validate_shipped_scenario_reachability_walks_the_real_corpus(self) -> None:
        # The directly-imported lane body walks the shipped corpus against the live
        # registry and returns a non-vacuous report.
        report = validate_shipped_scenario_reachability()
        assert report.checked > 0

    def test_reachability_command_renders_a_report(self) -> None:
        # The directly-imported command function renders the advisory report and
        # exits normally (no sys.exit) when gating is off.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reachability(output_format="text", fail_on_unreachable=False)
        assert buf.getvalue().strip()


class TestAdvisoryDefault:
    def test_default_is_advisory_exit_zero_even_with_unreachable_references(self, monkeypatch) -> None:
        monkeypatch.setattr(reachability_lane, "validate_shipped_scenario_reachability", _unreachable_report)
        result = CliRunner().invoke(app, ["eval", "reachability"])
        assert result.exit_code == 0, result.output
        assert "frobnicate" in result.output

    def test_shipped_corpus_run_is_advisory_green(self) -> None:
        # The real corpus carries known false-positive references, but the DEFAULT
        # lane never reds a PR on them.
        result = CliRunner().invoke(app, ["eval", "reachability"])
        assert result.exit_code == 0, result.output


class TestGatingOptIn:
    def test_fail_on_unreachable_reds_when_a_reference_is_unreachable(self, monkeypatch) -> None:
        monkeypatch.setattr(reachability_lane, "validate_shipped_scenario_reachability", _unreachable_report)
        result = CliRunner().invoke(app, ["eval", "reachability", "--fail-on-unreachable"])
        assert result.exit_code == 1, result.output

    def test_fail_on_unreachable_stays_green_when_everything_resolves(self, monkeypatch) -> None:
        monkeypatch.setattr(
            reachability_lane,
            "validate_shipped_scenario_reachability",
            lambda: ReachabilityReport(unreachable=(), checked=5),
        )
        result = CliRunner().invoke(app, ["eval", "reachability", "--fail-on-unreachable"])
        assert result.exit_code == 0, result.output


class TestJsonFormat:
    def test_json_format_emits_the_machine_readable_verdict(self, monkeypatch) -> None:
        monkeypatch.setattr(reachability_lane, "validate_shipped_scenario_reachability", _unreachable_report)
        result = CliRunner().invoke(app, ["eval", "reachability", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert '"ok"' in result.output
        assert '"checked"' in result.output
        assert "frobnicate" in result.output
