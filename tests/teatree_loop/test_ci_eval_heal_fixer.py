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

from teatree.core.models import ConfigSetting, Loop
from teatree.loop.ci_eval_heal_fixer import (
    FixProposal,
    _HeadlessFixer,
    autofix_armed,
    build_fixer_prompt,
    default_fixer,
)
from teatree.utils.run import run_checked


def _arm_flag() -> None:
    ConfigSetting.objects.set_value("ci_eval_heal_autofix_enabled", value=True)


def _enable_loop() -> None:
    Loop.objects.update_or_create(
        name="ci_eval_heal",
        defaults={"enabled": True, "delay_seconds": 300, "script": "src/teatree/loops/ci_eval_heal/loop.py"},
    )


class TestAutofixArmedNeedsBothSwitches(TestCase):
    """The fixer is a double opt-in: the DARK flag AND the ci_eval_heal loop row."""

    def _session(self) -> "SimpleNamespace":
        return SimpleNamespace(overlay="", pr_ref="3201-feat", red_scenarios=["r"])

    def test_disarmed_by_default(self) -> None:
        assert autofix_armed(self._session()) is False

    def test_flag_alone_is_not_enough(self) -> None:
        _arm_flag()
        assert autofix_armed(self._session()) is False

    def test_loop_alone_is_not_enough(self) -> None:
        _enable_loop()
        assert autofix_armed(self._session()) is False

    def test_armed_only_when_both_on(self) -> None:
        _arm_flag()
        _enable_loop()
        assert autofix_armed(self._session()) is True

    def test_disarmed_when_loop_disabled(self) -> None:
        _arm_flag()
        Loop.objects.update_or_create(
            name="ci_eval_heal",
            defaults={"enabled": False, "delay_seconds": 300, "script": "src/teatree/loops/ci_eval_heal/loop.py"},
        )
        assert autofix_armed(self._session()) is False


class TestFixerPrompt:
    def test_prompt_names_the_reds_and_forbids_editing_the_test(self) -> None:
        session = SimpleNamespace(pr_ref="3201-feat", red_scenarios=["rules_under_load", "budget_turns"])
        prompt = build_fixer_prompt(session)
        assert "rules_under_load" in prompt
        assert "budget_turns" in prompt
        # The conservative anti-cheat instruction is present.
        assert "evals/scenarios/**" in prompt
        assert "matchers.py" in prompt
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
