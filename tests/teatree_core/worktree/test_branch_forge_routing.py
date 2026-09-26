"""Forge-specific branch probes never cross credentials or ambient logins."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.core import forge_pr_probe
from teatree.core.worktree import branch_classification
from teatree.core.worktree.branch_classification import RedundancyVerdict
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from tests._git_repo import make_git_repo, run_git


@pytest.mark.parametrize("state", [ForgeTokenState.UNSET, ForgeTokenState.UNREADABLE])
def test_gitlab_probe_never_inherits_ambient_or_stored_login(state: ForgeTokenState) -> None:
    runner = MagicMock()
    resolution = ForgeTokenResolution("gitlab_token", "owner", state, route_source="overlay-db")
    with (
        patch.dict(
            os.environ,
            {"GITLAB_TOKEN": "hostile", "GLAB_CONFIG_DIR": "/hostile/stored", "GH_TOKEN": "hostile-gh"},
            clear=False,
        ),
        patch.object(forge_pr_probe, "resolve_repo_token", return_value=resolution) as resolve,
        patch.object(branch_classification, "run_allowed_to_fail", runner),
    ):
        assert branch_classification.probe_host_cli(["glab", "mr", "list"], "/repo", lambda rows: rows) == ""

    resolve.assert_called_once_with("/repo", credential="gitlab_token")
    runner.assert_not_called()


def test_gitlab_probe_uses_db_token_and_isolates_stored_login() -> None:
    resolution = ForgeTokenResolution(
        "gitlab_token", "owner", ForgeTokenState.TOKEN, token="db-gitlab", route_source="overlay-db"
    )
    completed = subprocess.CompletedProcess([], 0, stdout='[{"iid": 7}]', stderr="")
    with (
        patch.dict(
            os.environ,
            {
                "GITLAB_TOKEN": "hostile",
                "GLAB_CONFIG_DIR": "/hostile/stored",
                "GH_TOKEN": "hostile-gh",
                "GITHUB_TOKEN": "hostile-github",
            },
            clear=False,
        ),
        patch.object(forge_pr_probe, "resolve_repo_token", return_value=resolution) as resolve,
        patch.object(branch_classification, "run_allowed_to_fail", return_value=completed) as runner,
    ):
        assert (
            branch_classification.probe_host_cli(["glab", "mr", "list"], "/repo", lambda rows: str(rows[0]["iid"]))
            == "7"
        )

    resolve.assert_called_once_with("/repo", credential="gitlab_token")
    env = runner.call_args.kwargs["env"]
    assert env["GITLAB_TOKEN"] == "db-gitlab"
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["GLAB_CONFIG_DIR"] != "/hostile/stored"


@pytest.mark.parametrize(("forge", "expected_tool"), [("github", "gh"), ("gitlab", "glab")])
def test_merged_probe_invokes_only_the_matching_forge(forge: str, expected_tool: str) -> None:
    seen: list[str] = []

    def _probe(cmd: list[str], *_args: object, **_kwargs: object) -> str:
        seen.append(cmd[0])
        return "7"

    with (
        patch.object(branch_classification, "forge_for_repo", return_value=forge),
        patch.object(branch_classification, "probe_host_cli", side_effect=_probe),
    ):
        branch_classification._branch_pr_is_merged.cache_clear()
        assert branch_classification._branch_pr_is_merged("/repo", "feature") is True

    assert seen == [expected_tool]


def _gitlab_repo(tmp_path: Path) -> Path:
    repo = make_git_repo(tmp_path / "clone")
    run_git(repo, "remote", "add", "origin", "git@gitlab.com:acme/widgets.git")
    return repo


@pytest.mark.parametrize(
    ("open_url", "expected_source"),
    [("https://gitlab.com/acme/widgets/-/merge_requests/7", "open-pr-veto"), ("", "cherry-zero-unique")],
)
def test_gitlab_open_mr_controls_the_redundancy_veto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, open_url: str, expected_source: str
) -> None:
    repo = _gitlab_repo(tmp_path)
    monkeypatch.setattr(forge_pr_probe, "_gitlab_open_mr_url", lambda *_args: open_url)
    content = RedundancyVerdict(redundant=True, forge_merged=False, source="cherry-zero-unique")
    with patch.object(branch_classification, "_content_redundancy", return_value=content):
        verdict = branch_classification.branch_redundancy(str(repo), "feature", "origin/main")

    assert verdict.redundant is (not open_url)
    assert verdict.source == expected_source
