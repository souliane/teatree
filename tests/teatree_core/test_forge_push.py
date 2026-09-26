"""Integration tests for the ``t3 push`` seam (souliane/teatree#3927).

Real git repos under ``tmp_path`` with a local bare ``origin``; the only faked
things are the environment (credential chain) and the overlay getter. Fixture
secrets are assembled at runtime so this file carries no literal token.
"""

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.core import forge_push, forge_push_verdict
from teatree.core.forge_push import (
    PUSH_TIMEOUT_SECONDS,
    PushOutcome,
    push_branch,
    remote_url_embeds_credential,
    resolve_forge_credential,
    scrub_token,
)
from teatree.core.forge_push_verdict import (
    PUSH_EXIT_CODES,
    CredentialSource,
    ForgeCredential,
    PushFailure,
    credential_failure_hint,
)
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.utils import git_run
from teatree.utils.git_run import run_with_status
from teatree.utils.run import CompletedProcess, TimeoutExpired
from tests._git_repo import make_git_repo, run_git

FAKE_TOKEN = "gh" + "p_" + "x" * 36


def _install_pre_push_hook(clone: Path, body: str) -> Path:
    hook = clone / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{body}")
    hook.chmod(0o755)
    return hook


def _recording_pre_push_hook(body: str) -> str:
    record_lib = Path(__file__).resolve().parents[2] / "dev" / "lib" / "gate-record.sh"
    return f'. "{record_lib}"\nstart_push_gate_record\ntrap \'finish_push_gate_record "$?"\' EXIT\n{body}'


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


class _SpyingRun:
    """Records every argv it is handed, then delegates to the real runner."""

    def __init__(self, inner: Callable[..., CompletedProcess[str]]) -> None:
        self._inner = inner
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> CompletedProcess[str]:
        self.commands.append(list(cmd))
        return self._inner(cmd, **kwargs)


class _RecordingRun:
    """A push-runner stand-in that records each call and reports success."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.expected_codes: list[object] = []

    def __call__(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        expected_codes: object = (0,),
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        self.envs.append(dict(env or {}))
        self.expected_codes.append(expected_codes)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


class TestResolveForgeCredential:
    def test_uses_the_owning_overlay_route_despite_hostile_ambient(self) -> None:
        routed = ForgeTokenResolution(
            "github_token",
            "owner",
            ForgeTokenState.TOKEN,
            token=FAKE_TOKEN,
            pass_key="owner/github",
        )
        with (
            patch.dict(
                os.environ,
                {"GH_TOKEN": "hostile-gh", "GITHUB_TOKEN": "hostile-github", "TEATREE_GH_TOKEN": "bootstrap-only"},
                clear=False,
            ),
            patch("teatree.core.forge_push.resolve_repo_token", return_value=routed) as resolve,
        ):
            credential = resolve_forge_credential("/repo")

        resolve.assert_called_once_with("/repo", credential="github_token")
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.OVERLAY_PASS_STORE
        assert credential.state is ForgeTokenState.TOKEN

    @pytest.mark.parametrize("state", [ForgeTokenState.UNSET, ForgeTokenState.UNREADABLE])
    def test_preserves_empty_route_state_and_refuses_ambient(self, state: ForgeTokenState) -> None:
        routed = ForgeTokenResolution("github_token", "owner", state, detail=f"route is {state.value}")
        with (
            patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN, "GITHUB_TOKEN": FAKE_TOKEN}, clear=False),
            patch("teatree.core.forge_push.resolve_repo_token", return_value=routed),
        ):
            credential = resolve_forge_credential("/repo")

        assert credential.token == ""
        assert credential.source is CredentialSource.OVERLAY_PASS_STORE
        assert credential.state is state


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
        credential = ForgeCredential(
            token="",
            source=CredentialSource.OVERLAY_PASS_STORE,
            state=ForgeTokenState.UNSET,
            detail="github_token_pass_key is unset",
        )
        hint = credential_failure_hint("fatal: could not read Username for 'https://github.com'", credential)
        assert "github_token_pass_key" in hint
        assert "owning overlay" in hint

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

        with patch("teatree.core.forge_push.run_bounded_group", recorder):
            push_branch(repo=clone_with_origin)

        seen = recorder.envs[-1]
        assert seen["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["GIT_ASKPASS"] == ""
        assert seen["GCM_INTERACTIVE"] == "never"

    def test_push_uses_the_group_bounded_runner_and_accepts_its_verdict(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_bounded_group", recorder):
            push_branch(repo=clone_with_origin)

        assert recorder.commands
        assert recorder.expected_codes == [None]

    def test_passes_the_token_as_gh_token_env_never_on_argv(self, clone_with_origin: Path) -> None:
        routed = ForgeTokenResolution(
            "github_token", "owner", ForgeTokenState.TOKEN, token=FAKE_TOKEN, pass_key="owner/github"
        )
        with patch("teatree.core.forge_push.resolve_repo_token", return_value=routed):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail
        assert outcome.credential_source is CredentialSource.OVERLAY_PASS_STORE
        assert FAKE_TOKEN not in outcome.detail
        assert FAKE_TOKEN not in run_git(clone_with_origin, "config", "--get", "remote.origin.url")

    @pytest.mark.parametrize("state", [ForgeTokenState.UNSET, ForgeTokenState.UNREADABLE])
    def test_github_push_refuses_hostile_ambient_when_owner_route_is_empty(
        self, clone_with_origin: Path, state: ForgeTokenState
    ) -> None:
        run_git(clone_with_origin, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
        routed = ForgeTokenResolution("github_token", "owner", state, detail=f"owner route is {state.value}")
        with (
            patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN, "GITHUB_TOKEN": FAKE_TOKEN}, clear=False),
            patch("teatree.core.forge_push.resolve_repo_token", return_value=routed),
            patch("teatree.core.forge_push.run_bounded_group") as push,
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.CREDENTIAL
        assert state.value in outcome.detail
        assert "refusing ambient" in outcome.detail
        push.assert_not_called()

    def test_never_silences_the_pre_push_hooks(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_bounded_group", recorder):
            push_branch(repo=clone_with_origin)

        pushed = recorder.commands[-1]
        assert "--no-verify" not in pushed
        assert "--force" not in pushed

    def test_force_with_lease_is_opt_in(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_bounded_group", recorder):
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
            patch(
                "teatree.core.forge_push.resolve_repo_token",
                return_value=ForgeTokenResolution(
                    "github_token", "owner", ForgeTokenState.TOKEN, token=FAKE_TOKEN, pass_key="owner/github"
                ),
            ),
            patch("teatree.core.forge_push.run_bounded_group", return_value=blocked),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert "gh auth setup-git" in outcome.detail
        assert FAKE_TOKEN not in outcome.detail

    def test_timeout_is_bounded(self) -> None:
        assert pytest.approx(2700.0) == PUSH_TIMEOUT_SECONDS

    def test_a_push_that_timed_out_is_a_transport_refusal(self, clone_with_origin: Path) -> None:
        with patch(
            "teatree.core.forge_push.run_bounded_group",
            side_effect=TimeoutExpired("git", PUSH_TIMEOUT_SECONDS),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.TRANSPORT
        assert "timed out" in outcome.detail

    def test_a_timeout_after_the_remote_landed_is_reported_as_delivered(self, clone_with_origin: Path) -> None:
        def land_then_time_out(cmd: list[str], *, timeout: float, **kwargs: Any) -> CompletedProcess[str]:
            completed = subprocess.run(cmd, capture_output=True, text=True, check=False, env=kwargs.get("env"))
            assert completed.returncode == 0, completed.stderr
            raise TimeoutExpired(cmd, timeout, output=completed.stdout, stderr=completed.stderr)

        with patch("teatree.core.forge_push.run_bounded_group", side_effect=land_then_time_out):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok
        assert outcome.pushed_sha == run_git(clone_with_origin, "rev-parse", "HEAD")
        assert "deadline hit after landing" in outcome.detail

    def test_a_timeout_with_only_an_old_lock_heartbeat_is_transport(self, clone_with_origin: Path) -> None:
        timed_out = TimeoutExpired(
            "git",
            PUSH_TIMEOUT_SECONDS,
            output="=== push-gate: waiting for lock held by pid=41; elapsed=30s max=1500s ===\n",
        )

        with patch("teatree.core.forge_push.run_bounded_group", side_effect=timed_out):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.TRANSPORT

    def test_a_timeout_during_an_active_gate_is_an_abort_without_a_lock_heartbeat(
        self, clone_with_origin: Path
    ) -> None:
        started = int(time.time())
        record_path = Path(
            run_git(
                clone_with_origin,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "t3-push-gate-run",
            ).strip()
        )
        record_path.write_text(f"started={started}\npid=41\nstage=2\n", encoding="utf-8")

        with (
            patch.object(forge_push.time, "time", return_value=float(started)),
            patch.object(
                forge_push,
                "run_bounded_group",
                side_effect=TimeoutExpired("git", PUSH_TIMEOUT_SECONDS, output="ordinary gate progress\n"),
            ),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_ABORTED
        assert "stage=2" in outcome.detail

    def test_a_timeout_after_a_finished_gate_is_transport_despite_an_old_lock_heartbeat(
        self, clone_with_origin: Path
    ) -> None:
        started = int(time.time())
        record_path = Path(
            run_git(
                clone_with_origin,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "t3-push-gate-run",
            ).strip()
        )
        record_path.write_text(
            f"started={started}\npid=41\nlock_wait_s=30\nstage=3\nfinished={started}\nrc=0\n",
            encoding="utf-8",
        )
        timed_out = TimeoutExpired(
            "git",
            PUSH_TIMEOUT_SECONDS,
            output="=== push-gate: waiting for lock held by pid=17; elapsed=30s max=1500s ===\n",
        )

        with (
            patch.object(forge_push.time, "time", return_value=float(started)),
            patch.object(forge_push, "run_bounded_group", side_effect=timed_out),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.TRANSPORT


class TestPushTimeoutCoversTheHookChainNotJustTransport:
    """PUSH_TIMEOUT_SECONDS must bound hooks-plus-transport, and still be finite (#4484)."""

    #: Longer than the OLD 300s bound (the #4484 regression), shorter than the current
    #: one — a hook chain that takes this long must still succeed under the fix.
    _SIMULATED_HOOK_PHASE_SECONDS = 400.0

    def test_a_hook_phase_past_the_old_bound_still_succeeds_under_the_current_one(
        self, clone_with_origin: Path
    ) -> None:
        """MUTATION: restore ``PUSH_TIMEOUT_SECONDS = 300.0`` → red."""
        real_run = forge_push.run_bounded_group  # captured BEFORE patching, so the fake can still delegate

        def clock_gated_run(cmd: list[str], *, timeout: float | None = None, **kwargs: Any) -> CompletedProcess[str]:
            """A fake clock: refuses the call if its budget is too small, else runs for real.

            No wall-clock sleep — the *requested timeout* stands in for "how long
            this push would need", so a too-small budget reproduces exactly what a
            real ``subprocess.run(timeout=...)`` would do without actually waiting.
            """
            if timeout is not None and timeout < self._SIMULATED_HOOK_PHASE_SECONDS:
                raise TimeoutExpired(cmd, timeout)
            return real_run(cmd, timeout=timeout, **kwargs)

        with patch("teatree.core.forge_push.run_bounded_group", side_effect=clock_gated_run):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail

    def test_a_genuine_transport_hang_is_still_bounded_not_infinite(self, clone_with_origin: Path) -> None:
        """MUTATION: pass ``timeout=None`` (drop the bound entirely) → red."""
        captured: dict[str, float | None] = {}

        def hangs_forever(cmd: list[str], *, timeout: float | None = None, **_: object) -> CompletedProcess[str]:
            captured["timeout"] = timeout
            raise TimeoutExpired(cmd, timeout)

        with patch("teatree.core.forge_push.run_bounded_group", side_effect=hangs_forever):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.TRANSPORT
        # A real hang must still be caught by SOME finite bound — never `None` (an
        # unbounded wait) and never a value so large it is unbounded in practice.
        assert captured["timeout"] is not None
        assert 0 < captured["timeout"] < 4 * 3600


class TestPushOutcome:
    def test_is_json_serialisable_for_the_sub_agent_return_contract(self, clone_with_origin: Path) -> None:
        outcome = push_branch(repo=clone_with_origin)
        assert isinstance(outcome, PushOutcome)
        assert outcome.as_dict()["credential_source"] == outcome.credential_source.value


class TestTheRemoteSettlesWhetherThePushLanded:
    """An rc=0 ``git push`` is a claim; only a read of the remote settles it (#4088)."""

    def test_a_push_that_exited_0_without_landing_is_not_reported_as_pushed(self, clone_with_origin: Path) -> None:
        with patch("teatree.core.forge_push.run_bounded_group", _RecordingRun()):
            outcome = push_branch(repo=clone_with_origin)

        assert not run_git(clone_with_origin, "ls-remote", "--heads", "origin")
        assert not outcome.ok
        assert outcome.failure is PushFailure.NOT_ON_REMOTE
        assert outcome.pushed_sha == ""
        assert "feature" in outcome.detail

    def test_a_remote_ref_left_behind_the_local_tip_is_not_reported_as_pushed(self, clone_with_origin: Path) -> None:
        run_git(clone_with_origin, "push", "-q", "--set-upstream", "origin", "feature")
        landed = run_git(clone_with_origin, "rev-parse", "HEAD")
        (clone_with_origin / "file.txt").write_text("more\n")
        run_git(clone_with_origin, "add", "file.txt")
        run_git(clone_with_origin, "commit", "-q", "-m", "more")

        with patch("teatree.core.forge_push.run_bounded_group", _RecordingRun()):
            outcome = push_branch(repo=clone_with_origin)

        assert landed != run_git(clone_with_origin, "rev-parse", "HEAD")
        assert not outcome.ok
        assert outcome.failure is PushFailure.REMOTE_SHA_MISMATCH
        assert landed in outcome.detail

    def test_a_remote_that_cannot_be_read_is_not_reported_as_pushed(self, clone_with_origin: Path) -> None:
        """An unreadable remote is an unknown, and an unknown is never a success."""
        run_git(clone_with_origin, "remote", "set-url", "origin", str(clone_with_origin / "gone.git"))

        with patch("teatree.core.forge_push.run_bounded_group", _RecordingRun()):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.UNVERIFIABLE

    def test_a_verification_that_times_out_is_not_reported_as_pushed(self, clone_with_origin: Path) -> None:
        timed_out = TimeoutExpired("git", 1.0)

        def time_out_only_on_the_remote_read(*, repo: str, args: list[str], **kwargs: Any) -> CompletedProcess[str]:
            if args[0] == "ls-remote":
                raise timed_out
            return run_with_status(repo=repo, args=args, **kwargs)

        with (
            patch("teatree.core.forge_push.run_bounded_group", _RecordingRun()),
            patch("teatree.core.forge_push.run_with_status", side_effect=time_out_only_on_the_remote_read),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.UNVERIFIABLE

    def test_a_commit_landing_during_the_push_does_not_falsify_it(self, clone_with_origin: Path) -> None:
        """The tip is what was ASKED to be pushed, read before the attempt — not after."""

        def push_then_commit_locally(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            done = subprocess.run(cmd, capture_output=True, text=True, check=False, env=kwargs.get("env"))
            (clone_with_origin / "later.txt").write_text("later\n")
            run_git(clone_with_origin, "add", "later.txt")
            run_git(clone_with_origin, "commit", "-q", "-m", "later")
            return done

        with patch("teatree.core.forge_push.run_bounded_group", side_effect=push_then_commit_locally):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail
        assert outcome.pushed_sha != run_git(clone_with_origin, "rev-parse", "HEAD")

    def test_a_verified_push_carries_the_sha_observed_on_the_remote(self, clone_with_origin: Path) -> None:
        outcome = push_branch(repo=clone_with_origin)

        observed = run_git(clone_with_origin, "ls-remote", "origin", "refs/heads/feature").split()[0]
        assert outcome.ok, outcome.detail
        assert outcome.failure is PushFailure.NONE
        assert outcome.exit_code == 0
        assert outcome.pushed_sha == observed

    def test_a_stale_remote_tracking_ref_cannot_stand_in_for_the_remote(self, clone_with_origin: Path) -> None:
        """The local `origin/feature` is what this clone last heard, not what origin holds."""
        run_git(clone_with_origin, "push", "-q", "--set-upstream", "origin", "feature")
        run_git(clone_with_origin, "push", "-q", "--delete", "origin", "feature")

        with patch("teatree.core.forge_push.run_bounded_group", _RecordingRun()):
            outcome = push_branch(repo=clone_with_origin)

        assert run_git(clone_with_origin, "rev-parse", "origin/feature", check=False)
        assert not outcome.ok
        assert outcome.failure is PushFailure.NOT_ON_REMOTE


class TestAGateRefusalIsToldApartFromATransportFailure:
    """A refusing pre-push gate and a broken transport are different operator actions (#4076)."""

    def test_a_gate_refusal_surfaces_the_gates_own_output_not_gits_outer_message(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(clone_with_origin, 'echo "push-gate: FULL sweep escalated, killed" >&2\nexit 1\n')

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.GATE_REFUSED
        assert "push-gate: FULL sweep escalated, killed" in outcome.detail
        assert "failed to push some refs" not in outcome.detail

    def test_a_gate_that_died_without_output_is_an_infrastructure_abort(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(clone_with_origin, "exit 137\n")

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.GATE_ABORTED
        assert "did not reach a verdict" in outcome.detail
        assert "infrastructure" in outcome.detail
        assert "not a finding against the branch" in outcome.detail
        assert "Nothing was verified and nothing was rejected" in outcome.detail

    def test_a_killed_child_with_a_surviving_recording_wrapper_is_an_abort(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(
            clone_with_origin,
            _recording_pre_push_hook("child_rc=0\nsh -c 'kill -KILL $$' || child_rc=$?\nexit \"$child_rc\"\n"),
        )

        outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_ABORTED
        assert "rc=137" in outcome.detail

    def test_positive_oom_evidence_makes_a_finished_gate_failure_an_abort(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(
            clone_with_origin,
            _recording_pre_push_hook('echo "ordinary gate progress" >&2\nexit 1\n'),
        )

        with (
            patch.object(forge_push, "cgroup_v2_oom_kills", return_value=11),
            patch.object(forge_push_verdict, "cgroup_v2_oom_kills", return_value=12),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_ABORTED
        assert "oom_kill delta: 1" in outcome.detail

    def test_an_unfinished_gate_record_reports_the_stage_bound_and_oom_delta(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(clone_with_origin, 'echo "ordinary gate progress" >&2\nexit 1\n')
        record_path = Path(
            run_git(
                clone_with_origin,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "t3-push-gate-run",
            ).strip()
        )
        started = int(time.time())
        record_path.write_text(
            f"started={started}\npid=41\nlock=/tmp/gate.lock\nlock_wait_s=8\n"
            "bound=workers=2 cap_mib=6144 headroom_mib=2048 reserve_mib=512 per_worker_mib=512\n"
            "stage=3\n",
            encoding="utf-8",
        )

        with (
            patch("teatree.core.forge_push.time.time", return_value=float(started)),
            patch("teatree.core.forge_push.cgroup_v2_oom_kills", return_value=11),
            patch("teatree.core.forge_push_verdict.cgroup_v2_oom_kills", return_value=12),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_ABORTED
        assert "workers=2" in outcome.detail
        assert "cap_mib=6144" in outcome.detail
        assert "stage=3" in outcome.detail
        assert "oom_kill delta: 1" in outcome.detail

    @pytest.mark.parametrize(
        "marker",
        [
            "=== push-gate: ABORTED waiting for lock held by pid=41 ===",
            "crashed while running",
            "replacing crashed worker",
            "Cannot allocate memory",
            "MemoryError",
        ],
    )
    def test_gate_abort_markers_never_become_branch_refusals(self, clone_with_origin: Path, marker: str) -> None:
        _install_pre_push_hook(clone_with_origin, f'echo "{marker}" >&2\nexit 1\n')

        outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_ABORTED

    def test_a_refusal_merely_naming_an_abort_stays_a_refusal(self, clone_with_origin: Path) -> None:
        refusal = "FAILED tests/teatree_core/test_forge_push.py::test_gate_aborted_markers - assert 1 == 2"
        _install_pre_push_hook(clone_with_origin, f'echo "{refusal}" >&2\nexit 1\n')

        outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_REFUSED
        assert refusal in outcome.detail

    def test_git_push_error_combines_stdout_and_stderr_before_classifying(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(clone_with_origin, "exit 0\n")
        killed = subprocess.CompletedProcess(
            args=["git", "push"],
            returncode=1,
            stdout="Cannot allocate memory\n",
            stderr="error: failed to push some refs to '../origin.git'\n",
        )

        with patch("teatree.core.forge_push.run_bounded_group", return_value=killed):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_ABORTED
        assert "Cannot allocate memory" in outcome.detail

    def test_a_non_fast_forward_is_not_blamed_on_the_gate(self, clone_with_origin: Path, tmp_path: Path) -> None:
        run_git(clone_with_origin, "push", "-q", "--set-upstream", "origin", "feature")
        other = make_git_repo(tmp_path / "other", initial_commit=False)
        run_git(other, "remote", "add", "origin", str(tmp_path / "origin.git"))
        run_git(other, "fetch", "-q", "origin")
        run_git(other, "checkout", "-q", "-B", "feature", "origin/feature")
        (other / "file.txt").write_text("theirs\n")
        run_git(other, "add", "file.txt")
        run_git(other, "commit", "-q", "-m", "theirs")
        run_git(other, "push", "-q", "origin", "feature")
        (clone_with_origin / "file.txt").write_text("mine\n")
        run_git(clone_with_origin, "add", "file.txt")
        run_git(clone_with_origin, "commit", "-q", "-m", "mine")
        _install_pre_push_hook(clone_with_origin, "exit 0\n")

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.NON_FAST_FORWARD
        assert "fetch" in outcome.detail

    def test_an_unreachable_remote_is_not_blamed_on_the_gate(self, clone_with_origin: Path) -> None:
        """Every teatree checkout has a pre-push hook, so absence of remote contact proves nothing."""
        _install_pre_push_hook(clone_with_origin, "exit 0\n")
        run_git(clone_with_origin, "remote", "set-url", "origin", "https://nonexistent.invalid/acme/app.git")

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.TRANSPORT

    def test_a_remote_side_rejection_is_not_blamed_on_the_gate(self, clone_with_origin: Path) -> None:
        declined = subprocess.CompletedProcess(
            args=["git", "push"],
            returncode=1,
            stdout="",
            stderr=(
                "To ../origin.git\n"
                " ! [remote rejected] feature -> feature (pre-receive hook declined)\n"
                "error: failed to push some refs to '../origin.git'\n"
            ),
        )
        _install_pre_push_hook(clone_with_origin, "exit 0\n")
        with patch("teatree.core.forge_push.run_bounded_group", return_value=declined):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.REMOTE_REJECTED
        assert "no retry from here" in outcome.detail

    def test_a_gate_that_prints_an_auth_error_is_still_a_gate_refusal(self, clone_with_origin: Path) -> None:
        """The gate's own words must not be mined for another kind's markers."""
        _install_pre_push_hook(clone_with_origin, 'echo "leak-gate: authentication failed in fixture" >&2\nexit 1\n')

        outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.GATE_REFUSED

    def test_a_credential_failure_is_its_own_kind(self, clone_with_origin: Path) -> None:
        blocked = subprocess.CompletedProcess(
            args=["git", "push"],
            returncode=128,
            stdout="",
            stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        )
        with patch("teatree.core.forge_push.run_bounded_group", return_value=blocked):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.CREDENTIAL

    @pytest.mark.parametrize(
        "prepare",
        [
            pytest.param(lambda clone: run_git(clone, "checkout", "-q", "--detach", "HEAD"), id="detached-head"),
            pytest.param(lambda clone: run_git(clone, "remote", "remove", "origin"), id="no-such-remote"),
        ],
    )
    def test_a_repo_config_refusal_is_its_own_kind(
        self, clone_with_origin: Path, prepare: Callable[[Path], object]
    ) -> None:
        prepare(clone_with_origin)

        outcome = push_branch(repo=clone_with_origin)

        assert outcome.failure is PushFailure.CONFIG


class TestABranchThatDoesNotExistIsNeverTheGatesFault:
    """git resolves the refspec BEFORE it runs any hook, so the hook cannot be the reason."""

    def test_a_misspelled_branch_is_a_config_refusal_not_a_gate_refusal(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(clone_with_origin, "exit 0\n")

        outcome = push_branch(repo=clone_with_origin, branch="no-such-branch")

        assert not outcome.ok
        assert outcome.failure is PushFailure.CONFIG
        assert outcome.exit_code == PUSH_EXIT_CODES[PushFailure.CONFIG]
        assert "no-such-branch" in outcome.detail

    @pytest.mark.parametrize("spelling", ["HEAD", "refs/heads/feature", "feature"])
    def test_every_spelling_git_push_accepts_still_works(self, clone_with_origin: Path, spelling: str) -> None:
        """The refusal must catch a typo, never a legal way of naming the same branch."""
        outcome = push_branch(repo=clone_with_origin, branch=spelling)

        assert outcome.ok, outcome.detail
        assert outcome.branch == "feature"
        assert outcome.pushed_sha == run_git(clone_with_origin, "rev-parse", "HEAD")

    def test_the_push_is_never_attempted_for_an_unknown_branch(self, clone_with_origin: Path) -> None:
        recorder = _RecordingRun()

        with patch("teatree.core.forge_push.run_bounded_group", recorder):
            push_branch(repo=clone_with_origin, branch="no-such-branch")

        assert recorder.commands == []


class TestATagSharingABranchsNameCannotBeResolvedForIt:
    """One ref form throughout, so no lookup can answer the tag where the branch was meant.

    A tag named after the branch makes every bare spelling ambiguous at once:
    `rev-parse --abbrev-ref HEAD` answers `heads/feature`, `rev-parse feature`
    answers the TAG's sha, and `push origin feature` refuses the refspec before any
    hook runs (souliane/teatree#4117).
    """

    @pytest.fixture
    def shadowed(self, clone_with_origin: Path) -> Path:
        """A clone whose `feature` branch is shadowed by a `feature` tag at an EARLIER sha."""
        run_git(clone_with_origin, "tag", "feature", "HEAD")
        (clone_with_origin / "more.txt").write_text("more\n")
        run_git(clone_with_origin, "add", "more.txt")
        run_git(clone_with_origin, "commit", "-q", "-m", "more")
        return clone_with_origin

    def test_the_auto_detected_branch_is_not_refused_as_a_typo(self, shadowed: Path) -> None:
        outcome = push_branch(repo=shadowed)

        assert outcome.ok, outcome.detail
        assert outcome.branch == "feature"
        assert outcome.pushed_sha == run_git(shadowed, "rev-parse", "refs/heads/feature")

    def test_an_explicit_branch_is_not_blamed_on_the_pre_push_gate(self, shadowed: Path) -> None:
        _install_pre_push_hook(shadowed, "exit 0\n")

        outcome = push_branch(repo=shadowed, branch="feature")

        assert outcome.ok, outcome.detail
        assert outcome.failure is PushFailure.NONE
        assert outcome.pushed_sha == run_git(shadowed, "rev-parse", "refs/heads/feature")

    def test_the_tags_sha_is_never_what_lands(self, shadowed: Path) -> None:
        """The tag is the earlier commit, so reading it would push — or verify — the wrong sha."""
        outcome = push_branch(repo=shadowed)

        assert outcome.ok, outcome.detail
        assert outcome.pushed_sha != run_git(shadowed, "rev-parse", "refs/tags/feature")

    def test_no_git_call_names_the_branch_in_its_bare_form(self, shadowed: Path) -> None:
        """The grep-proof half: a bare name left anywhere is a lookup a tag can answer."""
        push_spy = _SpyingRun(forge_push.run_bounded_group)
        git_spy = _SpyingRun(git_run.run_allowed_to_fail)

        with (
            patch.object(forge_push, "run_bounded_group", push_spy),
            patch.object(git_run, "run_allowed_to_fail", git_spy),
        ):
            outcome = push_branch(repo=shadowed, branch="feature")

        assert outcome.ok, outcome.detail
        assert [cmd for spy in (push_spy, git_spy) for cmd in spy.commands if "feature" in cmd] == []


class TestAPushUrlIsWhereThePushActuallyGoes:
    """`remote.<name>.pushurl` divorces the push target from the fetch url."""

    def test_a_credential_in_the_push_url_is_refused_too(self, clone_with_origin: Path) -> None:
        run_git(
            clone_with_origin,
            "config",
            "remote.origin.pushurl",
            f"https://{FAKE_TOKEN}@github.example.invalid/acme/app.git",
        )

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.CONFIG
        assert "embeds a credential" in outcome.detail
        assert FAKE_TOKEN not in outcome.detail

    def test_the_push_url_is_the_endpoint_read_back(self, clone_with_origin: Path, tmp_path: Path) -> None:
        """Verifying against the fetch url would confirm a ref the push never touched."""
        elsewhere = make_git_repo(tmp_path / "elsewhere.git", bare=True)
        run_git(clone_with_origin, "config", "remote.origin.pushurl", str(elsewhere))

        outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail
        assert (
            outcome.pushed_sha
            == run_git(clone_with_origin, "ls-remote", str(elsewhere), "refs/heads/feature").split()[0]
        )
        assert not run_git(clone_with_origin, "ls-remote", "--heads", str(tmp_path / "origin.git"))

    def test_without_a_pushurl_the_remotes_own_config_still_applies(self, clone_with_origin: Path) -> None:
        """Reading a raw url would silently drop the `uploadpack` / `proxy` the push honours."""
        run_git(clone_with_origin, "config", "remote.origin.uploadpack", "/nonexistent-upload-pack")

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.UNVERIFIABLE


class TestEveryFailureKindIsActionable:
    def test_every_kind_has_an_exit_code(self) -> None:
        assert set(PUSH_EXIT_CODES) == set(PushFailure)

    def test_only_success_maps_to_zero(self) -> None:
        zeros = [failure for failure, code in PUSH_EXIT_CODES.items() if code == 0]
        assert zeros == [PushFailure.NONE]
