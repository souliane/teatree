"""Auto t3-update scanner — pull teatree+overlays current per tick (#1249).

The editable-installed teatree clone (resolved via ``T3_REPO`` / the
``uv`` tool receipt) and every registered overlay clone drift behind
``origin/<default-branch>`` until a human runs ``t3 update``. This
scanner closes the loop: every tick, for each configured clone, it
checks the cadence gate, then — if the working tree is clean, on the
default branch, AND the default branch's CI is green — fast-forwards the
clone to its tracking branch.

The scanner deliberately does **not** reinstall the editable install or
run ``t3 setup`` inline: those mutate the running interpreter and would
steal the foreground mid-tick. Instead, when ``auto_update_reinstall``
is enabled and a clone actually advances, the scanner upserts a
:class:`teatree.core.models.pending_reinstall.PendingReinstall` row; the
next per-tick subprocess drains it in a clean process before any scanner
imports (:mod:`teatree.loop.self_update_reinstall`). With the flag off
(the default) the scanner's contract is unchanged — a ``git pull
--ff-only`` per repo, no more.

Decision ladder per repo (per tick):

1. cadence elapsed since last pull? → skip (``cadence_not_elapsed``)
2. repo path missing on disk? → ``failed`` (logged + signal emitted)
3. ``git fetch origin`` fails? → ``failed``
4. on a non-default branch? → ``skipped`` (``branch=<name>``)
5. tracked-dirty working tree? → ``skipped`` (``dirty_tracked``)
6. origin not ahead of HEAD? → ``up_to_date`` (CI is NOT queried)
7. ``require_green_main`` and default-branch CI not green? → ``skipped``
    (``ci_red`` / ``ci_pending`` / ``ci_unknown`` — fail closed: only
    an explicit green proceeds)
8. ``git pull --ff-only`` advances HEAD? → ``updated`` (+ deferred
    reinstall row when ``auto_update_reinstall`` is on)

The post-pass :class:`SelfUpdateMarker` row records the outcome + the
new HEAD SHA so the cadence gate can short-circuit cheaply on the next
tick without re-shelling git.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.self_update_ci import CiVerdict, MainCiStatus
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

logger = logging.getLogger(__name__)

_CI_SKIP_REASON: dict[CiVerdict, str] = {
    CiVerdict.RED: "ci_red",
    CiVerdict.PENDING: "ci_pending",
    CiVerdict.UNKNOWN: "ci_unknown",
}

# The off-default skip reason is a structured ``branch=<current>!=<default>``
# string carried on the outcome + persisted marker. Construction and parsing
# share these two helpers so a format change cannot silently desync the two
# sites (the constructor in ``_pre_pull_gate`` and the parser in
# ``_maybe_notify_stale_clone``) — the recurrence #5 guards against.
_OFF_DEFAULT_REASON_PREFIX = "branch="
_OFF_DEFAULT_REASON_SEP = "!="


def _off_default_reason(current: str, default_branch: str) -> str:
    """Build the off-default skip reason ``branch=<current>!=<default>``."""
    return f"{_OFF_DEFAULT_REASON_PREFIX}{current}{_OFF_DEFAULT_REASON_SEP}{default_branch}"


def _parse_off_default_branch(reason: str) -> str:
    """Extract the default branch from an off-default reason, ``""`` if it doesn't match.

    Defensive: returns ``""`` unless *reason* is the exact
    ``branch=<current>!=<default>`` shape :func:`_off_default_reason` produces,
    so a format drift degrades to an empty default branch only when the shape
    genuinely no longer matches — and the paired round-trip test goes red the
    moment construction and parsing disagree.
    """
    head, sep, default_branch = reason.partition(_OFF_DEFAULT_REASON_SEP)
    if sep and head.startswith(_OFF_DEFAULT_REASON_PREFIX):
        return default_branch
    return ""


@dataclass(frozen=True, slots=True)
class _PullOutcome:
    """Internal record of one repo's pass through the decision ladder."""

    outcome: str  # "updated" | "up_to_date" | "skipped" | "failed" | "cadence_not_elapsed"
    reason: str = ""
    old_sha: str = ""
    new_sha: str = ""


@dataclass(slots=True)
class SelfUpdateScanner:
    """Fast-forward each configured editable clone to ``origin/<default>``.

    *repos* is an ordered list of ``(label, path)`` pairs; *label* is the
    stable identity used both for the persisted :class:`SelfUpdateMarker`
    row and for the emitted signal payloads. *cadence_hours* gates how
    often the scanner is allowed to issue git operations against a given
    clone — it is decoupled from the loop tick cadence so a sub-minute
    tick doesn't degenerate into sub-minute git fetches.

    The scanner is a stateless pure-Python object; all persistence is
    in :class:`SelfUpdateMarker`, all logging goes through ``logger`` so
    the tick-orchestrator's pipeline picks it up without special-casing.

    *ci_status* is the injectable default-branch CI verdict source. When
    *require_green_main* is true (the default — fail closed) the scanner
    queries it once origin is detected ahead of HEAD and refuses the pull
    unless the verdict is an explicit :attr:`CiVerdict.GREEN`. A missing
    *ci_status* with *require_green_main* true is treated as ``ci_unknown``
    (still fail closed). Set *require_green_main* false for back-compat /
    a clone whose default branch has no CI.

    *auto_update_reinstall* opts into the deferred-reinstall queue: on an
    actual ``updated`` outcome the scanner upserts a
    :class:`teatree.core.models.pending_reinstall.PendingReinstall` row so
    the next per-tick subprocess re-anchors the running interpreter in a
    clean process. Off by default — the genuinely new side-effect on the
    running orchestrator is never enabled silently.
    """

    repos: tuple[tuple[str, Path], ...] = ()
    cadence_hours: int = 1
    name: str = "self_update"
    ci_status: MainCiStatus | None = None
    require_green_main: bool = True
    auto_update_reinstall: bool = False

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for label, path in self.repos:
            outcome = self._process_one(label=label, path=path)
            _maybe_notify_stale_clone(label=label, path=path, outcome=outcome)
            signals.append(_signal_from_outcome(label=label, outcome=outcome))
            logger.info(
                "self_update %s outcome=%s reason=%s",
                label,
                outcome.outcome,
                outcome.reason,
            )
        return signals

    def _process_one(self, *, label: str, path: Path) -> _PullOutcome:
        if self._cadence_blocks(label=label):
            return _PullOutcome(outcome="cadence_not_elapsed", reason="recent_marker")
        if not path.is_dir():
            return _record_marker(
                label=label,
                path=path,
                outcome=_PullOutcome(outcome="failed", reason=f"repo_path_missing:{path}"),
            )
        outcome = _attempt_pull(repo=path, ci_gate=self._ci_gate)
        if outcome.outcome == "updated" and self.auto_update_reinstall:
            _queue_reinstall(label=label, target_sha=outcome.new_sha)
        return _record_marker(label=label, path=path, outcome=outcome)

    def _ci_gate(self, repo: Path) -> CiVerdict | None:
        """Resolve the default-branch CI verdict, or ``None`` when the gate is off.

        Returns ``None`` when *require_green_main* is false (the pull is
        not CI-gated). Otherwise returns the injected source's verdict, or
        :attr:`CiVerdict.UNKNOWN` when no source is configured — both keep
        the gate fail-closed (only an explicit green proceeds).
        """
        if not self.require_green_main:
            return None
        if self.ci_status is None:
            return CiVerdict.UNKNOWN
        return self.ci_status.verdict(repo=repo)

    def _cadence_blocks(self, *, label: str) -> bool:
        """Return True iff a recent enough marker for *label* exists."""
        # Import inside the method so the scanner module imports cleanly even
        # when Django app loading hasn't run yet (the wiring layer imports
        # this class at module load time).
        from teatree.core.models.self_update_marker import SelfUpdateMarker  # noqa: PLC0415 — lazy ORM import

        marker = SelfUpdateMarker.objects.filter(repo_label=label).first()
        if marker is None:
            return False
        elapsed_hours = (timezone.now() - marker.last_pull_at).total_seconds() / 3600.0
        return elapsed_hours < self.cadence_hours


def _maybe_notify_stale_clone(*, label: str, path: Path, outcome: _PullOutcome) -> None:
    """Emit a durable notice when the clone was skipped as dirty / off-default (#2836).

    Only the silently-stale skip classes notify: ``dirty_tracked`` and the
    off-default ``branch=…`` reason (which includes a detached HEAD —
    ``_current_branch`` returns ``HEAD`` when detached). CI-gated and
    no-origin skips are expected waits, not a clone the operator must fix, so
    they stay log-only. The notice is idempotent per (clone, reason, HEAD), so a
    persistent skip is surfaced once rather than every tick.
    """
    if outcome.outcome != "skipped":
        return
    from teatree.core.worktree.stale_clone_notice import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        StaleCloneReason,
        StaleCloneSkip,
        notify_stale_clone_skip,
    )

    reason = outcome.reason
    if reason.startswith("dirty_tracked"):
        kind, default_branch = StaleCloneReason.DIRTY, ""
    elif reason.startswith(_OFF_DEFAULT_REASON_PREFIX):
        kind = StaleCloneReason.OFF_DEFAULT
        default_branch = _parse_off_default_branch(reason)
    else:
        return
    notify_stale_clone_skip(
        StaleCloneSkip(
            label=label,
            repo_path=str(path),
            reason=kind,
            head_sha=outcome.old_sha,
            default_branch=default_branch,
            detail=reason,
        )
    )


def _record_marker(*, label: str, path: Path, outcome: _PullOutcome) -> _PullOutcome:
    """Upsert the :class:`SelfUpdateMarker` row + return *outcome*."""
    from teatree.core.models.self_update_marker import SelfUpdateMarker  # noqa: PLC0415 — deferred: ORM/app-registry

    sha = outcome.new_sha or outcome.old_sha
    try:
        with transaction.atomic():
            SelfUpdateMarker.objects.update_or_create(
                repo_label=label,
                defaults={
                    "repo_path": str(path),
                    "last_outcome": outcome.outcome,
                    "last_reason": outcome.reason[:200],
                    "last_pulled_sha": sha,
                    "last_pull_at": timezone.now(),
                },
            )
    except Exception:
        # Persisting the marker must never crash the tick — log and
        # let the scanner emit its signal anyway. The next tick will
        # try again; the worst case is one extra git fetch.
        logger.exception("self_update failed to upsert SelfUpdateMarker for %s", label)
    return outcome


def _signal_from_outcome(*, label: str, outcome: _PullOutcome) -> ScanSignal:
    kind = f"self_update.{outcome.outcome}"
    summary = f"self-update {label}: {outcome.outcome}"
    if outcome.reason:
        summary = f"{summary} ({outcome.reason})"
    return ScanSignal(
        kind=kind,
        summary=summary,
        payload={
            "repo": label,
            "outcome": outcome.outcome,
            "reason": outcome.reason,
            "old_sha": outcome.old_sha,
            "new_sha": outcome.new_sha,
        },
    )


type _CiGate = Callable[[Path], CiVerdict | None]


def _attempt_pull(*, repo: Path, ci_gate: _CiGate) -> _PullOutcome:
    """Run the per-repo decision ladder against an existing clone.

    Mirrors the contract of :func:`teatree.cli.update.update_repo` but
    decoupled from typer / CLI side effects: no echo, no reinstall, no
    self-DB migration probe — only the git fetch + ff-only pull plus
    the safety gates (origin remote present, default branch resolved,
    branch matches default, tracked-clean tree) and the CI-green gate.
    Each gate yields a :class:`_PullOutcome` with a structured reason.
    ``old_sha`` on every outcome carries the pre-pass HEAD so the
    persisted marker records what the clone was anchored on, even on a
    skip.

    The CI gate is consulted ONLY after ``origin/<default>`` is detected
    ahead of HEAD: an already-current clone is ``up_to_date`` and the CI
    status is never queried (no remote call on the common path).
    """
    pre_sha = _full_sha(repo)
    pre_check = _pre_pull_gate(repo=repo, pre_sha=pre_sha)
    if pre_check is not None:
        return pre_check
    if not _origin_ahead(repo, pre_sha=pre_sha):
        return _PullOutcome(outcome="up_to_date", old_sha=pre_sha, new_sha=pre_sha)
    ci_check = _ci_skip(repo=repo, pre_sha=pre_sha, ci_gate=ci_gate)
    if ci_check is not None:
        return ci_check
    pull = _git(repo, "pull", "--ff-only")
    if pull.returncode != 0:
        return _PullOutcome(outcome="failed", reason=f"pull:{pull.stderr.strip()[:120]}", old_sha=pre_sha)
    new_sha = _full_sha(repo)
    if new_sha == pre_sha:
        return _PullOutcome(outcome="up_to_date", old_sha=pre_sha, new_sha=new_sha)
    return _PullOutcome(outcome="updated", old_sha=pre_sha, new_sha=new_sha)


def _ci_skip(*, repo: Path, pre_sha: str, ci_gate: _CiGate) -> _PullOutcome | None:
    """Fail-closed CI gate: return a skip outcome unless the verdict is green."""
    verdict = ci_gate(repo)
    if verdict is None or verdict is CiVerdict.GREEN:
        return None
    return _PullOutcome(outcome="skipped", reason=_CI_SKIP_REASON[verdict], old_sha=pre_sha)


def _pre_pull_gate(*, repo: Path, pre_sha: str) -> _PullOutcome | None:
    """Run fetch + branch/clean safety gates; return ``None`` when clear to pull."""
    fetch = _git(repo, "fetch", "origin")
    if fetch.returncode != 0:
        return _PullOutcome(outcome="failed", reason=f"fetch:{fetch.stderr.strip()[:120]}", old_sha=pre_sha)
    default_branch = _default_branch(repo)
    if default_branch is None:
        return _PullOutcome(outcome="skipped", reason="no_origin_head", old_sha=pre_sha)
    current = _current_branch(repo)
    if current != default_branch:
        return _PullOutcome(
            outcome="skipped",
            reason=_off_default_reason(current, default_branch),
            old_sha=pre_sha,
        )
    dirty = _tracked_dirty_paths(repo)
    if dirty:
        return _PullOutcome(
            outcome="skipped",
            reason=f"dirty_tracked:{','.join(dirty)[:80]}",
            old_sha=pre_sha,
        )
    return None


def _origin_ahead(repo: Path, *, pre_sha: str) -> bool:
    """True iff ``origin/<default>`` carries commits HEAD does not.

    Compares HEAD against the tracking ref resolved from ``origin/HEAD``
    (already confirmed present by the pre-pull gate). When the upstream
    SHA cannot be resolved, conservatively reports *not ahead* so the
    scanner reports ``up_to_date`` rather than querying CI / pulling
    against an unknown upstream.
    """
    default_branch = _default_branch(repo)
    if default_branch is None:
        return False
    upstream = _git(repo, "rev-parse", f"origin/{default_branch}")
    if upstream.returncode != 0:
        return False
    return upstream.stdout.strip() != pre_sha


def _queue_reinstall(*, label: str, target_sha: str) -> None:
    """Upsert a deferred-reinstall row; never crash the tick on a DB error."""
    from teatree.core.models.pending_reinstall import PendingReinstall  # noqa: PLC0415 — deferred: ORM/app-registry

    try:
        with transaction.atomic():
            PendingReinstall.objects.upsert_pending(repo_label=label, target_sha=target_sha)
    except Exception:
        logger.exception("self_update failed to queue PendingReinstall for %s", label)


def _git(repo: Path, *args: str) -> CompletedProcess[str]:
    """Run ``git`` in *repo*; never raise on non-zero exit (caller branches on returncode)."""
    return run_allowed_to_fail(["git", *args], cwd=repo, expected_codes=None)


def _full_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _default_branch(repo: Path) -> str | None:
    result = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().rsplit("/", 1)[-1]


def _tracked_dirty_paths(repo: Path) -> list[str]:
    """Return paths with uncommitted *tracked* changes — untracked are not blockers."""
    result = _git(repo, "status", "--porcelain")
    return [line[3:] for line in result.stdout.splitlines() if line and not line.startswith("??")]


__all__ = ["SelfUpdateScanner"]
