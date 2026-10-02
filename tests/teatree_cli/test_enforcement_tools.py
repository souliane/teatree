"""Tests for cli/enforcement_tools.py — where the ``t3 tool`` leaves take their bearings."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.forge_pr_probe import PrProbe, PrProbeOutcome

runner = CliRunner()


class TestTheRepoDefaultFollowsTheInvocationCwd:
    """#151: under containerized ``t3`` the process cwd is the IMAGE WORKDIR, not the operator's.

    ``deploy/t3`` runs the CLI through a container exec that starts in the image WORKDIR
    and deliberately passes no container workdir — a host cwd usually has no container
    counterpart, so passing one would break every invocation from outside the mounted
    tree. The operator's directory therefore crosses ONLY as ``TEATREE_INVOCATION_CWD``,
    which these leaves never read: measured, ``t3 tool open-pr`` answered ``unknown`` for
    a branch whose merge request exists, and ``unknown`` is a legitimate tri-state value
    so nothing downstream could tell.
    """

    @staticmethod
    def _probed_repo(argv: list[str]) -> Path:
        seen: dict[str, Path] = {}

        def _probe(repo: Path, branch: str) -> PrProbe:
            seen["repo"] = repo
            return PrProbe(outcome=PrProbeOutcome.NONE, url="")

        with (
            patch("teatree.core.forge_pr_probe.find_open_pr_for_branch", side_effect=_probe),
            patch("teatree.utils.git.current_branch", return_value="some-branch"),
        ):
            result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["outcome"] == "none"
        return seen["repo"]

    def test_the_declared_invocation_cwd_is_probed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(tmp_path))

        assert self._probed_repo(["tool", "open-pr"]) == tmp_path

    def test_an_explicit_repo_still_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Control: the declaration is a DEFAULT, never an override of what the caller asked."""
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(tmp_path))

        assert self._probed_repo(["tool", "open-pr", "--repo", str(explicit)]) == explicit

    def test_no_declaration_falls_back_to_the_process_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: a host-native run is byte-identical to the pre-#151 behaviour."""
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)

        assert self._probed_repo(["tool", "open-pr"]) == Path.cwd()

    def test_a_declaration_that_is_not_a_directory_here_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Control: a stale or untranslated value must not redirect the probe."""
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(tmp_path / "does-not-exist"))

        assert self._probed_repo(["tool", "open-pr"]) == Path.cwd()
