"""Integration tests for the ``t3 push`` seam (souliane/teatree#3927).

Real git repos under ``tmp_path`` with a local bare ``origin``; the only faked
things are the environment (credential chain) and the overlay getter. Fixture
secrets are assembled at runtime so this file carries no literal token.
"""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.core import forge_push
from teatree.core.forge_push import PUSH_EXIT_CODES, PUSH_TIMEOUT_SECONDS, PushFailure, PushOutcome, push_branch
from teatree.core.forge_push_credential import CredentialSource
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
        # The bound covers the pre-push hook chain, not just the network (#4484) —
        # generous, but never unbounded (see the mutation tests below).
        assert 900 < PUSH_TIMEOUT_SECONDS <= 3600

    def test_a_push_that_timed_out_is_a_transport_refusal(self, clone_with_origin: Path) -> None:
        with patch(
            "teatree.core.forge_push.run_allowed_to_fail",
            side_effect=TimeoutExpired("git", PUSH_TIMEOUT_SECONDS),
        ):
            outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.TRANSPORT
        assert "timed out" in outcome.detail


class TestPushTimeoutCoversTheHookChainNotJustTransport:
    """PUSH_TIMEOUT_SECONDS must bound hooks-plus-transport, and still be finite (#4484)."""

    #: Longer than the OLD 300s bound (the #4484 regression), shorter than the current
    #: one — a hook chain that takes this long must still succeed under the fix.
    _SIMULATED_HOOK_PHASE_SECONDS = 400.0

    def test_a_hook_phase_past_the_old_bound_still_succeeds_under_the_current_one(
        self, clone_with_origin: Path
    ) -> None:
        """MUTATION: restore ``PUSH_TIMEOUT_SECONDS = 300.0`` → red."""
        real_run = forge_push.run_allowed_to_fail  # captured BEFORE patching, so the fake can still delegate

        def clock_gated_run(cmd: list[str], *, timeout: float | None = None, **kwargs: Any) -> CompletedProcess[str]:
            """A fake clock: refuses the call if its budget is too small, else runs for real.

            No wall-clock sleep — the *requested timeout* stands in for "how long
            this push would need", so a too-small budget reproduces exactly what a
            real ``subprocess.run(timeout=...)`` would do without actually waiting.
            """
            if timeout is not None and timeout < self._SIMULATED_HOOK_PHASE_SECONDS:
                raise TimeoutExpired(cmd, timeout)
            return real_run(cmd, timeout=timeout, **kwargs)

        with patch("teatree.core.forge_push.run_allowed_to_fail", side_effect=clock_gated_run):
            outcome = push_branch(repo=clone_with_origin)

        assert outcome.ok, outcome.detail

    def test_a_genuine_transport_hang_is_still_bounded_not_infinite(self, clone_with_origin: Path) -> None:
        """MUTATION: pass ``timeout=None`` (drop the bound entirely) → red."""
        captured: dict[str, float | None] = {}

        def hangs_forever(cmd: list[str], *, timeout: float | None = None, **_: object) -> CompletedProcess[str]:
            captured["timeout"] = timeout
            raise TimeoutExpired(cmd, timeout)

        with patch("teatree.core.forge_push.run_allowed_to_fail", side_effect=hangs_forever):
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
        with patch("teatree.core.forge_push.run_allowed_to_fail", _RecordingRun()):
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

        with patch("teatree.core.forge_push.run_allowed_to_fail", _RecordingRun()):
            outcome = push_branch(repo=clone_with_origin)

        assert landed != run_git(clone_with_origin, "rev-parse", "HEAD")
        assert not outcome.ok
        assert outcome.failure is PushFailure.REMOTE_SHA_MISMATCH
        assert landed in outcome.detail

    def test_a_remote_that_cannot_be_read_is_not_reported_as_pushed(self, clone_with_origin: Path) -> None:
        """An unreadable remote is an unknown, and an unknown is never a success."""
        run_git(clone_with_origin, "remote", "set-url", "origin", str(clone_with_origin / "gone.git"))

        with patch("teatree.core.forge_push.run_allowed_to_fail", _RecordingRun()):
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
            patch("teatree.core.forge_push.run_allowed_to_fail", _RecordingRun()),
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

        with patch("teatree.core.forge_push.run_allowed_to_fail", side_effect=push_then_commit_locally):
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

        with patch("teatree.core.forge_push.run_allowed_to_fail", _RecordingRun()):
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

    def test_a_gate_that_printed_only_to_stdout_still_has_its_words_kept(self, clone_with_origin: Path) -> None:
        """Git leaves a hook's stdout on `git push`'s stdout, and prek writes its whole diagnostic there."""
        _install_pre_push_hook(clone_with_origin, 'echo "eval-pinned-regressions...Failed"\nexit 1\n')

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.GATE_REFUSED
        assert "eval-pinned-regressions...Failed" in outcome.detail
        assert "failed to push some refs" not in outcome.detail

    def test_a_gate_writing_to_both_streams_keeps_both(self, clone_with_origin: Path) -> None:
        _install_pre_push_hook(clone_with_origin, 'echo "on-stdout"\necho "on-stderr" >&2\nexit 1\n')

        outcome = push_branch(repo=clone_with_origin)

        assert "on-stdout" in outcome.detail
        assert "on-stderr" in outcome.detail

    def test_a_gate_that_died_without_output_names_no_cause_it_did_not_measure(self, clone_with_origin: Path) -> None:
        """A confident wrong diagnosis stops the reader looking; an unmeasured cause is never named."""
        _install_pre_push_hook(clone_with_origin, "exit 137\n")

        outcome = push_branch(repo=clone_with_origin)

        assert not outcome.ok
        assert outcome.failure is PushFailure.GATE_REFUSED
        assert "pre-push" in outcome.detail
        assert "OOM" not in outcome.detail
        assert "prek run --hook-stage pre-push" in outcome.detail

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
        with patch("teatree.core.forge_push.run_allowed_to_fail", return_value=declined):
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
        with patch("teatree.core.forge_push.run_allowed_to_fail", return_value=blocked):
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

        with patch("teatree.core.forge_push.run_allowed_to_fail", recorder):
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
        spies = [_SpyingRun(module.run_allowed_to_fail) for module in (forge_push, git_run)]

        with (
            patch.object(forge_push, "run_allowed_to_fail", spies[0]),
            patch.object(git_run, "run_allowed_to_fail", spies[1]),
        ):
            outcome = push_branch(repo=shadowed, branch="feature")

        assert outcome.ok, outcome.detail
        assert [cmd for spy in spies for cmd in spy.commands if "feature" in cmd] == []


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
