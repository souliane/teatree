"""No-post preview of the followup mini-loop — the same decisions, none of the consequences.

The followup loop posts under the OWNER's identity, and without a preview the only ways to check
a change to *who may be nagged* were to fire a real tick at real merge requests and watch
what landed, or to hand-roll a shadow run that had to mirror the scanner's internals to
stay honest. Both are how a colleague's merge request gets re-pinged under the owner's
name by an agent verifying the guard meant to prevent exactly that.

The preview is the same run with two terminal substitutions, and nothing else:

1. **The database is a throwaway file copy** (:func:`_disposable_database_copies`, a
    WAL-safe :func:`teatree.sqlite_snapshot._sqlite_snapshot` per alias). Scanners claim,
    update and ``on_commit`` exactly as they do live; every one of those writes lands on
    the copy and is discarded, so no ``ReviewRequestPost`` claim, ``DeferredQuestion``,
    ``OnBehalfAudit``, ``LoopLease`` or ``Loop.last_run_at`` bump reaches the real rows.
2. **The final Slack call is swapped for a recorder** (the context-scoped suppressor in
    :mod:`teatree.core.egress_transport`). It sits BELOW every gate, so the route, the
    on-behalf approval check and the audit all still run for real and their refusals are
    what the report prints.

Everything between those two is the live code: the same ``collect_scan`` fan-out, the same
scanners, the same ``_is_self_authored``, the same ``OnBehalfSlackEgress``. That is the
point — a preview with its own copy of the decisions is a preview that can be right while
live is wrong. What this module adds is purely observational: three ContextVar observers
(authorship, egress attempt, owner question) correlate one row per candidate.

The correlation is per-context, which is why the preview's pool submits each job through
``copy_context().run``: the observers must reach the worker threads, and each thread needs
its own ``_CURRENT_ASSESSMENT`` so two scanners assessing different targets concurrently
cannot attribute one another's verdicts. The live tick's pool carries no caller context.
"""

import logging
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper

from teatree.core.on_behalf_egress import EgressAttempt, EgressKind, observe_on_behalf_egress, suppress_on_behalf_egress
from teatree.core.review.mr_state_question import observe_owner_question_creation
from teatree.core.review.review_candidate import AuthorshipAssessment, AuthorshipVerdict, observe_authorship
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import SCAN_DEADLINE_SECONDS, ScanOutcome, collect_scan, scan_pool_size
from teatree.sqlite_snapshot import _sqlite_snapshot
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

_OWNER_QUESTION_ACTION = "owner_question_create"
_OWNER_DM_ACTION = "owner_dm_post"
_MERGE_REACTION_ACTION = "merge_reaction"
_REFUSAL_REASONS = {
    AuthorshipVerdict.FOREIGN: "the author is not a configured self identity",
    AuthorshipVerdict.UNREADABLE: "authorship could not be proved from the forge",
}
#: Actions the authorship guard does NOT gate, so a non-SELF verdict on one is not a
#: breach. ``merge_reaction`` is colleague-facing BY DESIGN — the ``:merge:`` on a
#: colleague's merged merge request is the feature, and it is a self-authored row that
#: the live scanner skips. The other two reach only the owner. Every other action on a
#: non-SELF target is a breach, INCLUDING an action this module has never seen: an
#: unknown colleague-facing egress must fail the gate loudly rather than pass unmapped.
_UNGATED_BY_AUTHORSHIP = frozenset({_MERGE_REACTION_ACTION, _OWNER_QUESTION_ACTION, _OWNER_DM_ACTION})
_CURRENT_ASSESSMENT: ContextVar[AuthorshipAssessment | None] = ContextVar("dry_run_authorship", default=None)
type DryRunJobs = list[_ScannerJob] | Callable[[], list[_ScannerJob]]


@dataclass(slots=True)
class FollowupDryRunCandidate:
    event: str
    target: str
    author: str
    verdict: str
    suppressed_action: str
    outcome: str
    refusal_reason: str
    channel: str = ""
    thread: str = ""


@dataclass(slots=True)
class FollowupDryRunReport:
    """What the followup loop WOULD do, and whether that is safe to let run live.

    ``refused_count`` is descriptive — a foreign or unreadable candidate the guard
    turned away is the guard WORKING, so it never fails the run. ``breach_count`` is
    the gate: it counts action attempts that reached the egress on a target the guard
    did not class as the owner's — the incident this whole preview exists to make impossible.
    """

    candidates: list[FollowupDryRunCandidate]
    errors: dict[str, str]
    selected_scanners: tuple[str, ...] = ()
    live_admission: str = ""

    @property
    def refused_count(self) -> int:
        return sum(
            (candidate.event == "assessment" and candidate.verdict != AuthorshipVerdict.SELF.value)
            or (candidate.event == "action" and candidate.outcome == "refused")
            for candidate in self.candidates
        )

    @property
    def breach_count(self) -> int:
        return len(self.breaches)

    @property
    def breaches(self) -> list[FollowupDryRunCandidate]:
        """Action rows that would act on a target the authorship guard did not clear."""
        return [
            candidate
            for candidate in self.candidates
            if candidate.event == "action"
            and candidate.verdict != AuthorshipVerdict.SELF.value
            and candidate.suppressed_action not in _UNGATED_BY_AUTHORSHIP
        ]

    @property
    def exit_code(self) -> int:
        """0 clean · 1 guard breach · 3 vacuous — the gate contract (usage 2 is the CLI's).

        3 exists so a gate run can never PASS by previewing nothing: with the active
        posture forbidding egress, or with no messaging backend, ``build_jobs`` selects
        no colleague scanner at all and a plain "0 candidates, exit 0" would read as
        proof the guard holds when in fact nothing was examined.
        """
        if self.breach_count:
            return 1
        if not self.selected_scanners:
            return 3
        return 0

    @property
    def live_admission_line(self) -> str:
        return f"blocked — {self.live_admission}" if self.live_admission else "admitted"

    def as_payload(self) -> dict[str, Any]:
        return {
            "dry_run": True,
            "loop": "followup",
            "candidate_count": len(self.candidates),
            "refused_count": self.refused_count,
            "breach_count": self.breach_count,
            "selected_scanners": list(self.selected_scanners),
            "live_admission": self.live_admission,
            "exit_code": self.exit_code,
            "candidates": [asdict(candidate) for candidate in self.candidates],
            "errors": self.errors,
        }


class _Recorder:
    def __init__(self) -> None:
        self._entries: list[FollowupDryRunCandidate] = []
        self._lock = Lock()

    @property
    def candidates(self) -> list[FollowupDryRunCandidate]:
        with self._lock:
            return list(self._entries)

    def _append(self, candidate: FollowupDryRunCandidate) -> None:
        with self._lock:
            self._entries.append(candidate)

    def observe_authorship(self, assessment: AuthorshipAssessment) -> None:
        _CURRENT_ASSESSMENT.set(assessment)
        reason = _REFUSAL_REASONS.get(assessment.verdict, "")
        self._append(
            FollowupDryRunCandidate(
                event="assessment",
                target=assessment.target,
                author=assessment.author,
                verdict=assessment.verdict.value,
                suppressed_action="",
                outcome="",
                refusal_reason=reason,
            ),
        )

    def record_action(self, attempt: EgressAttempt) -> None:
        assessment = _CURRENT_ASSESSMENT.get()
        self._append(
            FollowupDryRunCandidate(
                event="action",
                target=attempt.target,
                author=assessment.author if assessment is not None else "",
                verdict=assessment.verdict.value if assessment is not None else AuthorshipVerdict.UNREADABLE.value,
                suppressed_action=attempt.action,
                outcome=attempt.outcome.value,
                refusal_reason=attempt.reason,
                channel=attempt.channel,
                thread=attempt.thread,
            ),
        )

    def record_owner_question(self, mr_url: str) -> None:
        assessment = _CURRENT_ASSESSMENT.get()
        self._append(
            FollowupDryRunCandidate(
                event="action",
                target=mr_url,
                author=assessment.author if assessment is not None else "",
                verdict=assessment.verdict.value if assessment is not None else AuthorshipVerdict.UNREADABLE.value,
                suppressed_action=_OWNER_QUESTION_ACTION,
                outcome="asked",
                refusal_reason="owner-question deduplication and capacity checks passed",
            ),
        )

    @staticmethod
    def suppress_egress(_target: str, _action: str, _kind: EgressKind) -> RawAPIDict:
        return {"ok": True, "ts": "dry-run"}


class _CopyLease:
    """Counts the jobs running against the disposable copy; once closed it admits no new one."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._running = 0
        self._closed = False

    def run[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        with self._lock:
            if self._closed:
                msg = "the followup preview has ended; its disposable database is gone"
                raise RuntimeError(msg)
            self._running += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with self._lock:
                self._running -= 1

    def close(self) -> bool:
        """Close the lease; True only when no job is still running on the copy."""
        with self._lock:
            self._closed = True
            return self._running == 0


class _SubmitterContextPool(ThreadPoolExecutor):
    def __init__(self, lease: _CopyLease, *, max_workers: int) -> None:
        super().__init__(max_workers=max_workers)
        self._lease = lease

    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        return super().submit(partial(copy_context().run, self._lease.run, fn, *args, **kwargs))


def _scan_to_completion(jobs: list[_ScannerJob], lease: _CopyLease) -> ScanOutcome:
    if not jobs:
        return ScanOutcome()
    pool = _SubmitterContextPool(lease, max_workers=scan_pool_size(len(jobs)))
    try:
        return collect_scan(
            pool,
            jobs,
            per_job_timeout=SCAN_DEADLINE_SECONDS,
            overrun_note="past the live deadline; the preview waited for it to finish",
        )
    finally:
        # Joined while the disposable copy is still the configured database: an abandoned
        # scanner that reconnected after the switch back would write to the real rows.
        pool.shutdown(wait=True, cancel_futures=True)


def _database_sources() -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for alias in connections.databases:
        wrapper = connections[alias]
        if not isinstance(wrapper, SQLiteDatabaseWrapper):
            msg = f"followup dry-run requires SQLite; database alias {alias!r} uses {wrapper.vendor!r}"
            raise ImproperlyConfigured(msg)
        name = str(wrapper.settings_dict["NAME"])
        if not name or ":memory:" in name or name.startswith("file:"):
            msg = f"followup dry-run requires a file-backed SQLite database; alias {alias!r} uses {name!r}"
            raise ImproperlyConfigured(msg)
        source = Path(name)
        if not source.is_file():
            msg = f"followup dry-run database for alias {alias!r} does not exist: {source}"
            raise ImproperlyConfigured(msg)
        sources[alias] = source
    return sources


@contextmanager
def _disposable_database_copies() -> Iterator[_CopyLease]:
    sources = _database_sources()
    connections.close_all()
    with TemporaryDirectory(prefix="teatree-followup-preview-") as directory:
        copies: dict[Path, Path] = {}
        for source in set(sources.values()):
            copy = Path(directory) / f"database-{len(copies)}.sqlite3"
            _sqlite_snapshot(source, copy)
            copies[source] = copy

        original_names = {alias: connections.databases[alias]["NAME"] for alias in sources}
        lease = _CopyLease()
        try:
            for alias, source in sources.items():
                connections.databases[alias]["NAME"] = str(copies[source])
            yield lease
        finally:
            connections.close_all()
            # Every thread's connection reads this one shared settings dict, so restoring it
            # under a still-running scanner would send that scanner's next connect to the real rows.
            if lease.close():
                for alias, name in original_names.items():
                    connections.databases[alias]["NAME"] = name
            else:
                logger.warning("followup preview interrupted with a scanner still running; database left on the copy")


def run_followup_dry_run(jobs_or_builder: DryRunJobs, *, live_admission: str = "") -> FollowupDryRunReport:
    """Run the followup scanners for real against a disposable copy, posting nothing.

    Everything the live tick decides is decided here, by the live code: the same
    ``collect_scan`` fan-out, the same scanners, the same ``_is_self_authored``, the same
    ``OnBehalfSlackEgress`` gates. Only two things differ, and both are terminal — the
    database is a throwaway file copy, and the final Slack call is swapped for a
    recorder. So there is no second decision surface that can drift from live.

    *live_admission* is what the live tick's admission gate would say right now (the
    caller reads it from ``loop_block_reasons``); it is REPORTED, never applied — see
    :func:`teatree.loops.loop_table.preview_loop_jobs` for why previewing through the
    gate would answer "nothing to see here" precisely when the loop is masked off.
    """
    recorder = _Recorder()
    with (
        _disposable_database_copies() as lease,
        observe_authorship(recorder.observe_authorship),
        suppress_on_behalf_egress(recorder.suppress_egress),
        observe_on_behalf_egress(recorder.record_action),
        observe_owner_question_creation(recorder.record_owner_question),
    ):
        jobs = jobs_or_builder if isinstance(jobs_or_builder, list) else jobs_or_builder()
        outcome = _scan_to_completion(jobs, lease)
    return FollowupDryRunReport(
        candidates=recorder.candidates,
        errors=outcome.errors,
        selected_scanners=tuple(job.scanner.name for job in jobs),
        live_admission=live_admission,
    )


def render_followup_dry_run(report: FollowupDryRunReport) -> str:
    scanners = ", ".join(report.selected_scanners) or "<none>"
    lines = [
        (
            f"DRY-RUN followup — {len(report.candidates)} candidate(s), "
            f"{report.refused_count} refused, {report.breach_count} breach(es); no writes"
        ),
        f"live tick would be: {report.live_admission_line}",
        f"scanners selected: {scanners}",
    ]
    if not report.selected_scanners:
        lines.append(
            "VACUOUS no colleague scanner was selected — the active posture forbids egress or no "
            "messaging backend is configured, so NOTHING was examined and this run proves nothing",
        )
    lines.extend(
        f"BREACH would act on a non-self target: {breach.suppressed_action} on {breach.target} "
        f"(author {breach.author or '<unreadable>'}, verdict {breach.verdict})"
        for breach in report.breaches
    )
    lines.append("event | target | author | verdict | action | channel | thread | live outcome | reason")
    lines.extend(
        " | ".join(
            (
                candidate.event,
                candidate.target,
                candidate.author or "<unreadable>",
                candidate.verdict,
                candidate.suppressed_action,
                candidate.channel or "-",
                candidate.thread or "-",
                candidate.outcome or "-",
                candidate.refusal_reason or "-",
            ),
        )
        for candidate in report.candidates
    )
    lines.extend(f"WARN {name}: {message}" for name, message in report.errors.items())
    return "\n".join(lines)
