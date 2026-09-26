"""Bounded, anti-cheat-gated CI-eval heal fixer (#3201 PR-3b).

Two guardrails are asserted red-first here: the fixer arms ONLY when both switches
are on (the DARK flag AND the loop row), and it PROPOSES without pushing — the
production ``_HeadlessFixer`` writes and commits a fix in a throwaway worktree but
never publishes it, so the driver can run the anti-cheat gate BEFORE any push. The
one unstoppable external (the ``claude`` write turn) is injected, so the real git
worktree / commit / diff / push orchestration runs under a tmp-path repo.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import TestCase

from teatree.agents.compaction_guard import COMPACTION_BLOCKED_REASON
from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride
from teatree.loop.ci_eval_heal_fixer import (
    SALVAGE_REF_PREFIX,
    FixProposal,
    _HeadlessFixer,
    autofix_armed,
    build_fixer_prompt,
    default_fixer,
)
from teatree.utils.run import run_checked


def _arm_flag() -> None:
    ConfigSetting.objects.set_value("ci_eval_heal_autofix_enabled", value=True)


def _admit_loop() -> None:
    Mode.objects.update_or_create(name="quiet", defaults={"entries": {"ci_eval_heal": True}})


class TestAutofixArmedNeedsBothSwitches(TestCase):
    """The fixer is a double opt-in: the DARK flag AND the loop's own run verdict."""

    def setUp(self) -> None:
        # A real preset has to GOVERN, or resolution fails open and admits every loop —
        # which would make every "the loop is off" case here unable to fail.
        Loop.objects.update_or_create(
            name="ci_eval_heal",
            defaults={"delay_seconds": 300, "script": "src/teatree/loops/ci_eval_heal/loop.py"},
        )
        Mode.objects.create(name="quiet", entries={"ci_eval_heal": False})
        ModeOverride.objects.set_override("quiet", reason="test posture")

    def _session(self) -> "SimpleNamespace":
        return SimpleNamespace(overlay="", pr_ref="3201-feat", red_scenarios=["r"])

    def test_disarmed_by_default(self) -> None:
        assert autofix_armed(self._session()) is False

    def test_flag_alone_is_not_enough(self) -> None:
        _arm_flag()
        assert autofix_armed(self._session()) is False

    def test_loop_alone_is_not_enough(self) -> None:
        _admit_loop()
        assert autofix_armed(self._session()) is False

    def test_armed_only_when_both_on(self) -> None:
        _arm_flag()
        _admit_loop()
        assert autofix_armed(self._session()) is True

    def test_a_manual_override_off_disarms_a_preset_admitted_loop(self) -> None:
        _arm_flag()
        _admit_loop()
        Loop.objects.set_manual_override("ci_eval_heal", runs=False, reason="the fixer is misbehaving")
        assert autofix_armed(self._session()) is False

    def test_an_absent_loop_row_disarms(self) -> None:
        _arm_flag()
        _admit_loop()
        Loop.objects.filter(name="ci_eval_heal").delete()
        assert autofix_armed(self._session()) is False


class TestFixerPrompt:
    def test_prompt_names_the_reds_and_forbids_editing_the_test(self) -> None:
        session = SimpleNamespace(pr_ref="3201-feat", red_scenarios=["rules_under_load", "budget_turns"])
        prompt = build_fixer_prompt(session)
        assert "rules_under_load" in prompt
        assert "budget_turns" in prompt
        # The conservative anti-cheat instruction is present, and names the WHOLE
        # harness — the pre-#4220 hint listed four matchers and left report.py, the
        # module computing the verdict, reading as a legal fix target.
        assert "evals/scenarios/**" in prompt
        assert "src/teatree/eval/**" in prompt
        assert "report.py" in prompt
        assert "make NO change" in prompt

    def test_default_fixer_is_a_headless_fixer(self) -> None:
        assert isinstance(default_fixer(), _HeadlessFixer)


def _git(repo: str, *args: str) -> str:
    return run_checked(["git", *args], cwd=repo).stdout.strip()


def _seed_repo(tmp_path: Path, *, branch: str = "pr-branch") -> tuple[str, str]:
    """A bare origin + a working clone with *branch* pushed. Returns (work_repo, origin)."""
    origin = str(tmp_path / "origin.git")
    work = str(tmp_path / "work")
    run_checked(["git", "init", "--bare", "-b", "main", origin])
    run_checked(["git", "clone", origin, work], cwd=str(tmp_path))
    _git(work, "config", "user.email", "t@e")
    _git(work, "config", "user.name", "t")
    (Path(work) / "product.txt").write_text("v1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "branch", branch)
    _git(work, "push", "origin", "main", branch)
    return work, origin


def _salvage_ref(message: str) -> str:
    """The recovery ref the failure names, or ``""`` when it names none."""
    return next((word.rstrip(";.,") for word in message.split() if word.startswith(SALVAGE_REF_PREFIX)), "")


def _fake_session(branch: str = "pr-branch") -> "SimpleNamespace":
    return SimpleNamespace(overlay="", pr_ref=branch, red_scenarios=["rules_under_load"])


class TestHeadlessFixerProposeGatePublish:
    """The production fixer: propose (no push) → publish/discard, with a fake write turn."""

    def test_propose_commits_the_turn_edit_and_returns_the_diff_unpushed(self, tmp_path: Path) -> None:
        work, origin = _seed_repo(tmp_path)

        def turn(_prompt: str, cwd: Path) -> None:
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        proposal = fixer.propose(_fake_session())
        assert proposal.changed_paths == ("product.txt",)
        assert proposal.commit_sha
        # The fix is committed in the throwaway worktree but NOT pushed to origin.
        origin_tip = _git(work, "ls-remote", origin, "refs/heads/pr-branch").split()[0]
        assert origin_tip != proposal.commit_sha
        fixer.discard(proposal)
        assert not Path(proposal.worktree_path).exists()

    def test_propose_reports_no_change_when_the_turn_edits_nothing(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)

        def turn(_prompt: str, _cwd: Path) -> None:
            pass  # the turn declined to change anything (un-fixable without editing the test)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        proposal = fixer.propose(_fake_session())
        assert proposal.changed_paths == ()
        assert proposal.commit_sha == ""

    def test_publish_pushes_the_vetted_fix_to_the_branch(self, tmp_path: Path) -> None:
        work, origin = _seed_repo(tmp_path)

        def turn(_prompt: str, cwd: Path) -> None:
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        session = _fake_session()
        proposal = fixer.propose(session)
        head = fixer.publish(session, proposal)
        origin_tip = _git(work, "ls-remote", origin, "refs/heads/pr-branch").split()[0]
        assert origin_tip == proposal.commit_sha
        assert head == proposal.commit_sha
        assert not Path(proposal.worktree_path).exists()

    def test_propose_tolerates_a_failing_fetch_and_uses_the_local_ref(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)

        def turn(_prompt: str, cwd: Path) -> None:
            (cwd / "product.txt").write_text("v2\n", encoding="utf-8")

        # A remote that cannot be fetched: the swallowed fetch falls back to the local ref.
        fixer = _HeadlessFixer(repo=work, remote="no-such-remote", turn_runner=turn, worktree_root=str(tmp_path))
        proposal = fixer.propose(_fake_session())
        assert proposal.changed_paths == ("product.txt",)
        fixer.discard(proposal)

    def test_branch_tip_falls_back_to_the_local_ref_without_a_tracking_ref(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)
        _git(work, "branch", "orphan")  # local only — no origin/orphan tracking ref
        fixer = _HeadlessFixer(repo=work, worktree_root=str(tmp_path))
        local_tip = _git(work, "rev-parse", "orphan")
        assert fixer._branch_tip("orphan", remote=True) == local_tip

    def test_propose_raises_when_the_worktree_cannot_be_created(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)
        fixer = _HeadlessFixer(repo=work, turn_runner=lambda _p, _c: None, worktree_root=str(tmp_path))
        # A ref that does not resolve — worktree add cannot materialise it.
        session = _fake_session(branch="does-not-exist")
        with pytest.raises(Exception):  # noqa: B017, PT011 — any git/lookup failure is a hard stop
            fixer.propose(session)

    def test_a_stopped_turns_fix_survives_the_worktree_it_is_removed_with(self, tmp_path: Path) -> None:
        # A turn stopped mid-flight (a blocked auto-compaction, a timeout, a full context
        # window) may already have written a valid fix. ``propose`` force-removes the
        # worktree on the way out, so the work has to be pinned somewhere first.
        work, _ = _seed_repo(tmp_path)
        created: list[str] = []

        def turn(_prompt: str, cwd: Path) -> None:
            created.append(str(cwd))
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            raise RuntimeError(COMPACTION_BLOCKED_REASON)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        assert not Path(created[0]).exists()
        ref = _salvage_ref(str(raised.value))
        assert ref, f"the failure names no recovery ref: {raised.value}"
        recovered = run_checked(["git", "show", f"{ref}:product.txt"], cwd=work).stdout
        assert recovered.strip() == "v2-fixed"

    def test_a_turn_the_watchdog_stops_keeps_the_fix_it_had_committed(self, tmp_path: Path) -> None:
        # Every early stop takes the same force-remove path: the watchdog timeout and a full
        # context window discard the work exactly as a blocked compaction would, and a turn
        # that committed its own fix leaves a clean tree the commit step reports as no change.
        work, _ = _seed_repo(tmp_path)

        def turn(_prompt: str, cwd: Path) -> None:
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            run_checked(["git", "add", "-A"], cwd=str(cwd))
            run_checked(["git", "commit", "-m", "the turn's own fix"], cwd=str(cwd))
            raise TimeoutError

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        ref = _salvage_ref(str(raised.value))
        assert ref, f"the failure names no recovery ref: {raised.value}"
        assert run_checked(["git", "show", f"{ref}:product.txt"], cwd=work).stdout.strip() == "v2-fixed"

    def test_work_that_cannot_be_committed_keeps_its_worktree(self, tmp_path: Path) -> None:
        # A commit that fails (a hook, a full disk) leaves the ONLY copy of the work in the
        # worktree, so removing it is the data loss the salvage exists to prevent.
        work, _ = _seed_repo(tmp_path)
        hook = Path(work) / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        created: list[str] = []

        def turn(_prompt: str, cwd: Path) -> None:
            created.append(str(cwd))
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            raise RuntimeError(COMPACTION_BLOCKED_REASON)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        kept = Path(created[0])
        assert kept.exists(), "the worktree holding the only copy of the work was removed"
        assert (kept / "product.txt").read_text(encoding="utf-8").strip() == "v2-fixed"
        assert str(kept) in str(raised.value)

    def test_work_whose_ref_cannot_be_written_keeps_its_worktree(self, tmp_path: Path) -> None:
        # ``update-ref`` cannot nest a ref under one that already exists as a ref itself, so
        # this is a real write failure: the commit exists but nothing points at it.
        work, _ = _seed_repo(tmp_path)
        base = run_checked(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
        run_checked(["git", "update-ref", SALVAGE_REF_PREFIX.rstrip("/"), base], cwd=work)
        created: list[str] = []

        def turn(_prompt: str, cwd: Path) -> None:
            created.append(str(cwd))
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            raise RuntimeError(COMPACTION_BLOCKED_REASON)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        kept = Path(created[0])
        assert kept.exists(), "the worktree was removed while no ref points at the salvaged commit"
        assert str(kept) in str(raised.value)

    def test_a_commit_the_turn_reset_away_is_still_recoverable(self, tmp_path: Path) -> None:
        # A turn that commits, then resets back to the base while revising, leaves that commit
        # reachable ONLY through the linked worktree's HEAD reflog — which removing the
        # worktree deletes. The tree is clean at the base, so nothing else reports the work.
        work, _ = _seed_repo(tmp_path)

        def turn(_prompt: str, cwd: Path) -> None:
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            run_checked(["git", "add", "-A"], cwd=str(cwd))
            run_checked(["git", "commit", "-m", "the turn's first attempt"], cwd=str(cwd))
            run_checked(["git", "reset", "--hard", "HEAD^"], cwd=str(cwd))
            raise RuntimeError(COMPACTION_BLOCKED_REASON)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        ref = _salvage_ref(str(raised.value))
        assert ref, f"the reset-away commit is named nowhere: {raised.value}"
        assert run_checked(["git", "show", f"{ref}:product.txt"], cwd=work).stdout.strip() == "v2-fixed"

    def test_an_unusable_reflog_keeps_the_worktree_rather_than_guessing(self, tmp_path: Path) -> None:
        # ``git reflog show`` exits 0 with no entries when the worktree's HEAD reflog is absent
        # (reflog creation disabled), so an empty read is not evidence the turn kept nothing.
        work, _ = _seed_repo(tmp_path)
        run_checked(["git", "config", "core.logAllRefUpdates", "false"], cwd=work)
        created: list[str] = []

        def turn(_prompt: str, cwd: Path) -> None:
            created.append(str(cwd))
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            run_checked(["git", "add", "-A"], cwd=str(cwd))
            run_checked(["git", "commit", "-m", "the turn's first attempt"], cwd=str(cwd))
            run_checked(["git", "reset", "--hard", "HEAD^"], cwd=str(cwd))
            raise RuntimeError(COMPACTION_BLOCKED_REASON)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        kept = Path(created[0])
        assert kept.exists(), "the worktree was removed on an unreadable reflog"
        assert str(kept) in str(raised.value)

    def test_a_stopped_turn_that_wrote_nothing_names_no_recovery_ref(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)

        def turn(_prompt: str, _cwd: Path) -> None:
            raise RuntimeError(COMPACTION_BLOCKED_REASON)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            fixer.propose(_fake_session())

        assert _salvage_ref(str(raised.value)) == ""

    def test_propose_cleans_up_the_worktree_when_the_turn_raises(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)
        created: list[str] = []

        def turn(_prompt: str, cwd: Path) -> None:
            created.append(str(cwd))
            msg = "turn boom"
            raise RuntimeError(msg)

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        with pytest.raises(RuntimeError):
            fixer.propose(_fake_session())
        assert created
        assert not Path(created[0]).exists()


def test_fix_proposal_is_frozen() -> None:
    proposal = FixProposal(changed_paths=("a.py",), worktree_path="/tmp/x", base_sha="b", commit_sha="c")
    assert proposal.changed_paths == ("a.py",)
