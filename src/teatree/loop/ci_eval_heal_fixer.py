"""Bounded, anti-cheat-gated autonomous fixer for the CI-eval self-healing loop (#3201 PR-3b).

PR-3a's observe loop can only dispatch a behavioral eval, poll, and GREEN or
HALT+escalate on any red — it NEVER writes a fix. This module is the PR-3b
follow-up: when a run comes back with a BEHAVIORAL red (not infra) AND the fixer
is armed, the driver dispatches a bounded autonomous fix through a
:class:`CiEvalHealFixer`.

Two guardrails are structural, not left to prose:

* **Both switches, or observe-only.** :func:`autofix_armed` is true ONLY when the
    ``ci_eval_heal_autofix_enabled`` DARK feature flag is on AND the ``ci_eval_heal``
    ``Loop`` row is enabled. Either off ⇒ the loop stays observe-only (a red HALTs
    and escalates exactly as PR-3a). Autonomous CI mutation needs a deliberate
    double opt-in.
* **The gate runs BEFORE the push.** A fixer only PROPOSES — it writes and commits
    a fix in a THROWAWAY worktree and returns the changed paths, but pushes NOTHING.
    The driver runs the #3282 anti-cheat gate (``record_fix``) over those paths and
    calls :meth:`~CiEvalHealFixer.publish` only when the gate passes, so a diff that
    edits a scenario (``evals/scenarios/**``) or a red matcher is REJECTED and the
    cheating commit is DISCARDED, never reaching the PR branch. A genuinely-failing
    eval can never be greened by editing its test.

The fix budget (``max_fix_attempts``, default 2 at open time) makes "un-greenable"
decidable: once exhausted, the driver HALTs and escalates rather than looping
forever. The single unstoppable external — the ``claude`` write turn — is injected
(:func:`_run_fix_turn`) so the worktree / commit / diff / push orchestration is
exercised for real under a tmp-path git repo, mocking only the LLM.
"""

import logging
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from teatree.utils.git_worktree import worktree_add_at_ref, worktree_remove
from teatree.utils.run import CommandFailedError, run_checked

if TYPE_CHECKING:
    from teatree.core.models import CiEvalHealSession

logger = logging.getLogger(__name__)

#: The loop whose ``Loop`` row must ALSO be enabled before the fixer arms — the
#: second of the two switches (:func:`autofix_armed`).
_LOOP_NAME = "ci_eval_heal"

#: The scenario tree and red-matcher paths the fixer is told, in-prompt, it may
#: NEVER touch. Mirrors the authority in
#: :mod:`teatree.core.gates.eval_heal_anticheat_gate` (which independently REJECTS
#: such a diff) — the prompt is defence-in-depth, the gate is the enforcement.
_FORBIDDEN_HINT = (
    "evals/scenarios/** (the scenario definitions) and the red matchers "
    "src/teatree/eval/matchers.py, triage.py, judge.py, matcher_vacuity.py"
)

#: Wall-clock bound on the whole write turn — a stalled ``claude`` spawn can never
#: wedge the loop tick. Mirrors the dream distiller's watchdog contract.
_FIX_TURN_WATCHDOG_SECONDS = 1800.0

#: The turn-runner seam: write a fix into *cwd* for the given prompt. Injected so
#: tests drive the git orchestration with a fake that edits a file, mocking only
#: the ``claude`` subprocess (the one unstoppable external).
TurnRunner = Callable[[str, Path], None]


@dataclass(frozen=True, slots=True)
class FixProposal:
    """A fix a fixer wrote and committed in a throwaway worktree — NOT yet pushed.

    ``changed_paths`` are repo-relative POSIX paths (``git diff --name-only``), the
    exact shape the anti-cheat gate classifies. The driver gates these BEFORE any
    publish; an empty tuple means the turn produced no change (the driver halts).
    """

    changed_paths: tuple[str, ...]
    worktree_path: str
    base_sha: str
    commit_sha: str = ""


class CiEvalHealFixer(Protocol):
    """The fixer seam the driver dispatches to — propose (gate here) then publish OR discard.

    A fixer NEVER pushes in :meth:`propose`; the driver runs the anti-cheat gate over
    the proposal's ``changed_paths`` and calls :meth:`publish` only on a clean gate,
    :meth:`discard` otherwise. Both terminal methods release the throwaway worktree.
    """

    def propose(self, session: "CiEvalHealSession") -> FixProposal:
        """Write + commit a fix for the session's reds in a throwaway worktree (no push)."""
        ...

    def publish(self, session: "CiEvalHealSession", proposal: FixProposal) -> str:
        """Push the gate-cleared fix to the PR branch; return the new head SHA. Releases the worktree."""
        ...

    def discard(self, proposal: FixProposal) -> None:
        """Drop an un-published (empty or gate-rejected) proposal; release the worktree."""
        ...


def autofix_armed(session: "CiEvalHealSession") -> bool:
    """True only when BOTH the DARK flag AND the ``ci_eval_heal`` loop row are on.

    Either switch off ⇒ observe-only (the caller HALTs + escalates a red exactly as
    PR-3a). The flag resolves per-overlay so an overlay can trial the fixer on its
    own budget; the loop-row check keeps even a by-hand ``t3 eval ci-heal advance``
    from mutating CI unless the operator deliberately enabled the autonomous loop.
    """
    from teatree.config.resolution import get_effective_settings  # noqa: PLC0415 — deferred: config resolve reaches DB
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM needs the app registry

    if not get_effective_settings(session.overlay or None).ci_eval_heal_autofix_enabled:
        return False
    return Loop.objects.filter(name=_LOOP_NAME, enabled=True).exists()


def build_fixer_prompt(session: "CiEvalHealSession") -> str:
    """The conservative, auditable instruction the autonomous fixer runs.

    Names the red scenarios, restates the fix-the-code-never-the-test rule and the
    forbidden paths, and asks for the MINIMAL product change plus a clear commit —
    or a no-op (which the driver reads as "un-fixable" and HALTs) if the only way to
    green the scenario would be to edit the test.
    """
    reds = ", ".join(session.red_scenarios) or "(none recorded)"
    return (
        "You are the CI-eval self-healing FIXER. A behavioral eval graded these scenarios RED on "
        f"PR branch {session.pr_ref!r}: {reds}.\n\n"
        "Fix the PRODUCT behaviour the scenario asserts — skill prose, hooks, or core code — with the "
        "SMALLEST change that makes the agent behave as the scenario expects. Then stage and commit it "
        "with a clear message naming the scenarios.\n\n"
        f"You may NEVER edit the eval TEST: {_FORBIDDEN_HINT}. Editing any of those would suppress the "
        "red without changing behaviour — it is rejected by an anti-cheat gate and the fix is discarded. "
        "If the ONLY way to make the scenario pass would be to change the test, make NO change and do not "
        "commit: that is a human decision to escalate, never a self-edit of the test.\n\n"
        "Be conservative and auditable: touch as few files as possible, add no unrelated changes, and "
        "leave the working tree clean apart from your single fix commit."
    )


def _run_fix_turn(prompt: str, cwd: Path) -> None:  # pragma: no cover
    """Run ONE bounded, WRITE-capable headless ``claude`` turn in *cwd* (the unstoppable external).

    The write twin of the dream distiller turn: the ``claude_code`` preset +
    ``bypassPermissions`` (a headless agent has no human to grant tool permissions),
    the credential child-env resolved in THIS sync frame (Django forbids the DB read
    inside the async turn), and one :func:`asyncio.timeout` bounding the WHOLE turn
    (connect + query + drain) so a stalled spawn can never wedge the loop. Raises when
    ``claude`` is unavailable or the turn fails, so a broken fixer surfaces loud
    rather than as a silent no-op the driver would misread as "un-fixable".
    """
    # The literal ``claude`` subprocess spawn is the one unstoppable external (a
    # third-party subprocess + an off-box LLM), never exercised in the suite; the
    # fixer's whole orchestration is tested with an injected ``turn_runner`` instead.
    import asyncio  # noqa: PLC0415 — deferred: loaded only on this code path
    import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

    from teatree.agents._headless_env import system_child_env  # noqa: PLC0415 — deferred: avoids the SDK-heavy runner

    if shutil.which("claude") is None:
        msg = "claude is not installed — the CI-eval heal fixer cannot run"
        raise RuntimeError(msg)
    env = system_child_env()
    asyncio.run(_drive_fix_turn(prompt, cwd=cwd, env=env))


async def _drive_fix_turn(prompt: str, *, cwd: Path, env: dict[str, str] | None) -> None:  # pragma: no cover
    import asyncio  # noqa: PLC0415 — deferred: loaded only on this code path

    from claude_agent_sdk import (  # noqa: PLC0415 — deferred: optional heavy SDK dep, imported only at turn time
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )
    from claude_agent_sdk.types import SystemPromptPreset  # noqa: PLC0415 — deferred: optional heavy SDK dep

    options = ClaudeAgentOptions(
        system_prompt=SystemPromptPreset(type="preset", preset="claude_code", append=prompt),
        cwd=str(cwd),
        add_dirs=[str(cwd)],
        permission_mode="bypassPermissions",
        disallowed_tools=["AskUserQuestion"],
        max_turns=0,
    )
    if env is not None:
        options.env = env
    async with asyncio.timeout(_FIX_TURN_WATCHDOG_SECONDS), ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for _message in client.receive_response():
            pass


@dataclass(slots=True)
class _HeadlessFixer:
    """The default fixer: a bounded headless write turn in a throwaway worktree of the PR branch.

    ``propose`` fetches the branch, adds a detached worktree at its tip, runs the
    write turn, then stages + commits and returns the diff (NO push). ``publish``
    pushes that commit to the branch; ``discard`` drops it. Both release the
    worktree. The ``claude`` turn is the injected :attr:`turn_runner`; every other
    step is real git, so the orchestration is testable under a tmp-path repo.
    """

    repo: str = "."
    remote: str = "origin"
    turn_runner: TurnRunner = _run_fix_turn
    worktree_root: str = ""

    def propose(self, session: "CiEvalHealSession") -> FixProposal:
        self._fetch(session.pr_ref)
        wt_path = self._new_worktree_path()
        base_sha = self._branch_tip(session.pr_ref)
        if not worktree_add_at_ref(self.repo, wt_path, base_sha):
            msg = f"could not create fix worktree for {session.pr_ref!r} at {base_sha[:12]}"
            raise RuntimeError(msg)
        try:
            self.turn_runner(build_fixer_prompt(session), Path(wt_path))
            changed, commit_sha = self._commit(wt_path, base_sha)
        except Exception:
            worktree_remove(self.repo, wt_path)
            raise
        return FixProposal(changed_paths=changed, worktree_path=wt_path, base_sha=base_sha, commit_sha=commit_sha)

    def publish(self, session: "CiEvalHealSession", proposal: FixProposal) -> str:
        try:
            run_checked(
                ["git", "push", self.remote, f"{proposal.commit_sha}:refs/heads/{session.pr_ref}"],
                cwd=self.repo,
            )
            return self._branch_tip(session.pr_ref, remote=True) or proposal.commit_sha
        finally:
            worktree_remove(self.repo, proposal.worktree_path)

    def discard(self, proposal: FixProposal) -> None:
        worktree_remove(self.repo, proposal.worktree_path)

    def _fetch(self, branch: str) -> None:
        try:
            run_checked(["git", "fetch", self.remote, branch], cwd=self.repo)
        except CommandFailedError as exc:
            logger.warning("ci_eval_heal fixer: fetch %s failed, using local ref: %s", branch, exc)

    def _branch_tip(self, branch: str, *, remote: bool = False) -> str:
        ref = f"{self.remote}/{branch}" if remote else branch
        try:
            return run_checked(["git", "rev-parse", ref], cwd=self.repo).stdout.strip()
        except CommandFailedError:
            return run_checked(["git", "rev-parse", branch], cwd=self.repo).stdout.strip()

    def _new_worktree_path(self) -> str:
        """A UNIQUE, not-yet-existing path (``git worktree add`` refuses an existing dir)."""
        root = self.worktree_root or tempfile.gettempdir()
        return str(Path(root) / f"ci-eval-heal-fix-{uuid.uuid4().hex}")

    @staticmethod
    def _commit(wt_path: str, base_sha: str) -> tuple[tuple[str, ...], str]:
        run_checked(["git", "add", "-A"], cwd=wt_path)
        status = run_checked(["git", "status", "--porcelain"], cwd=wt_path).stdout.strip()
        if not status:
            return (), ""
        run_checked(["git", "commit", "-m", "fix: CI-eval self-heal autonomous fix"], cwd=wt_path)
        changed = run_checked(["git", "diff", "--name-only", f"{base_sha}..HEAD"], cwd=wt_path).stdout.split()
        commit_sha = run_checked(["git", "rev-parse", "HEAD"], cwd=wt_path).stdout.strip()
        return tuple(changed), commit_sha


def default_fixer() -> CiEvalHealFixer:
    """The production fixer — a headless write turn against the current repo checkout."""
    return _HeadlessFixer()


__all__ = [
    "CiEvalHealFixer",
    "FixProposal",
    "TurnRunner",
    "autofix_armed",
    "build_fixer_prompt",
    "default_fixer",
]
