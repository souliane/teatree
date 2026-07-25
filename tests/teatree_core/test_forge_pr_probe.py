"""The single tri-state open-PR probe the orphan, teardown, and fast-push gates share.

The whole point of unifying the three hand-rolled probes is that FOUND / NONE /
UNKNOWN survive the merge exactly — a fail-closed gate must still see "probe failed"
apart from "no PR". These tests pin all three outcomes at the probe seam and pin
both collapse mappers, so a future edit that quietly folds UNKNOWN into NONE (the
safety regression the fork was guarding against) turns them red.

Real git repos under ``tmp_path`` carry the forge ``origin`` so the remote sniff is
exercised for real; only the ``gh`` / ``glab`` subprocess (the unstoppable network
external) is faked.
"""

import json
import subprocess
from pathlib import Path

import pytest

from teatree.core import forge_pr_probe
from teatree.core.forge_pr_probe import (
    PrProbe,
    PrProbeOutcome,
    find_open_pr_for_branch,
    probe_github_open_pr,
    probe_gitlab_open_pr,
)
from tests._git_repo import make_git_repo, run_git

_GH_URL = "https://github.com/acme/widgets/pull/7"
_MR_URL = "https://gitlab.com/acme/widgets/-/merge_requests/7"


def _repo(tmp_path: Path, *, remote: str) -> Path:
    """A real git repo (born on ``main``) carrying the forge *remote* as ``origin``.

    A real checkout is needed because :func:`find_open_pr_for_branch` sniffs the
    forge from ``git remote get-url origin``; the ``gh`` / ``glab`` subprocess it
    then runs is the only external the tests fake.
    """
    repo = make_git_repo(tmp_path / "clone")
    if remote:
        run_git(repo, "remote", "add", "origin", remote)
    return repo


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["stub"], returncode=returncode, stdout=stdout, stderr="")


def _fake_cli(monkeypatch: pytest.MonkeyPatch, result: object) -> list[list[str]]:
    """Fake the forge subprocess. ``result`` is a CompletedProcess or an exception to raise."""
    seen: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    monkeypatch.setattr(forge_pr_probe, "run_allowed_to_fail", _run)
    return seen


class TestPrProbeMappers:
    """The two collapse mappers each caller uses to keep its own posture."""

    def test_url_or_empty_collapses_none_and_unknown(self) -> None:
        assert PrProbe.found(_GH_URL).url_or_empty() == _GH_URL
        assert PrProbe.none().url_or_empty() == ""
        assert PrProbe.unknown().url_or_empty() == ""

    def test_url_or_none_on_unknown_keeps_them_apart(self) -> None:
        assert PrProbe.found(_GH_URL).url_or_none_on_unknown() == _GH_URL
        assert PrProbe.none().url_or_none_on_unknown() == ""
        assert PrProbe.unknown().url_or_none_on_unknown() is None

    def test_outcome_predicates(self) -> None:
        assert PrProbe.found(_GH_URL).is_found
        assert not PrProbe.none().is_found
        assert PrProbe.unknown().is_unknown
        assert not PrProbe.found(_GH_URL).is_unknown


class TestFindOpenPrForBranch:
    """The sniffing entry: found / none / unknown across both forges and both fail modes."""

    def test_github_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        seen = _fake_cli(monkeypatch, _completed(json.dumps([{"url": _GH_URL}])))
        probe = find_open_pr_for_branch(repo, "feature")
        assert probe.outcome is PrProbeOutcome.FOUND
        assert probe.url == _GH_URL
        assert seen[0][0] == "gh"
        assert "--head" in seen[0]

    def test_gitlab_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="https://gitlab.com/acme/widgets.git")
        seen = _fake_cli(monkeypatch, _completed(json.dumps([{"web_url": _MR_URL}])))
        probe = find_open_pr_for_branch(repo, "feature")
        assert probe.outcome is PrProbeOutcome.FOUND
        assert probe.url == _MR_URL
        assert seen[0][0] == "glab"
        assert "--source-branch" in seen[0]

    def test_empty_array_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        _fake_cli(monkeypatch, _completed("[]"))
        assert find_open_pr_for_branch(repo, "feature").outcome is PrProbeOutcome.NONE

    def test_nonzero_exit_is_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        _fake_cli(monkeypatch, _completed("", returncode=1))
        assert find_open_pr_for_branch(repo, "feature").outcome is PrProbeOutcome.UNKNOWN

    def test_missing_binary_is_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        _fake_cli(monkeypatch, FileNotFoundError("gh"))
        assert find_open_pr_for_branch(repo, "feature").outcome is PrProbeOutcome.UNKNOWN

    def test_unparsable_json_is_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        _fake_cli(monkeypatch, _completed("not json at all"))
        assert find_open_pr_for_branch(repo, "feature").outcome is PrProbeOutcome.UNKNOWN

    def test_non_list_json_is_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        _fake_cli(monkeypatch, _completed('{"url": "x"}'))
        assert find_open_pr_for_branch(repo, "feature").outcome is PrProbeOutcome.UNKNOWN

    def test_no_forge_remote_is_none_without_probing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@git.example.org:acme/widgets.git")
        seen = _fake_cli(monkeypatch, AssertionError("a non-forge remote must not probe"))
        assert find_open_pr_for_branch(repo, "feature").outcome is PrProbeOutcome.NONE
        assert seen == []

    def test_empty_branch_is_unknown_without_probing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo(tmp_path, remote="git@github.com:acme/widgets.git")
        seen = _fake_cli(monkeypatch, AssertionError("an empty branch must not probe"))
        assert find_open_pr_for_branch(repo, "").outcome is PrProbeOutcome.UNKNOWN
        assert seen == []


class TestForgeSpecificProbes:
    """The forge-explicit wrappers fast_push uses — no re-sniff, right CLI each."""

    def test_probe_github_uses_gh_head(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _fake_cli(monkeypatch, _completed(json.dumps([{"url": _GH_URL}])))
        assert probe_github_open_pr(tmp_path, "feature").url == _GH_URL
        assert seen[0][0] == "gh"
        assert seen[0][seen[0].index("--head") + 1] == "feature"

    def test_probe_gitlab_uses_glab_source_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _fake_cli(monkeypatch, _completed(json.dumps([{"web_url": _MR_URL}])))
        assert probe_gitlab_open_pr(tmp_path, "feature").url == _MR_URL
        assert seen[0][0] == "glab"
        assert seen[0][seen[0].index("--source-branch") + 1] == "feature"
