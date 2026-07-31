"""Integration tests for the ``t3 push`` seam (souliane/teatree#3927).

Real git repos under ``tmp_path`` with a local bare ``origin``; the only faked
things are the environment (credential chain) and the overlay getter. Fixture
secrets are assembled at runtime so this file carries no literal token.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.forge_push import (
    PUSH_TIMEOUT_SECONDS,
    CredentialSource,
    ForgeCredential,
    PushOutcome,
    credential_failure_hint,
    push_branch,
    remote_url_embeds_credential,
    resolve_forge_credential,
    scrub_token,
)
from tests._git_repo import make_git_repo, run_git

FAKE_TOKEN = "gh" + "p_" + "x" * 36


@pytest.fixture
def clone_with_origin(tmp_path: Path) -> Path:
    """A working clone whose ``origin`` is a local bare repo (a real push target)."""
    remote = make_git_repo(tmp_path / "origin.git", bare=True)
    clone = make_git_repo(tmp_path / "clone")
    run_git(clone, "remote", "add", "origin", str(remote))
    run_git(clone, "checkout", "-q", "-b", "feature")
    (clone / "file.txt").write_text("work\n")
    run_git(clone, "add", "file.txt")
    run_git(clone, "commit", "-q", "-m", "work")
    return clone


class _RecordingRun:
    """A ``run_allowed_to_fail`` stand-in that records each call and reports success."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(
        self, cmd: list[str], *, env: dict[str, str] | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        self.envs.append(dict(env or {}))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


class TestResolveForgeCredential:
    def test_prefers_gh_token_over_teatree_gh_token(self) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN, "TEATREE_GH_TOKEN": "other"}, clear=False):
            credential = resolve_forge_credential()
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.GH_TOKEN

    def test_falls_back_to_teatree_gh_token(self) -> None:
        env = {"TEATREE_GH_TOKEN": FAKE_TOKEN}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GH_TOKEN", None)
            credential = resolve_forge_credential()
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.TEATREE_GH_TOKEN

    def test_falls_back_to_overlay_pass_store(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.core.forge_push._overlay_github_token", return_value=FAKE_TOKEN),
        ):
            os.environ.pop("GH_TOKEN", None)
            os.environ.pop("TEATREE_GH_TOKEN", None)
            credential = resolve_forge_credential()
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.OVERLAY_PASS_STORE

    def test_no_credential_falls_through_to_ambient_helper(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.core.forge_push._overlay_github_token", return_value=""),
        ):
            os.environ.pop("GH_TOKEN", None)
            os.environ.pop("TEATREE_GH_TOKEN", None)
            credential = resolve_forge_credential()
        assert credential.token == ""
        assert credential.source is CredentialSource.AMBIENT


class TestRemoteUrlEmbedsCredential:
    def test_flags_a_token_in_the_userinfo(self) -> None:
        assert remote_url_embeds_credential(f"https://{FAKE_TOKEN}@github.com/acme/app.git")

    def test_flags_a_password_component(self) -> None:
        url = "https://user:hunter2@github.com/acme/app.git"  # privacy-scan:allow (fake test credential, not PII)
        assert remote_url_embeds_credential(url)

    def test_leaves_a_plain_https_remote_alone(self) -> None:
        assert not remote_url_embeds_credential("https://github.com/acme/app.git")

    def test_leaves_an_scp_style_ssh_remote_alone(self) -> None:
        assert not remote_url_embeds_credential("git@github.com:acme/app.git")

    def test_a_malformed_url_is_not_flagged_and_does_not_raise(self) -> None:
        url = "https://[oops@github.com/acme/app.git"  # privacy-scan:allow (malformed fixture URL, not PII)
        assert not remote_url_embeds_credential(url)


class TestCredentialFailureHint:
    def test_names_the_token_sources_when_none_resolved(self) -> None:
        credential = ForgeCredential(token="", source=CredentialSource.AMBIENT)
        hint = credential_failure_hint("fatal: could not read Username for 'https://github.com'", credential)
        assert "TEATREE_GH_TOKEN" in hint
        assert "pass store" in hint

    def test_names_the_helper_wiring_when_a_token_was_supplied(self) -> None:
        credential = ForgeCredential(token=FAKE_TOKEN, source=CredentialSource.TEATREE_GH_TOKEN)
        hint = credential_failure_hint("fatal: Authentication failed for 'https://github.com'", credential)
        assert "gh auth setup-git" in hint
        assert FAKE_TOKEN not in hint

    def test_is_silent_for_a_failure_that_is_not_about_credentials(self) -> None:
        credential = ForgeCredential(token=FAKE_TOKEN, source=CredentialSource.GH_TOKEN)
        assert credential_failure_hint("! [rejected] feature -> feature (non-fast-forward)", credential) == ""


class TestScrubToken:
    def test_replaces_every_occurrence(self) -> None:
        assert scrub_token(f"a {FAKE_TOKEN} b {FAKE_TOKEN}", FAKE_TOKEN) == "a <redacted> b <redacted>"

    def test_empty_token_is_a_no_op(self) -> None:
        assert scrub_token("nothing to hide", "") == "nothing to hide"


class TestPushBranch:
    def test_pushes_the_current_branch_and_verifies_by_re_read(self, clone_with_origin: Path) -> None:
        outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail
        assert outcome.branch == "feature"
        assert outcome.remote == "origin"
        local_sha = run_git(clone_with_origin, "rev-parse", "HEAD")
        assert outcome.pushed_sha == local_sha

    def test_is_idempotent(self, clone_with_origin: Path) -> None:
        assert push_branch(repo=clone_with_origin).ok
        second = push_branch(repo=clone_with_origin)
        assert second.ok, second.detail
        assert second.pushed_sha == run_git(clone_with_origin, "rev-parse", "HEAD")

    def test_refuses_a_remote_url_that_embeds_a_credential(self, clone_with_origin: Path) -> None:
        run_git(
            clone_with_origin,
            "remote",
            "set-url",
            "origin",
            f"https://{FAKE_TOKEN}@github.example.invalid/acme/app.git",
        )

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert "embeds a credential" in outcome.detail
        assert FAKE_TOKEN not in outcome.detail
        assert "t3 push" in outcome.detail

    def test_refuses_a_detached_head(self, clone_with_origin: Path) -> None:
        run_git(clone_with_origin, "checkout", "-q", "--detach", "HEAD")

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert "detached HEAD" in outcome.detail
        assert not run_git(clone_with_origin, "ls-remote", "--heads", "origin")

    def test_refuses_an_unknown_remote_without_touching_the_known_one(self, clone_with_origin: Path) -> None:
        outcome = push_branch(repo=clone_with_origin, remote="upstream")

        assert not outcome.ok
        assert "upstream" in outcome.detail
        assert not run_git(clone_with_origin, "ls-remote", "--heads", "origin")

    def test_child_env_disables_the_interactive_credential_prompt(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_allowed_to_fail", recorder):
            push_branch(repo=clone_with_origin)

        seen = recorder.envs[-1]
        assert seen["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["GIT_ASKPASS"] == ""
        assert seen["GCM_INTERACTIVE"] == "never"

    def test_passes_the_token_as_gh_token_env_never_on_argv(self, clone_with_origin: Path) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN}, clear=False):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail
        assert outcome.credential_source is CredentialSource.GH_TOKEN
        assert FAKE_TOKEN not in outcome.detail
        assert FAKE_TOKEN not in run_git(clone_with_origin, "config", "--get", "remote.origin.url")

    def test_never_silences_the_pre_push_hooks(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_allowed_to_fail", recorder):
            push_branch(repo=clone_with_origin)

        pushed = recorder.commands[-1]
        assert "--no-verify" not in pushed
        assert "--force" not in pushed

    def test_force_with_lease_is_opt_in(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_allowed_to_fail", recorder):
            push_branch(repo=clone_with_origin, force_with_lease=True)

        assert "--force-with-lease" in recorder.commands[-1]
        assert "--force" not in recorder.commands[-1]

    def test_a_failed_push_reports_the_git_error_and_is_not_ok(self, clone_with_origin: Path) -> None:
        run_git(clone_with_origin, "remote", "set-url", "origin", str(clone_with_origin / "missing.git"))

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.detail

    def test_a_credential_class_failure_carries_the_actionable_hint(self, clone_with_origin: Path) -> None:
        blocked = subprocess.CompletedProcess(
            args=["git", "push"],
            returncode=128,
            stdout="",
            stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        )
        with (
            patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN}, clear=False),
            patch("teatree.core.forge_push.run_allowed_to_fail", return_value=blocked),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert "gh auth setup-git" in outcome.detail
        assert FAKE_TOKEN not in outcome.detail

    def test_timeout_is_bounded(self) -> None:
        assert 0 < PUSH_TIMEOUT_SECONDS <= 600


class TestPushOutcome:
    def test_is_json_serialisable_for_the_sub_agent_return_contract(self, clone_with_origin: Path) -> None:
        outcome = push_branch(repo=clone_with_origin)
        assert isinstance(outcome, PushOutcome)
        assert outcome.as_dict()["credential_source"] == outcome.credential_source.value
