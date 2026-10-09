"""No-post dry-run coverage for the followup mini-loop."""

import datetime as dt
import hashlib
import io
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connections, transaction
from django.db.models import F
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone
from typer.testing import CliRunner

import teatree.cli.overlay as overlay_module
import teatree.config as config_module
import teatree.core.gates.review_request_guard as review_request_guard_module
import teatree.core.management.commands.loops_tick as loops_tick_module
import teatree.loop.followup_dry_run as followup_dry_run_module
import teatree.loop.phases.scan as scan_phase_module
import teatree.loop.scanners.review_request_resume as review_request_resume_module
import teatree.loops.loop_table as loop_table_module
from teatree.cli.overlay import OverlayAppBuilder
from teatree.config import TeaTreeConfig, UserSettings, cold_reader
from teatree.core.backend_protocols import DraftState, PrOpenState
from teatree.core.gates.review_request_guard import GuardTarget, ReconcileResult, ReconcileStatus
from teatree.core.models import Loop, LoopLease, OnBehalfApproval, OnBehalfAudit, ReviewRequestPost
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.on_behalf_egress import OnBehalfSlackEgress
from teatree.core.review.review_candidate import _is_self_authored
from teatree.loop.followup_dry_run import render_followup_dry_run, run_followup_dry_run
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import ScanOutcome
from teatree.loop.scanners.review_nag import ReviewNagScanner
from teatree.loop.scanners.review_request_merge_react import ReviewRequestMergeReactScanner
from teatree.loop.scanners.review_request_resume import ReviewRequestResumeScanner
from teatree.loop.tick import TickRequest
from teatree.types import RawAPIDict
from tests._send_gate import allow_slack_channels

_CHANNEL = "C_REVIEW"
_OWNER = "owner"
_SELF_POST = "https://gitlab.example/o/r/-/merge_requests/1"
_FOREIGN_REACT = "https://gitlab.example/o/r/-/merge_requests/2"
_UNREADABLE = "https://gitlab.example/o/r/-/merge_requests/3"
_OWNER_QUESTION = "https://gitlab.example/o/r/-/merge_requests/4"
_SELF_RESUME = "https://gitlab.example/o/r/-/merge_requests/5"
_CONCURRENT_FIRST = "https://gitlab.example/o/r/-/merge_requests/6"
_CONCURRENT_SECOND = "https://gitlab.example/o/r/-/merge_requests/7"
_CONCURRENT_ACTION = "concurrent_preview_post"
_BREACHING_ACTION = "review_nag_post"
_FOLLOWUP_SCRIPT = "src/teatree/loops/followup/loop.py"


@dataclass
class _Slack:
    posts: list[tuple[str, str, str]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = (channel, thread_ts)
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        reactions = [{"name": "double_vertical_bar"}] if ts == "thread-5" else []
        return {"ts": ts, "reactions": reactions}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.posts.append((channel, text, thread_ts))
        return {"ok": True, "ts": "posted"}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}


@dataclass
class _Host:
    def get_pr_author(self, *, pr_url: str) -> str:
        if pr_url == _UNREADABLE:
            message = "forge unavailable"
            raise RuntimeError(message)
        return "colleague" if pr_url == _FOREIGN_REACT else _OWNER

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return PrOpenState.MERGED if pr_url == _FOREIGN_REACT else PrOpenState.OPEN

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        return DraftState.NOT_DRAFT

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> dict[str, Any]:
        _ = (repo, pr_iid)
        return {"approvals_left": 1, "approved_by": [], "unresolved_resolvable": 0}

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        _ = (slug, pr_id)
        return [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]

    def fetch_required_status_check_contexts(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        _ = (slug, pr_id)
        return [{"context": "test"}]


class _UnreadableHost(_Host):
    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        message = "forge unavailable"
        raise RuntimeError(message)


class _ProbeReviewNagScanner(ReviewNagScanner):
    def __init__(self, probe: Callable[[], None], name: str) -> None:
        super().__init__(messaging=None, name=name)
        self._probe = probe

    def scan(self) -> list[Any]:
        self._probe()
        return []


@dataclass
class _ConcurrentWritingScanner:
    target: str
    messaging: _Slack
    barrier: Barrier
    name: str

    def scan(self) -> list[Any]:
        post = ReviewRequestPost.objects.get(mr_url=self.target)
        _is_self_authored(self.target, _Host(), (_OWNER,))
        self.barrier.wait(timeout=5)
        with transaction.atomic():
            ReviewRequestPost.objects.filter(pk=post.pk).update(nag_count=F("nag_count") + 1)
            transaction.on_commit(
                lambda: OnBehalfSlackEgress(self.messaging).post(
                    channel=_CHANNEL,
                    text=self.target,
                    target=self.target,
                    action=_CONCURRENT_ACTION,
                    thread_ts=post.slack_thread_ts,
                ),
            )
        return []


class _InlineThreadPoolExecutor:
    """Keep ORM scanner tests on xdist's connection-local in-memory schema."""

    def __init__(self, lease: followup_dry_run_module._CopyLease, *, max_workers: int) -> None:
        _ = max_workers
        self._lease = lease

    def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(self._lease.run(fn, *args))
        except Exception as exc:  # noqa: BLE001 — mirror Future's exception capture in this inline test executor
            future.set_exception(exc)
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        _ = (wait, cancel_futures)


@dataclass
class _LateWritingScanner:
    target: str
    delay: float
    name: str = "late"

    def scan(self) -> list[Any]:
        time.sleep(self.delay)
        ReviewRequestPost.objects.filter(mr_url=self.target).update(nag_count=F("nag_count") + 1)
        return []


@dataclass
class _HeldWritingScanner:
    target: str
    started: Event
    release: Event
    attempted: Event
    name: str = "held"

    def scan(self) -> list[Any]:
        self.started.set()
        self.release.wait(timeout=5)
        self.attempted.set()
        ReviewRequestPost.objects.filter(mr_url=self.target).update(nag_count=F("nag_count") + 1)
        return []


@dataclass
class _GuardIgnoringScanner:
    """A scanner that assesses authorship and then posts anyway — the breach the gate exists for."""

    target: str
    messaging: _Slack
    name: str

    def scan(self) -> list[Any]:
        post = ReviewRequestPost.objects.get(mr_url=self.target)
        _is_self_authored(self.target, _Host(), (_OWNER,))
        OnBehalfSlackEgress(self.messaging).post(
            channel=_CHANNEL,
            text=self.target,
            target=self.target,
            action=_BREACHING_ACTION,
            thread_ts=post.slack_thread_ts,
        )
        return []


class _PostgresWrapper:
    vendor = "postgresql"


class _PostgresConnections:
    databases: ClassVar[dict[str, dict[str, str]]] = {"default": {}}

    def __getitem__(self, alias: str) -> _PostgresWrapper:
        return _PostgresWrapper()


def _refuse_to_build() -> list[_ScannerJob]:
    msg = "the preview built jobs against a database it had already refused to copy"
    raise AssertionError(msg)


def _seed(url: str, number: int, *, nag_count: int = 0) -> None:
    ReviewRequestPost.objects.create(
        mr_url=url,
        overlay="",
        slack_channel_id=_CHANNEL,
        slack_thread_ts=f"thread-{number}",
        created_at=timezone.now() - dt.timedelta(days=40),
        nag_count=nag_count,
    )


def _seed_followup_loop(*, enabled: bool) -> Loop:
    loop, _created = Loop.objects.update_or_create(
        name="followup",
        defaults={"script": _FOLLOWUP_SCRIPT, "delay_seconds": 60, "enabled": enabled, "last_run_at": None},
    )
    return loop


@contextmanager
def _previewed_jobs(jobs: list[_ScannerJob]) -> Iterator[None]:
    """Hand the command a fixed job selection — ``preview_loop_jobs`` has its own tests."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(loops_tick_module.Command, "_build_request", return_value=TickRequest()))
        stack.enter_context(patch.object(loop_table_module, "preview_loop_jobs", return_value=jobs))
        yield


def _approve_actions() -> None:
    for target, action in (
        (_SELF_POST, "review_nag_post"),
        (_FOREIGN_REACT, "merge_reaction"),
        (_SELF_RESUME, "review_request_resume_post"),
    ):
        OnBehalfApproval.record(target=target, action=action, approver_id="owner")


def _jobs(slack: _Slack, host: _Host | None = None) -> list[_ScannerJob]:
    resolved_host = host or _Host()
    return [
        _ScannerJob(ReviewNagScanner(slack, host=resolved_host, identities=(_OWNER,)), "test"),
        _ScannerJob(ReviewRequestMergeReactScanner(slack, host=resolved_host, identities=(_OWNER,)), "test"),
        _ScannerJob(ReviewRequestResumeScanner(slack, host=resolved_host, identities=(_OWNER,)), "test"),
    ]


@contextmanager
def _file_backed_default_database() -> Iterator[Path]:
    wrapper = connections["default"]
    wrapper.ensure_connection()
    original_name = wrapper.settings_dict["NAME"]
    original_options = wrapper.settings_dict["OPTIONS"]
    original_transaction_mode = wrapper.transaction_mode
    original_raw = wrapper.connection
    if original_raw is None:
        msg = "the test database connection did not open"
        raise RuntimeError(msg)

    with TemporaryDirectory(prefix="followup-source-") as directory:
        source = Path(directory) / "source.sqlite3"
        destination = sqlite3.connect(source)
        try:
            original_raw.backup(destination)
        finally:
            destination.close()

        wrapper.connection = None
        wrapper.settings_dict["NAME"] = str(source)
        wrapper.settings_dict["OPTIONS"] = {**original_options, "timeout": 30, "transaction_mode": "IMMEDIATE"}
        wrapper.transaction_mode = "IMMEDIATE"
        try:
            yield source
        finally:
            redirected_raw = wrapper.connection
            if redirected_raw is not None:
                redirected_raw.close()
            wrapper.connection = None
            wrapper.settings_dict["NAME"] = original_name
            wrapper.settings_dict["OPTIONS"] = original_options
            wrapper.transaction_mode = original_transaction_mode
            wrapper.connection = original_raw


def _review_request_post_checksum(path: Path) -> tuple[int, str]:
    database = sqlite3.connect(path)
    try:
        rows = database.execute(
            "SELECT mr_url, slack_channel_id, slack_thread_ts, nag_count, last_nag_at, done_at, resumed_at "
            "FROM teatree_review_request_post ORDER BY id"
        ).fetchall()
    finally:
        database.close()
    return len(rows), hashlib.sha256(repr(rows).encode()).hexdigest()


@contextmanager
def _scanner_dependencies(*, inline_workers: bool = True) -> Iterator[Path]:
    config = TeaTreeConfig(user=UserSettings(review_nag_max_interval_days=30))
    target = GuardTarget(channel_id=_CHANNEL, channel_name="review", token="unused")
    with ExitStack() as stack:
        source = stack.enter_context(_file_backed_default_database())
        stack.enter_context(patch.object(config_module, "load_config", return_value=config))
        stack.enter_context(patch.object(cold_reader, "mapping_setting", return_value={}))
        stack.enter_context(
            patch.object(review_request_guard_module, "resolve_guard_target", return_value=target),
        )
        stack.enter_context(
            patch.object(
                review_request_guard_module,
                "reconcile_out_of_band",
                return_value=ReconcileResult(ReconcileStatus.ABSENT),
            ),
        )
        stack.enter_context(
            patch.object(review_request_resume_module, "draft_state", return_value=DraftState.NOT_DRAFT),
        )
        stack.enter_context(patch.object(review_request_resume_module, "_required_checks_green", return_value=True))
        if inline_workers:
            stack.enter_context(
                patch.object(followup_dry_run_module, "_SubmitterContextPool", _InlineThreadPoolExecutor)
            )
        yield source


class TestFollowupDryRun(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        allow_slack_channels(_CHANNEL)

    def test_records_every_authorship_verdict_and_suppresses_every_egress_and_write(self) -> None:
        _seed(_SELF_POST, 1)
        _seed(_FOREIGN_REACT, 2)
        _seed(_UNREADABLE, 3)
        _seed(_OWNER_QUESTION, 4, nag_count=7)
        _seed(_SELF_RESUME, 5)
        _approve_actions()
        approvals = list(OnBehalfApproval.objects.order_by("pk").values())
        before = list(ReviewRequestPost.objects.order_by("pk").values())
        slack = _Slack()
        with _scanner_dependencies():
            report = run_followup_dry_run(_jobs(slack))

        action_entries = {entry.suppressed_action: entry for entry in report.candidates if entry.suppressed_action}
        assert set(action_entries) == {
            "merge_reaction",
            "owner_question_create",
            "review_nag_post",
            "review_request_resume_post",
        }
        assert action_entries["review_nag_post"].outcome == "posted"
        assert action_entries["merge_reaction"].outcome == "posted"
        assert action_entries["owner_question_create"].outcome == "asked"
        assert action_entries["review_request_resume_post"].outcome == "posted"
        assert action_entries["review_nag_post"].channel == _CHANNEL
        assert action_entries["review_nag_post"].thread == "thread-1"
        assert action_entries["merge_reaction"].channel == _CHANNEL
        assert action_entries["merge_reaction"].thread == "thread-2"
        assert action_entries["review_request_resume_post"].channel == _CHANNEL
        assert action_entries["review_request_resume_post"].thread == "thread-5"
        assert action_entries["owner_question_create"].channel == ""
        assert action_entries["owner_question_create"].thread == ""
        assessments = [entry for entry in report.candidates if entry.event == "assessment"]
        assert {entry.verdict for entry in assessments} == {"self", "foreign", "unreadable"}
        assert any(entry.target == _UNREADABLE and entry.refusal_reason for entry in assessments)
        assert report.refused_count >= 2
        assert report.errors == {}
        assert slack.posts == []
        assert slack.reactions == []
        assert list(ReviewRequestPost.objects.order_by("pk").values()) == before
        assert DeferredQuestion.objects.count() == 0
        assert list(OnBehalfApproval.objects.order_by("pk").values()) == approvals
        assert OnBehalfAudit.objects.count() == 0
        rendered = render_followup_dry_run(report)
        assert "posted" in rendered
        assert "asked" in rendered
        assert f"{_CHANNEL} | thread-1" in rendered
        assert f"{_CHANNEL} | thread-2" in rendered

    def test_reports_a_live_gate_refusal_instead_of_a_synthetic_post(self) -> None:
        _seed(_SELF_POST, 1)
        slack = _Slack()

        with _scanner_dependencies():
            report = run_followup_dry_run([_jobs(slack)[0]])

        action = next(entry for entry in report.candidates if entry.suppressed_action == "review_nag_post")
        assert action.outcome == "refused"
        assert "approval" in action.refusal_reason
        assert slack.posts == []

    def test_records_all_conflicting_assessments_for_one_target(self) -> None:
        _seed(_SELF_POST, 1)
        ReviewRequestPost.objects.filter(mr_url=_SELF_POST).update(last_nag_at=timezone.now())
        slack = _Slack()
        jobs = [
            _ScannerJob(ReviewNagScanner(slack, host=_Host(), identities=(_OWNER,), name="self"), "test"),
            _ScannerJob(
                ReviewNagScanner(slack, host=_UnreadableHost(), identities=(_OWNER,), name="unreadable"),
                "test",
            ),
        ]

        with _scanner_dependencies():
            report = run_followup_dry_run(jobs)

        assessments = [entry for entry in report.candidates if entry.event == "assessment"]
        assert sorted((entry.target, entry.verdict) for entry in assessments) == [
            (_SELF_POST, "self"),
            (_SELF_POST, "unreadable"),
        ]
        assert report.refused_count == 1

    def test_non_due_candidate_does_not_report_an_action_attempt(self) -> None:
        _seed(_SELF_POST, 1)
        ReviewRequestPost.objects.filter(mr_url=_SELF_POST).update(last_nag_at=timezone.now())

        with _scanner_dependencies():
            report = run_followup_dry_run([_jobs(_Slack())[0]])

        entries = [entry for entry in report.candidates if entry.target == _SELF_POST]
        assert [(entry.event, entry.suppressed_action) for entry in entries] == [("assessment", "")]

    def test_uses_the_parallel_scan_pipeline(self) -> None:
        barrier = Barrier(2)
        counter_lock = Lock()
        active = 0
        peak_active = 0

        def overlap_probe() -> None:
            nonlocal active, peak_active
            with counter_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                barrier.wait(timeout=1)
            finally:
                with counter_lock:
                    active -= 1

        jobs = [
            _ScannerJob(_ProbeReviewNagScanner(overlap_probe, "first"), "test"),
            _ScannerJob(_ProbeReviewNagScanner(overlap_probe, "second"), "test"),
        ]

        with (
            _file_backed_default_database(),
            patch.dict(connections.databases["default"]["OPTIONS"], {"transaction_mode": "IMMEDIATE"}),
            patch.object(
                followup_dry_run_module, "collect_scan", wraps=followup_dry_run_module.collect_scan
            ) as collect,
        ):
            report = run_followup_dry_run(jobs)

        collect.assert_called_once()
        assert collect.call_args.args[1] == jobs
        assert report.errors == {}
        assert peak_active == 2

    def test_concurrent_writers_report_the_same_actions_as_live_without_lock_errors(self) -> None:
        for number, target in enumerate((_CONCURRENT_FIRST, _CONCURRENT_SECOND), start=6):
            _seed(target, number)
            OnBehalfApproval.record(target=target, action=_CONCURRENT_ACTION, approver_id="owner")
        preview_slack = _Slack()

        with _scanner_dependencies(inline_workers=False):
            preview_barrier = Barrier(2)
            preview_jobs = [
                _ScannerJob(
                    _ConcurrentWritingScanner(target, preview_slack, preview_barrier, f"preview-{number}"), "test"
                )
                for number, target in enumerate((_CONCURRENT_FIRST, _CONCURRENT_SECOND), start=1)
            ]
            preview = run_followup_dry_run(preview_jobs)

            live_slack = _Slack()
            live_barrier = Barrier(2)
            live_jobs = [
                _ScannerJob(_ConcurrentWritingScanner(target, live_slack, live_barrier, f"live-{number}"), "test")
                for number, target in enumerate((_CONCURRENT_FIRST, _CONCURRENT_SECOND), start=1)
            ]
            live = scan_phase_module.scan_phase(live_jobs)

        preview_actions = {
            (candidate.target, candidate.suppressed_action)
            for candidate in preview.candidates
            if candidate.event == "action"
        }
        live_actions = {(text, _CONCURRENT_ACTION) for _channel, text, _thread_ts in live_slack.posts}
        assert preview.errors == {}
        assert live.errors == {}
        assert (
            preview_actions
            == live_actions
            == {
                (_CONCURRENT_FIRST, _CONCURRENT_ACTION),
                (_CONCURRENT_SECOND, _CONCURRENT_ACTION),
            }
        )
        assert preview_slack.posts == []

    def test_preview_writes_and_on_commit_egress_leave_the_source_database_untouched(self) -> None:
        for number, target in enumerate((_CONCURRENT_FIRST, _CONCURRENT_SECOND), start=6):
            _seed(target, number)
            OnBehalfApproval.record(target=target, action=_CONCURRENT_ACTION, approver_id="owner")
        slack = _Slack()

        with _scanner_dependencies(inline_workers=False) as source:
            before_bytes = hashlib.sha256(source.read_bytes()).hexdigest()
            before_content = _review_request_post_checksum(source)
            barrier = Barrier(2)
            jobs = [
                _ScannerJob(_ConcurrentWritingScanner(target, slack, barrier, f"write-{number}"), "test")
                for number, target in enumerate((_CONCURRENT_FIRST, _CONCURRENT_SECOND), start=1)
            ]

            report = run_followup_dry_run(jobs)

            assert hashlib.sha256(source.read_bytes()).hexdigest() == before_bytes
            assert _review_request_post_checksum(source) == before_content
        assert report.errors == {}
        assert slack.posts == []

    def test_a_scanner_past_the_live_deadline_never_writes_to_the_source_database(self) -> None:
        _seed(_SELF_POST, 1)

        with (
            _scanner_dependencies(inline_workers=False) as source,
            patch.object(followup_dry_run_module, "SCAN_DEADLINE_SECONDS", 0.1),
        ):
            before = _review_request_post_checksum(source)
            report = run_followup_dry_run([_ScannerJob(_LateWritingScanner(_SELF_POST, delay=0.5), "test")])
            time.sleep(1.0)

            assert _review_request_post_checksum(source) == before
        assert "waited for it to finish" in report.errors["late"]

    def test_a_second_interrupt_during_the_join_never_lets_the_held_scanner_reach_the_source_database(self) -> None:
        _seed(_SELF_POST, 1)
        started, release, attempted = Event(), Event(), Event()
        pools: list[ThreadPoolExecutor] = []
        real_shutdown = followup_dry_run_module._SubmitterContextPool.shutdown

        def first_interrupt_while_collecting(
            pool: ThreadPoolExecutor, jobs: list[_ScannerJob], **_: object
        ) -> ScanOutcome:
            pools.append(pool)
            pool.submit(scan_phase_module._run_job_closing_connections, jobs[0])
            started.wait(timeout=5)
            raise KeyboardInterrupt

        def second_interrupt_while_joining(pool: ThreadPoolExecutor, *, wait: bool, cancel_futures: bool) -> None:
            _ = wait
            real_shutdown(pool, wait=False, cancel_futures=cancel_futures)
            raise KeyboardInterrupt

        job = _ScannerJob(_HeldWritingScanner(_SELF_POST, started, release, attempted), "test")
        with _scanner_dependencies(inline_workers=False) as source:
            before = _review_request_post_checksum(source)
            with (
                patch.object(followup_dry_run_module, "collect_scan", first_interrupt_while_collecting),
                patch.object(followup_dry_run_module._SubmitterContextPool, "shutdown", second_interrupt_while_joining),
                pytest.raises(KeyboardInterrupt),
            ):
                run_followup_dry_run([job])
            release.set()
            real_shutdown(pools[0], wait=True, cancel_futures=False)

            assert attempted.is_set()
            assert _review_request_post_checksum(source) == before

    def test_restores_database_settings_and_deletes_the_copy_when_job_building_raises(self) -> None:
        copies: list[Path] = []
        snapshot = followup_dry_run_module._sqlite_snapshot

        def record_snapshot(source: Path, destination: Path) -> None:
            copies.append(destination)
            snapshot(source, destination)

        def fail_to_build_jobs() -> list[_ScannerJob]:
            msg = "job construction failed"
            raise RuntimeError(msg)

        with _scanner_dependencies() as source:
            with (
                patch.object(followup_dry_run_module, "_sqlite_snapshot", side_effect=record_snapshot),
                pytest.raises(RuntimeError, match="job construction failed"),
            ):
                run_followup_dry_run(fail_to_build_jobs)

            assert connections.databases["default"]["NAME"] == str(source)
            assert ReviewRequestPost.objects.count() == 0
        assert copies
        assert all(not path.exists() for path in copies)

    def test_command_gates_on_a_breach_not_on_a_refusal(self) -> None:
        _seed(_SELF_POST, 1)
        _seed(_FOREIGN_REACT, 2)
        _approve_actions()
        slack = _Slack()
        out = io.StringIO()
        with _scanner_dependencies(), _previewed_jobs(_jobs(slack)):
            call_command("loops_tick", loop="followup", dry_run=True, json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["breach_count"] == 0
        assert payload["refused_count"] >= 1
        rows = {(row["suppressed_action"], row["verdict"]) for row in payload["candidates"]}
        assert ("merge_reaction", "foreign") in rows
        assert slack.posts == []
        assert slack.reactions == []

    def test_command_exits_one_when_a_scanner_would_act_on_a_foreign_target(self) -> None:
        _seed(_FOREIGN_REACT, 2)
        OnBehalfApproval.record(target=_FOREIGN_REACT, action=_BREACHING_ACTION, approver_id="owner")
        slack = _Slack()
        out = io.StringIO()
        job = _ScannerJob(_GuardIgnoringScanner(_FOREIGN_REACT, slack, "breacher"), "test")

        with _scanner_dependencies(), _previewed_jobs([job]), pytest.raises(SystemExit) as exc:
            call_command("loops_tick", loop="followup", dry_run=True, json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert exc.value.code == 1
        assert payload["breach_count"] == 1
        assert slack.posts == []

    def test_command_exits_zero_when_every_foreign_candidate_was_refused(self) -> None:
        _seed(_FOREIGN_REACT, 2)
        _seed(_UNREADABLE, 3)
        slack = _Slack()
        out = io.StringIO()
        nag = _ScannerJob(ReviewNagScanner(slack, host=_Host(), identities=(_OWNER,)), "test")

        with _scanner_dependencies(), _previewed_jobs([nag]):
            call_command("loops_tick", loop="followup", dry_run=True, json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["breach_count"] == 0
        assert payload["refused_count"] >= 2
        assert {row["verdict"] for row in payload["candidates"]} == {"foreign", "unreadable"}
        assert slack.posts == []

    def test_command_previews_a_loop_its_manual_override_masks_off(self) -> None:
        _seed(_SELF_POST, 1)
        _approve_actions()
        loop = _seed_followup_loop(enabled=False)
        slack = _Slack()
        out = io.StringIO()

        with _scanner_dependencies(), _previewed_jobs([_jobs(slack)[0]]):
            call_command("loops_tick", loop="followup", dry_run=True, json_output=True, stdout=out)

        payload = json.loads(out.getvalue())
        loop.refresh_from_db()
        assert payload["candidate_count"] > 0
        assert payload["selected_scanners"] == ["review_nag"]
        assert "forced OFF by a manual override" in payload["live_admission"]
        assert loop.last_run_at is None
        assert LoopLease.objects.count() == 0

    def test_command_exits_three_when_the_posture_selected_no_colleague_scanner(self) -> None:
        _seed(_SELF_POST, 1)
        _seed_followup_loop(enabled=True)
        err = io.StringIO()

        with _scanner_dependencies(), _previewed_jobs([]), pytest.raises(SystemExit) as exc:
            call_command("loops_tick", loop="followup", dry_run=True, stderr=err)

        assert exc.value.code == 3
        assert "VACUOUS" in err.getvalue()

    def test_command_rejects_dry_run_on_another_loop(self) -> None:
        err = io.StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("loops_tick", loop="ship", dry_run=True, stderr=err)

        assert exc.value.code == 2
        assert "--loop followup" in err.getvalue()

    def test_command_isolates_loop_bookkeeping_and_claims_no_lease(self) -> None:
        _seed(_SELF_POST, 1)
        _approve_actions()
        loop = _seed_followup_loop(enabled=True)
        out = io.StringIO()
        slack = _Slack()
        before = list(ReviewRequestPost.objects.order_by("pk").values())

        with _scanner_dependencies(), _previewed_jobs([_jobs(slack)[0]]):
            call_command("loops_tick", loop="followup", dry_run=True, json_output=True, stdout=out)

        loop.refresh_from_db()
        assert loop.last_run_at is None
        assert LoopLease.objects.count() == 0
        assert list(ReviewRequestPost.objects.order_by("pk").values()) == before
        assert slack.posts == []

    def test_overlay_cli_exposes_followup_dry_run(self) -> None:
        _seed(_SELF_POST, 1)
        _seed_followup_loop(enabled=True)
        OnBehalfApproval.record(target=_SELF_POST, action="review_nag_post", approver_id="owner")
        slack = _Slack()
        app = OverlayAppBuilder("sample", None).build()
        bridged: list[tuple[tuple[str, ...], str]] = []

        def run_management_command(*args: str, overlay_name: str = "") -> None:
            bridged.append((args, overlay_name))
            call_command(
                "loops_tick",
                loop=args[args.index("--loop") + 1],
                overlay=args[args.index("--overlay") + 1],
                dry_run="--dry-run" in args,
                json_output="--json" in args,
            )

        with _scanner_dependencies(), _previewed_jobs([_jobs(slack)[0]]), ExitStack() as stack:
            stack.enter_context(patch.object(overlay_module, "managepy_core", side_effect=run_management_command))
            result = CliRunner().invoke(app, ["loops", "tick", "--loop", "followup", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        assert bridged == [
            (("loops", "tick", "--loop", "followup", "--dry-run", "--json", "--overlay", "sample"), "sample"),
        ]
        assert slack.posts == []


class TestPreviewRefusesAnUncopyableDatabase(SimpleTestCase):
    """The disposable copy IS the no-write guarantee, so a database it cannot copy must refuse.

    Degrading to the live rows instead would run the real scanners — claims, questions,
    audit rows, the cadence bump — straight at the database the preview promises not to touch.
    """

    def test_refuses_a_non_sqlite_alias(self) -> None:
        with (
            patch.object(followup_dry_run_module, "connections", _PostgresConnections()),
            pytest.raises(ImproperlyConfigured, match="requires SQLite"),
        ):
            run_followup_dry_run(_refuse_to_build)

    def test_refuses_an_alias_with_no_file_to_copy(self) -> None:
        with (
            patch.dict(connections["default"].settings_dict, {"NAME": ":memory:"}),
            pytest.raises(ImproperlyConfigured, match="requires a file-backed SQLite database"),
        ):
            run_followup_dry_run(_refuse_to_build)

    def test_refuses_a_database_file_that_does_not_exist(self) -> None:
        with TemporaryDirectory(prefix="followup-absent-") as directory:
            absent = Path(directory) / "absent.sqlite3"
            with (
                patch.dict(connections["default"].settings_dict, {"NAME": str(absent)}),
                pytest.raises(ImproperlyConfigured, match="does not exist"),
            ):
                run_followup_dry_run(_refuse_to_build)


class TestCopyLease(SimpleTestCase):
    def test_refuses_a_job_that_starts_after_the_preview_closed(self) -> None:
        lease = followup_dry_run_module._CopyLease()

        assert lease.close()
        with pytest.raises(RuntimeError, match="preview has ended"):
            lease.run(_refuse_to_build)
