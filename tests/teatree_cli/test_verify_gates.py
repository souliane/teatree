"""``t3 tool verify-gates`` runs BOTH the commit-stage AND push-stage hooks.

Meta-test pinning the CI-parity contract: a bare ``prek run --all-files`` only
fires the commit/manual-stage hooks (``default_stages: [commit, manual]``), so
the push-stage gates CI re-runs (comment-density, doc-update, ensure-pr,
the public-repo leak gate) are structurally skipped. The
verify-gates command must invoke the push stage too, or "local green" can
disagree with CI. These tests assert the push-stage invocation exists and that
a push-stage failure fails the command.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app

runner = CliRunner()


def _calls(mock) -> list[list[str]]:
    return [list(call.args[0]) for call in mock.call_args_list]


class TestVerifyGatesRunsBothStages:
    def test_invokes_push_stage_hooks_not_just_commit(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        calls = _calls(run)
        # One bare commit/manual-stage run.
        assert ["prek", "run", "--all-files"] in calls
        # One push-stage run — the gate CI re-runs that the bare run skips.
        push = [c for c in calls if "--hook-stage" in c]
        assert push, "verify-gates must invoke the push stage"
        assert push[0][-2:] == ["--hook-stage", "pre-push"]

    def test_uses_canonical_pre_push_stage_value(self) -> None:
        """Prek rejects the literal ``push``; the canonical value is ``pre-push``."""
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            runner.invoke(app, ["tool", "verify-gates"])
        push = [c for c in _calls(run) if "--hook-stage" in c]
        assert push, "verify-gates must invoke the push stage"
        assert "push" not in push[0], "must pass pre-push, not the rejected 'push'"

    def test_fails_when_push_stage_fails(self) -> None:
        def _rc(cmd: list[str], **_kwargs: object) -> int:
            return 1 if "--hook-stage" in cmd else 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_rc),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1

    def test_fails_when_commit_stage_fails(self) -> None:
        def _rc(cmd: list[str], **_kwargs: object) -> int:
            return 0 if "--hook-stage" in cmd else 1

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_rc),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1

    def test_both_green_passes(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0

    def test_missing_prek_fails_closed(self) -> None:
        with patch("teatree.cli.verify_gates._prek_available", return_value=False):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
