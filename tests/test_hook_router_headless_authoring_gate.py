# test-path: cross-cutting — tests the headless_authoring_gate PreToolUse handler wired into hook_router.py.
"""The configured headless posture must be ENFORCED, not merely stored (#3883).

``agent_runtime = headless`` says all implementation work runs through the factory. Nothing
made that true: an interactive Claude Code session could edit ``src/``, dispatch ``t3:coder``
sub-agents, and commit — and none of it was refused.

The single most important case in this file is
:meth:`TestTheFactoryIsNeverRefused.test_a_factory_dispatched_sdk_session_is_allowed`. The
factory's own workers run through the Agent SDK with the SAME hook set, so a gate keyed on
"what is being touched" rather than "who is acting" would refuse the very agents meant to do
the implementing — the fix would stop the factory instead of feeding it. Every case here is
paired with its opposite so a uniformly-permissive gate (which would pass the allow-cases)
and a uniformly-restrictive one (which would pass the refuse-cases) both go red.
"""

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import headless_authoring_gate as gate
from tests._git_repo import make_git_repo, run_git

#: The env an INTERACTIVE Claude Code CLI session presents. The SDK transport sets
#: ``CLAUDE_CODE_ENTRYPOINT=sdk-py`` and strips ``CLAUDECODE`` from the child env, so these
#: two keys are what separate a human-driven session from every SDK embedding.
#: ``CLAUDE_AGENT_SDK_VERSION`` is pinned EMPTY rather than left absent: these patches merge
#: into the real environment, and when the suite itself runs under an SDK agent that marker
#: is already set — ``_lane`` would read SDK, and every refuse-case here would go green-by-
#: allowing. The fixture must state the whole interactive contract, not inherit half of it.
_INTERACTIVE_ENV = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDECODE": "1", "CLAUDE_AGENT_SDK_VERSION": ""}
_SDK_ENV = {"CLAUDE_CODE_ENTRYPOINT": "sdk-py", "CLAUDE_AGENT_SDK_VERSION": "0.2.95"}


def _run_chain(data: dict) -> bool:
    """Run the real PreToolUse handler chain; ``True`` when some gate denied."""
    return any(handler(data) is True for handler in router._HANDLERS["PreToolUse"])


@pytest.fixture
def engaged_session(tmp_path: Path) -> Iterator[Path]:
    """A teatree-ENGAGED interactive session under the headless posture, in a main clone."""
    with (
        mock.patch.object(router, "STATE_DIR", tmp_path),
        mock.patch.object(router, "_teatree_engaged", return_value=True),
        mock.patch.object(gate, "_posture_is_headless", return_value=True),
        mock.patch.object(gate, "_path_is_in_live_worktree", return_value=False),
        mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
        mock.patch.dict(os.environ, _INTERACTIVE_ENV, clear=False),
    ):
        yield tmp_path


def _edit(path: str = "/repo/src/teatree/config/resolution.py") -> dict:
    return {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": path, "new_string": "x"}}


def _dispatch(subagent: str = "t3:coder") -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent, "prompt": "implement it", "run_in_background": True},
    }


class TestTheInteractiveSessionIsRefused:
    def test_an_edit_to_teatree_source_is_refused(self, engaged_session: Path, capsys) -> None:
        assert _run_chain(_edit()) is True

    def test_the_refusal_names_the_factory_route(self, engaged_session: Path, capsys) -> None:
        _run_chain(_edit())
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        # A gate that says "no" without saying "do this instead" produces an agent that
        # retries creatively, which is worse than the thing being refused.
        assert "issue" in reason.lower()

    def test_dispatching_an_implementation_subagent_is_refused(self, engaged_session: Path, capsys) -> None:
        assert _run_chain(_dispatch("t3:coder")) is True


class TestTheFactoryIsNeverRefused:
    def test_a_factory_dispatched_sdk_session_is_allowed(self, tmp_path: Path) -> None:
        # THE load-bearing assertion. A wrong refusal here halts the whole factory and
        # every SDK consumer; a wrong allow costs one hand-written edit a human notices.
        with (
            mock.patch.object(router, "STATE_DIR", tmp_path),
            mock.patch.object(router, "_teatree_engaged", return_value=True),
            mock.patch.object(gate, "_posture_is_headless", return_value=True),
            mock.patch.object(gate, "_path_is_in_live_worktree", return_value=False),
            mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
            mock.patch.dict(os.environ, _SDK_ENV, clear=False),
        ):
            os.environ.pop("CLAUDECODE", None)
            assert _run_chain(_edit()) is False
            assert _run_chain(_dispatch("t3:coder")) is False

    def test_an_unreadable_lane_signal_allows(self, tmp_path: Path) -> None:
        # Fail OPEN — the inverse of every other gate in this repo, deliberately. An
        # "unknown" verdict is not evidence of an interactive session.
        with (
            mock.patch.object(router, "STATE_DIR", tmp_path),
            mock.patch.object(router, "_teatree_engaged", return_value=True),
            mock.patch.object(gate, "_posture_is_headless", return_value=True),
            mock.patch.object(gate, "_path_is_in_live_worktree", return_value=False),
            mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            for key in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION"):
                os.environ.pop(key, None)
            assert _run_chain(_edit()) is False

    def test_an_unreadable_posture_allows(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_posture_is_headless", return_value=None):
            assert _run_chain(_edit()) is False


class TestTheCoordinatorRoleStaysAvailable:
    def test_reading_and_searching_are_not_refused(self, engaged_session: Path) -> None:
        for command in ("git log --oneline -5", "rg 'autonomy' src/", "t3 doctor check"):
            data = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
            assert _run_chain(data) is False, command

    def test_review_merge_and_issue_filing_are_not_refused(self, engaged_session: Path) -> None:
        for command in (
            "t3 teatree ticket merge 42",
            "t3 teatree ticket clear 42",
            "gh issue create --title x --body y",
            "gh issue comment 42 --body y",
        ):
            data = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
            assert _run_chain(data) is False, command

    def test_host_operations_the_factory_cannot_do_are_not_refused(self, engaged_session: Path) -> None:
        # Dogfooding REQUIRES running t3 commands that mutate host state.
        for command in ("t3 teatree workspace reclaim-disk", "t3 teatree workspace clean-all"):
            data = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
            assert _run_chain(data) is False, command

    def test_a_non_implementation_subagent_dispatch_is_not_refused(self, engaged_session: Path) -> None:
        for subagent in ("Explore", "t3:reviewer", "t3:followup", "general-purpose"):
            assert _run_chain(_dispatch(subagent)) is False, subagent


class TestTheCarveOutsHold:
    def test_work_already_in_flight_in_a_live_worktree_is_not_refused(self, engaged_session: Path) -> None:
        # A t3-managed worktree exists only because a ticket started there. Handing that
        # back to the factory means reconciling state that is cheaper to finish in place;
        # only NEW work is refused.
        with mock.patch.object(gate, "_path_is_in_live_worktree", return_value=True):
            assert _run_chain(_edit("/wt/3873/src/teatree/x.py")) is False

    def test_the_same_edit_without_a_live_checkout_is_refused(self, engaged_session: Path) -> None:
        # The foil that proves the carve-out discriminates rather than swallowing the gate.
        assert _run_chain(_edit("/wt/3873/src/teatree/x.py")) is True

    def test_an_audited_override_unblocks_exactly_one_action(self, engaged_session: Path, tmp_path: Path) -> None:
        allowed = {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/repo/src/teatree/x.py",
                "new_string": "fix [headless-authoring-ok: factory is down, restoring it]",
            },
        }
        assert _run_chain(allowed) is False
        # Exactly one: the NEXT call, without the token, is refused again.
        assert _run_chain(_edit()) is True

    def test_the_override_is_recorded_with_its_reason(self, engaged_session: Path) -> None:
        data = {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/src/teatree/x.py", "new_string": "[headless-authoring-ok: emergency]"},
        }
        _run_chain(data)
        audit = engaged_session / "s1.authoring-overrides"
        assert audit.is_file()
        assert "emergency" in audit.read_text(encoding="utf-8")

    def test_an_empty_override_reason_does_not_unblock(self, engaged_session: Path) -> None:
        data = {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/src/teatree/x.py", "new_string": "[headless-authoring-ok: ]"},
        }
        assert _run_chain(data) is True


class TestDefaultOffIsPreserved:
    def test_nothing_is_refused_when_the_posture_is_not_headless(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_posture_is_headless", return_value=False):
            assert _run_chain(_edit()) is False
            assert _run_chain(_dispatch("t3:coder")) is False

    def test_an_unengaged_session_is_never_refused(self, engaged_session: Path) -> None:
        # A colleague cloning the repo has not opted in and must feel nothing.
        with mock.patch.object(router, "_teatree_engaged", return_value=False):
            assert _run_chain(_edit()) is False

    def test_an_edit_outside_the_teatree_repo_is_not_refused(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_targets_teatree_repo", return_value=False):
            assert _run_chain(_edit("/other/project/src/app.py")) is False

    def test_the_kill_switch_disables_the_gate(self, engaged_session: Path) -> None:
        with mock.patch.object(gate, "_gate_enabled", return_value=False):
            assert _run_chain(_edit()) is False


def _write_config_db(db: Path, rows: dict[str, object]) -> Path:
    """A minimal control DB carrying *rows* as global-scope ``ConfigSetting`` values."""
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT)")
        conn.executemany(
            "INSERT INTO teatree_config_setting VALUES ('', ?, ?)",
            [(key, json.dumps(value)) for key, value in rows.items()],
        )
    conn.close()
    return db


class TestThePostureReadIsRealRatherThanAlwaysUnreadable:
    """The posture reader, exercised for real rather than mocked.

    The gate is inert if ``_posture_is_headless`` always raises — and every other case here
    mocks it, so nothing above would notice. These run the REAL reader against a real control
    DB, so a wrong call signature (which fails open and disables the gate silently) turns this
    red instead of passing unnoticed.
    """

    def test_a_stored_headless_runtime_reads_as_headless(self, tmp_path: Path) -> None:
        db = _write_config_db(tmp_path / "db.sqlite3", {"agent_runtime": "headless"})
        with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(db), "T3_OVERLAY_NAME": ""}):
            assert gate._posture_is_headless() is True

    def test_a_stored_interactive_runtime_reads_as_not_headless(self, tmp_path: Path) -> None:
        db = _write_config_db(tmp_path / "db.sqlite3", {"agent_runtime": "interactive"})
        with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(db), "T3_OVERLAY_NAME": ""}):
            assert gate._posture_is_headless() is False

    def test_an_absent_store_is_unreadable_rather_than_a_verdict(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(tmp_path / "missing.sqlite3")}):
            assert gate._posture_is_headless() is None


def _bash(command: str, cwd: str) -> dict:
    return {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _run_gate(data: dict) -> bool:
    """Run ONLY this gate's handler; ``True`` when IT denied.

    :func:`_run_chain` cannot settle a ``git commit`` case: several other ``PreToolUse``
    gates also have an opinion about a commit, so a chain-level refusal would not attribute
    the verdict to this gate. The cases above already cover that this gate is wired in.
    """
    return gate.handle_block_interactive_authoring(data) is True


@pytest.fixture
def clone_and_live_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real primary clone and a real LINKED worktree of it, each carrying a ``src/`` dir.

    The carve-out dimension is deliberately NOT mocked here: the clone gets a ``.git``
    DIRECTORY and the worktree a ``.git`` FILE, which is the whole of what distinguishes
    them, and a mock of that distinction would test only the mock.
    """
    clone = make_git_repo(tmp_path / "clone")
    (clone / "src").mkdir()
    (clone / "src" / "seed.py").write_text("x = 1\n", encoding="utf-8")
    run_git(clone, "add", "src/seed.py")
    run_git(clone, "commit", "-q", "-m", "seed")
    worktree = tmp_path / "wt"
    run_git(clone, "worktree", "add", "-q", str(worktree), "-b", "3962-ticket")
    return clone, worktree


@pytest.fixture
def headless_interactive(tmp_path: Path) -> Iterator[None]:
    """An engaged interactive session under the headless posture, with the carve-out LIVE.

    The sibling :func:`engaged_session` pins ``_path_is_in_live_worktree`` to ``False``,
    which is precisely the answer these cases exist to compute. Only the orthogonal
    "is this repo teatree-managed" dimension is pinned.
    """
    with (
        mock.patch.object(router, "STATE_DIR", tmp_path / "state"),
        mock.patch.object(router, "_teatree_engaged", return_value=True),
        mock.patch.object(gate, "_gate_enabled", return_value=True),
        mock.patch.object(gate, "_posture_is_headless", return_value=True),
        mock.patch.object(gate, "_targets_teatree_repo", return_value=True),
        mock.patch.dict(os.environ, _INTERACTIVE_ENV, clear=False),
    ):
        yield


class TestACommitCanFollowTheEditThatProducedIt:
    """The ``Bash`` branch honours the same live-worktree carve-out as the file branch.

    Applying the carve-out to ``Edit``/``Write`` but not to ``git commit`` left an agent
    able to author a change inside a live worktree and then unable to commit it — with the
    refusal text advertising the very exemption the ``Bash`` branch did not implement.
    """

    def test_a_commit_inside_a_live_worktree_is_allowed(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        _clone, worktree = clone_and_live_worktree
        assert _run_gate(_bash("git commit -m 'fix the thing'", str(worktree))) is False

    def test_the_edit_that_produced_that_commit_is_allowed_too(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        # The pair the defect split apart: one worktree, one piece of work, two verdicts
        # that must agree. This side was already allowed; the commit side was not.
        _clone, worktree = clone_and_live_worktree
        assert _run_gate(_edit(str(worktree / "src" / "seed.py"))) is False

    def test_a_commit_in_the_primary_clone_is_still_refused(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        # The control that proves the carve-out discriminates rather than swallowing the
        # branch: deleting the ``Bash`` arm outright would pass the worktree case above.
        clone, _worktree = clone_and_live_worktree
        assert _run_gate(_bash("git commit -m 'new work'", str(clone))) is True

    def test_a_push_from_the_primary_clone_is_still_refused(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        clone, _worktree = clone_and_live_worktree
        assert _run_gate(_bash("git push origin HEAD", str(clone))) is True

    def test_a_read_only_git_in_the_primary_clone_is_still_allowed(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        clone, _worktree = clone_and_live_worktree
        assert _run_gate(_bash("git log --oneline -5", str(clone))) is False


class TestTheOverrideIsReachableFromACommitMessage:
    """The audited escape must be reachable from the calls most likely to need it.

    The scan window stopped at 512 characters, so a token placed in a commit message — the
    documented emergency shape, and the one call a truncating scanner is guaranteed to miss
    — was never seen, and the escape hatch did not exist for it.
    """

    @staticmethod
    def _long_message(tail: str) -> str:
        body = "restore the factory\n\n" + ("detail line for the commit message\n" * 40)
        assert len(body) > 512, "the body must clear the old window for this case to mean anything"
        return body + tail

    def test_a_token_beyond_the_old_window_is_seen(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        clone, _worktree = clone_and_live_worktree
        message = self._long_message("[headless-authoring-ok: the factory itself is down]")
        assert _run_gate(_bash(f"git commit -m '{message}'", str(clone))) is False

    def test_the_same_long_message_without_a_token_is_still_refused(
        self, headless_interactive: None, clone_and_live_worktree: tuple[Path, Path]
    ) -> None:
        clone, _worktree = clone_and_live_worktree
        message = self._long_message("ordinary work, no emergency")
        assert _run_gate(_bash(f"git commit -m '{message}'", str(clone))) is True


def _seeded_clone(path: Path) -> Path:
    """A primary clone carrying a committed ``src/`` dir — the shape the probe is built on."""
    repo = make_git_repo(path)
    (repo / "src").mkdir()
    (repo / "src" / "seed.py").write_text("x = 1\n", encoding="utf-8")
    run_git(repo, "add", "src/seed.py")
    run_git(repo, "commit", "-q", "-m", "seed")
    return repo


@pytest.fixture
def managed_and_unmanaged(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Two real clones, one teatree-MANAGED, under an engaged interactive headless session.

    Neither the posture nor the managed-repo classification is mocked: the control DB stores
    ``agent_runtime`` and an overlay registry whose ``path`` covers the managed clone, so both
    resolve exactly as they do in production. Which of the two the gate judges is the whole
    question these cases ask, and a mock of that dimension would answer it for them.
    """
    managed = _seeded_clone(tmp_path / "managed")
    unmanaged = _seeded_clone(tmp_path / "unmanaged")
    db = _write_config_db(
        tmp_path / "db.sqlite3",
        {"agent_runtime": "headless", "overlays": {"probe": {"path": str(managed)}}},
    )
    with (
        mock.patch.object(router, "STATE_DIR", tmp_path / "state"),
        mock.patch.object(router, "_teatree_engaged", return_value=True),
        mock.patch.object(gate, "_gate_enabled", return_value=True),
        mock.patch.dict(os.environ, {**_INTERACTIVE_ENV, "T3_CONFIG_DB": str(db), "T3_OVERLAY_NAME": ""}),
    ):
        yield managed, unmanaged


class TestTheBashArmJudgesTheRepoTheCommandTargets:
    """A ``Bash`` call is judged by the repo its write LANDS in, never by the session's cwd.

    The probe was the raw hook cwd, so a leading ``cd <dir> &&`` and a ``git -C <dir>`` were
    both discarded — and a session whose cwd happened to sit in a managed repo had every
    commit and push refused, to any repository at all. That is a session-wide ban, not the
    stated policy, which is about the repo being written to.
    """

    def test_a_push_to_an_unmanaged_repo_from_a_managed_cwd_is_allowed(
        self, managed_and_unmanaged: tuple[Path, Path]
    ) -> None:
        managed, unmanaged = managed_and_unmanaged
        assert _run_gate(_bash(f"cd {unmanaged} && git push origin HEAD", str(managed))) is False

    def test_a_dash_c_push_to_an_unmanaged_repo_from_a_managed_cwd_is_allowed(
        self, managed_and_unmanaged: tuple[Path, Path]
    ) -> None:
        managed, unmanaged = managed_and_unmanaged
        assert _run_gate(_bash(f"git -C {unmanaged} push origin HEAD", str(managed))) is False

    def test_a_bare_push_from_a_managed_cwd_is_still_refused(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        # The policy this fix must not weaken: with nothing redirecting it, the command does
        # land in the managed repo, and that is exactly what the gate exists to refuse.
        managed, _unmanaged = managed_and_unmanaged
        assert _run_gate(_bash("git push origin HEAD", str(managed))) is True

    def test_a_cd_into_a_managed_repo_from_an_unmanaged_cwd_is_refused(
        self, managed_and_unmanaged: tuple[Path, Path]
    ) -> None:
        # Resolution has to work in both directions, or it is just a differently-placed cwd.
        managed, unmanaged = managed_and_unmanaged
        assert _run_gate(_bash(f"cd {managed} && git commit -m 'new work'", str(unmanaged))) is True

    def test_a_cd_that_lands_nowhere_allows(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        # The ``cd`` itself would fail, so the command writes nowhere; an unresolvable target
        # is not evidence of authoring, and this gate allows on unknown.
        managed, _unmanaged = managed_and_unmanaged
        assert _run_gate(_bash(f"cd {managed}/gone && git push origin HEAD", str(managed))) is False


class TestAMentionOfAPushIsNotAPush:
    """The verb is detected on the quote/heredoc-stripped skeleton, as in every sibling gate.

    A read-only ``python - <<'PY'`` probe was refused because a string literal inside the
    heredoc body contained ``… && git push origin …`` and the ``&&`` satisfied the anchor.
    """

    def test_a_push_inside_a_heredoc_body_is_not_a_push(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        managed, _unmanaged = managed_and_unmanaged
        command = "python - <<'PY'\nprint('then run: cd repo && git push origin main')\nPY"
        assert _run_gate(_bash(command, str(managed))) is False

    def test_a_push_inside_a_quoted_argument_is_not_a_push(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        managed, _unmanaged = managed_and_unmanaged
        assert _run_gate(_bash("git log --grep 'ran && git push origin main'", str(managed))) is False

    def test_a_real_push_beside_that_text_is_still_refused(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        # The control that proves the strip narrows detection rather than removing it.
        managed, _unmanaged = managed_and_unmanaged
        assert _run_gate(_bash("echo 'about to && git push' && git push origin HEAD", str(managed))) is True


class TestTheOverrideMustLiveInTheCallsOwnText:
    """Where the token has to go — the refusal text is the only place an agent learns it.

    ``_call_text`` scans ``command``/``new_string``/``content``/``file_path``/``prompt``/
    ``new_source``. A ``description`` is not one of them, and two agents spent their attempts
    putting the token there.
    """

    def test_a_token_in_the_command_unblocks(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        managed, _unmanaged = managed_and_unmanaged
        command = "git push origin HEAD  # [headless-authoring-ok: the factory itself is down]"
        assert _run_gate(_bash(command, str(managed))) is False

    def test_a_token_only_in_the_description_does_not_unblock(self, managed_and_unmanaged: tuple[Path, Path]) -> None:
        data = _bash("git push origin HEAD", str(managed_and_unmanaged[0]))
        data["tool_input"]["description"] = "[headless-authoring-ok: the factory itself is down]"
        assert _run_gate(data) is True
