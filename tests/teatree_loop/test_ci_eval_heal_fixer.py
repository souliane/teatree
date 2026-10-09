"""Bounded, anti-cheat-gated CI-eval heal fixer (#3201 PR-3b).

Two guardrails are asserted here: the fixer runs only for a confirmed red within
the spend budget, and it PROPOSES without pushing — the
production ``_HeadlessFixer`` writes and commits a fix in a throwaway worktree but
never publishes it, so the driver can run the anti-cheat gate BEFORE any push. The
one unstoppable external (the ``claude`` write turn) is injected, so the real git
worktree / commit / diff / push orchestration runs under a tmp-path repo.
"""

import ast
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.loop.ci_eval_heal_fixer as fixer_module
from teatree.agents.compaction_guard import COMPACTION_BLOCKED_REASON
from teatree.core.models import CiEvalHealSession, Loop
from teatree.loop.ci_eval_heal_advance import advance_session
from teatree.loop.ci_eval_heal_fixer import (
    SALVAGE_REF_PREFIX,
    FixProposal,
    _HeadlessFixer,
    build_fixer_prompt,
    default_fixer,
)
from teatree.utils.run import run_checked


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

    def test_agent_commit_is_folded_into_one_fixer_commit(self, tmp_path: Path) -> None:
        work, _ = _seed_repo(tmp_path)

        def turn(_prompt: str, cwd: Path) -> None:
            (cwd / "product.txt").write_text("v2-fixed\n", encoding="utf-8")
            _git(str(cwd), "add", "product.txt")
            _git(str(cwd), "commit", "-m", "agent committed despite instruction")

        fixer = _HeadlessFixer(repo=work, turn_runner=turn, worktree_root=str(tmp_path))
        session = _fake_session()
        proposal = fixer.propose(session)
        assert proposal.changed_paths == ("product.txt",)
        fixer.publish(session, proposal)
        assert _git(work, "rev-list", "--count", "main..origin/pr-branch") == "1"

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


class TestHarnessDispatchAtAdvancerEntry(TestCase):
    """A confirmed red reaches the configured harness and the real PR branch gate."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)
        Loop.objects.update_or_create(
            name="ci_eval_heal", defaults={"delay_seconds": 300, "script": "src/teatree/loops/ci_eval_heal/loop.py"}
        )
        self.session = CiEvalHealSession.objects.create(overlay="", pr_ref="pr-branch")
        self.session.trigger(ci_run_id="run-1", head_sha="a" * 40)
        self.session.save()
        self.session.receive_result(red_scenarios=["rules_under_load"])
        self.session.save()

    def _dispatch(self, tmp_path: Path, changed_path: str) -> tuple[str, str, object]:
        work, origin = _seed_repo(tmp_path)
        opened: list[str] = []

        class FakeSession:
            async def query(self, prompt: str) -> None:
                opened.append(prompt)
                target = Path(self.cwd) / changed_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixed\n", encoding="utf-8")

            async def interrupt(self) -> None:
                return None

            async def receive_response(self):
                if False:
                    yield None

        class FakeHarness:
            capabilities = SimpleNamespace(spawns_cli_child=False)

            @asynccontextmanager
            async def open(self, options):
                session = FakeSession()
                session.cwd = options.cwd
                yield session

        class FakeClient:
            def trigger_workflow(self, workflow, *, ref, inputs):
                return None

        with patch("teatree.agents.write_turn.resolve_harness", return_value=FakeHarness()):
            outcome = advance_session(
                self.session,
                client=FakeClient(),
                escalate=lambda _session: None,
                fixer=_HeadlessFixer(repo=work, worktree_root=str(tmp_path)),
            )
        assert len(opened) == 1
        return work, origin, outcome

    def test_one_red_dispatch_creates_one_fix_commit_on_pr_branch(self) -> None:
        work, _origin, outcome = self._dispatch(self.tmp_path, "product.txt")
        self.session.refresh_from_db()
        assert outcome.to_state == self.session.State.AWAITING_CI
        assert self.session.fix_attempts == 1
        assert _git(work, "rev-list", "--count", "main..origin/pr-branch") == "1"

    def test_anticheat_violation_discards_commit_and_halts(self) -> None:
        work, _origin, outcome = self._dispatch(self.tmp_path, "evals/scenarios/rules.yaml")
        self.session.refresh_from_db()
        assert outcome.to_state == self.session.State.HALTED
        assert self.session.fix_attempts == 0
        assert _git(work, "rev-list", "--count", "main..origin/pr-branch") == "0"


def test_fixer_module_has_no_direct_sdk_import() -> None:
    tree = ast.parse(Path(fixer_module.__file__).read_text(encoding="utf-8"))
    modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert all(not name.startswith("claude_agent_sdk") for name in modules if name)
