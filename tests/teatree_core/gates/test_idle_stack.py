"""Idle-stack detection — the reapable-worktree predicate (#2190).

A locally-running worktree (``services_up``/``ready``) is REAPABLE when there
is no live Session and no active/claimed Task on its ticket, AND
``last_used_at`` is older than the idle threshold, AND it is not the
currently-active worktree (the CWD), AND its own docker stack is PROVEN QUIET.

Fail-safe: any uncertainty ⇒ KEEP (never reaped). The anti-vacuous core of
the suite — reverting the active-session / active-task / CWD guard, or the
stack-activity guard, must turn an ``active-stack-NOT-reaped`` test RED.
"""

from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.gates import idle_stack as idle_mod
from teatree.core.gates.idle_stack import StackActivity, classify_running_worktrees, reapable_worktrees
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery


def _running_worktree(
    *,
    overlay: str = "t3-heavy",
    ticket_number: str = "100",
    state: Worktree.State = Worktree.State.SERVICES_UP,
    idle_minutes_ago: int = 60,
    worktree_path: str = "",
) -> Worktree:
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://example.com/{overlay}/issues/{ticket_number}",
    )
    extra: dict[str, str] = {}
    if worktree_path:
        extra["worktree_path"] = worktree_path
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path="backend",
        branch=f"{ticket_number}-feat",
        state=state,
        db_name=f"wt_{ticket_number}",
        last_used_at=timezone.now() - timedelta(minutes=idle_minutes_ago),
        extra=extra,
    )


def _fake_docker(
    *,
    container_ids: Sequence[str] = ("app-1",),
    logs: dict[str, tuple[str, str]] | None = None,
    ps_returncode: int = 0,
    logs_returncode: int = 0,
    raises: BaseException | None = None,
) -> "Callable[..., CompletedProcess[str]]":
    """A stand-in docker CLI: ``ps`` lists *container_ids*, ``logs`` replays their output.

    *logs* maps a container id to its ``(stdout, stderr)`` — both, because a dev
    app server logs its requests on stderr and ``docker logs`` keeps the streams
    apart. Patched over ``run_allowed_to_fail`` — the real subprocess boundary —
    so the whole probe chain (``stack_activity`` → ``_project_container_ids`` →
    ``_emitted_within_window``) runs for real against a scripted daemon.
    """
    streams = logs or {}

    def _run(cmd: Sequence[str], **_: object) -> "CompletedProcess[str]":
        argv = list(cmd)
        if raises is not None:
            raise raises
        if argv[1] == "ps":
            return CompletedProcess(argv, ps_returncode, "".join(f"{cid}\n" for cid in container_ids), "")
        out, err = streams.get(argv[-1], ("", ""))
        return CompletedProcess(argv, logs_returncode, out, err)

    return _run


class _StackLiveBase(TestCase):
    """Default every stack to a PROVEN-QUIET docker stack.

    Isolates the DB-authored guards from the docker probe (which is exercised
    on its own in ``TestStackActivityProbe`` / ``TestOutOfBandRunNotReaped``),
    and keeps the suite hermetic on a host with no docker daemon.
    """

    def setUp(self) -> None:
        super().setUp()
        quiet = patch.object(idle_mod, "stack_activity", return_value=StackActivity.QUIET)
        quiet.start()
        self.addCleanup(quiet.stop)


class TestReapableHappyPath(_StackLiveBase):
    def test_idle_running_worktree_is_reapable(self) -> None:
        wt = _running_worktree()
        reapable = list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert wt in reapable

    def test_ready_state_is_reapable_too(self) -> None:
        wt = _running_worktree(state=Worktree.State.READY)
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestActiveStackNotReaped(_StackLiveBase):
    """The anti-vacuous core: an ACTIVE stack must never be reaped.

    Revert the corresponding guard in ``idle_stack.py`` and each of these
    goes RED (the active stack would be wrongly reaped).
    """

    def test_live_session_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="200")
        Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=None)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_ended_session_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="201")
        Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_pending_task_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="202")
        session = Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.PENDING)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_claimed_task_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="203")
        session = Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.CLAIMED)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_completed_task_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="204")
        session = Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.COMPLETED)
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_recently_used_stack_is_kept(self) -> None:
        wt = _running_worktree(ticket_number="205", idle_minutes_ago=5)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_null_last_used_at_is_kept_fail_safe(self) -> None:
        """A worktree with no recorded activity cannot be confirmed idle ⇒ KEEP."""
        wt = _running_worktree(ticket_number="206")
        wt.last_used_at = None
        wt.save(update_fields=["last_used_at"])
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_currently_active_worktree_is_kept(self) -> None:
        """The CWD's own worktree is never reaped even when otherwise idle."""
        wt = _running_worktree(ticket_number="207", worktree_path="/ws/207-feat/backend")
        with patch.object(idle_mod, "_active_worktree_path", return_value=Path("/ws/207-feat/backend")):
            assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_provisioned_worktree_is_not_a_candidate(self) -> None:
        """PROVISIONED holds no docker stack — nothing to reap."""
        wt = _running_worktree(ticket_number="208", state=Worktree.State.PROVISIONED)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestActiveDeliveryNotReaped(_StackLiveBase):
    """#2227: a stack under active delivery / fresh E2E evidence / a pin is KEPT.

    The anti-vacuous core for #2227: revert any one guard in
    ``idle_stack.preserve_reason`` and the matching test goes RED (the live
    target of in-flight work would be wrongly reaped, forcing a re-provision).
    A genuinely idle stack carrying NONE of the three is still reaped.
    """

    def test_live_external_delivery_lease_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="220")
        mark_external_delivery(wt.ticket)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_expired_external_delivery_lease_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="221")
        mark_external_delivery(wt.ticket, lease_seconds=-1)
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_recent_e2e_run_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="222")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_stale_e2e_run_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="223")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=120)
        wt.save(update_fields=["last_e2e_run"])
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_null_e2e_run_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="224")
        assert wt.last_e2e_run is None
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_explicit_pin_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="225")
        wt.extra = {**wt.extra, "reaper_pinned": True}
        wt.save(update_fields=["extra"])
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_genuinely_idle_with_none_is_reaped(self) -> None:
        wt = _running_worktree(ticket_number="226")
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_preserve_reason_names_the_lease(self) -> None:
        wt = _running_worktree(ticket_number="227")
        mark_external_delivery(wt.ticket)
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert classified[wt] is not None
        assert "external-delivery lease" in classified[wt]

    def test_preserve_reason_names_the_e2e_run(self) -> None:
        wt = _running_worktree(ticket_number="228")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))
        assert classified[wt] is not None
        assert "E2E" in classified[wt]

    def test_preserve_reason_names_the_pin(self) -> None:
        wt = _running_worktree(ticket_number="229")
        wt.extra = {**wt.extra, "reaper_pinned": True}
        wt.save(update_fields=["extra"])
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert classified[wt] is not None
        assert "pinned" in classified[wt]

    def test_reapable_idle_classifies_with_no_reason(self) -> None:
        wt = _running_worktree(ticket_number="230")
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))
        assert classified[wt] is None


class TestEffectiveE2eWindowFromConfig(_StackLiveBase):
    """The E2E window defaults to ``idle_stack_e2e_recent_minutes`` when not passed."""

    def test_default_window_keeps_a_recent_e2e_run(self) -> None:
        wt = _running_worktree(ticket_number="240")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        with patch.object(idle_mod, "get_effective_settings") as settings:
            settings.return_value.idle_stack_e2e_recent_minutes = 60
            assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_zero_window_disables_the_e2e_guard(self) -> None:
        wt = _running_worktree(ticket_number="241")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        with patch.object(idle_mod, "get_effective_settings") as settings:
            settings.return_value.idle_stack_e2e_recent_minutes = 0
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestCrossOverlayScope(_StackLiveBase):
    def test_other_overlays_worktrees_are_not_returned(self) -> None:
        _running_worktree(overlay="t3-other", ticket_number="300")
        mine = _running_worktree(overlay="t3-heavy", ticket_number="301")
        reapable = list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert mine in reapable
        assert all(w.overlay == "t3-heavy" for w in reapable)


class TestOutOfBandRunNotReaped(TestCase):
    """A stack driven OUT OF BAND must never be reaped — the #2190/#2227 blind spot.

    Every other guard is authored by the control plane: an FSM transition
    (``last_used_at``), a ``Session``/``Task`` row, a delivery lease, a
    post-hoc ``lifecycle record-e2e-run`` stamp. A live Playwright run drives
    the stack over HTTP and writes NONE of them — so a stack under an in-flight
    run presents to the reaper exactly like an abandoned one and gets stopped
    mid-run.

    The anti-vacuous core: delete the stack-activity guard from
    ``preserve_reason`` and ``test_in_flight_run_keeps_stack`` goes RED, while
    ``test_silent_stack_is_still_reaped`` stays GREEN — a reaper that never
    reaps is as broken as one that reaps everything.
    """

    def test_in_flight_run_keeps_stack(self) -> None:
        """The bug: every control-plane signal is stale, yet the stack is serving traffic."""
        wt = _running_worktree(ticket_number="500")
        serving = _fake_docker(
            container_ids=("web-1", "frontend-1"),
            logs={"frontend-1": ('192.168.65.1 - - "GET /loan-request/12 HTTP/1.1" 200 65488\n', "")},
        )
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=serving):
            assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_app_server_stderr_counts_as_traffic(self) -> None:
        """A dev app server logs requests on stderr — ``docker logs`` keeps the streams apart."""
        wt = _running_worktree(ticket_number="501")
        serving = _fake_docker(
            container_ids=("web-1",),
            logs={"web-1": ("", 'WARNING basehttp "GET /api/ HTTP/1.1" 401 58\n')},
        )
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=serving):
            assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_silent_stack_is_still_reaped(self) -> None:
        """The control: a genuinely idle stack emits nothing and is still reaped."""
        wt = _running_worktree(ticket_number="502")
        silent = _fake_docker(container_ids=("web-1", "frontend-1", "db-1"))
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=silent):
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_preserve_reason_names_the_traffic(self) -> None:
        wt = _running_worktree(ticket_number="503")
        serving = _fake_docker(container_ids=("web-1",), logs={"web-1": ("GET /\n", "")})
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=serving):
            classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert classified[wt] is not None
        assert "something is driving it" in classified[wt]


class TestUnprovableStackIsKept(TestCase):
    """FAIL CLOSED: a stack the reaper cannot PROVE quiet is kept, never reaped."""

    def _kept(self, wt: Worktree, runner: object) -> bool:
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=runner):
            return wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_docker_ps_failure_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="510")
        assert self._kept(wt, _fake_docker(ps_returncode=1))

    def test_docker_logs_failure_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="511")
        assert self._kept(wt, _fake_docker(container_ids=("web-1",), logs_returncode=1))

    def test_missing_docker_binary_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="512")
        assert self._kept(wt, _fake_docker(raises=FileNotFoundError("docker")))

    def test_wedged_daemon_timeout_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="513")
        assert self._kept(wt, _fake_docker(raises=TimeoutExpired(["docker", "ps"], 10.0)))

    def test_preserve_reason_names_the_unprovable_stack(self) -> None:
        wt = _running_worktree(ticket_number="514")
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=_fake_docker(ps_returncode=1)):
            classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert classified[wt] is not None
        assert "fail-closed KEEP" in classified[wt]


class TestPartialStackReconcile(TestCase):
    """A db-only partial stack (app tier down, db lingering) is reapable once quiet.

    The wt595 leak class: the app tier is down but a stray ``db-1`` survives.
    The reaper must treat a quiet one as reapable (stop the WHOLE project), NOT
    as a healthy stack to keep.
    """

    def test_quiet_db_only_partial_stack_is_reapable(self) -> None:
        wt = _running_worktree(ticket_number="400")
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=_fake_docker(container_ids=("db-1",))):
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_zero_container_stack_is_still_reapable(self) -> None:
        """A fully-gone stack is also reapable — nobody can drive it, and the stop is a no-op."""
        wt = _running_worktree(ticket_number="401")
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=_fake_docker(container_ids=())):
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestStackActivityProbe(TestCase):
    """``stack_activity`` maps the docker seam → BUSY / QUIET / UNKNOWN."""

    def _activity(self, runner: object, project: str = "backend-wt1") -> StackActivity:
        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=runner):
            return idle_mod.stack_activity(project, window_minutes=30)

    def test_blank_project_is_unknown(self) -> None:
        assert self._activity(_fake_docker(), project="") is StackActivity.UNKNOWN

    def test_no_containers_is_quiet(self) -> None:
        assert self._activity(_fake_docker(container_ids=())) is StackActivity.QUIET

    def test_any_emitting_container_is_busy(self) -> None:
        runner = _fake_docker(container_ids=("db-1", "web-1"), logs={"web-1": ("GET /\n", "")})
        assert self._activity(runner) is StackActivity.BUSY

    def test_whitespace_only_output_is_quiet(self) -> None:
        runner = _fake_docker(container_ids=("web-1",), logs={"web-1": ("\n  \n", "")})
        assert self._activity(runner) is StackActivity.QUIET

    def test_window_is_a_relative_duration_floored_at_one_minute(self) -> None:
        """The daemon resolves ``--since Nm`` on its own clock — skew cannot fake quiet."""
        seen: list[list[str]] = []

        def _record(cmd: Sequence[str], **_: object) -> "CompletedProcess[str]":
            seen.append(list(cmd))
            argv = list(cmd)
            return CompletedProcess(argv, 0, "web-1\n" if argv[1] == "ps" else "", "")

        with patch.object(idle_mod, "run_allowed_to_fail", side_effect=_record):
            idle_mod.stack_activity("backend-wt1", window_minutes=0)
        assert [seen[-1][2], seen[-1][3]] == ["--since", "1m"]


class TestActiveWorktreePathHelper(TestCase):
    """``_active_worktree_path`` returns the resolved CWD, ``None`` on OSError."""

    def test_returns_none_on_oserror(self) -> None:
        with patch.object(idle_mod.Path, "cwd", side_effect=OSError):
            assert idle_mod._active_worktree_path() is None


class TestIsCurrentlyActiveHelper(TestCase):
    """``_is_currently_active`` matches a worktree's own dir or a child of it."""

    def test_none_active_path_is_not_active(self) -> None:
        wt = _running_worktree(ticket_number="900", worktree_path="/ws/900/backend")
        assert idle_mod._is_currently_active(wt, None) is False

    def test_blank_worktree_path_is_not_active(self) -> None:
        wt = _running_worktree(ticket_number="901")  # no worktree_path
        assert idle_mod._is_currently_active(wt, Path("/ws/901/backend")) is False

    def test_child_of_worktree_is_active(self) -> None:
        wt = _running_worktree(ticket_number="902", worktree_path="/ws/902/backend")
        assert idle_mod._is_currently_active(wt, Path("/ws/902/backend/src/app")) is True


class TestPreserveReasonFailSafeGuards(TestCase):
    """``preserve_reason`` directly — the defensive fail-safe guards still hold.

    A non-``None`` reason means KEEP; ``None`` means reapable.
    """

    def _cutoff(self) -> object:
        return timezone.now() - timedelta(minutes=30)

    def _e2e_cutoff(self) -> object:
        return timezone.now() - timedelta(minutes=60)

    def _reason(self, wt: Worktree) -> str | None:
        return idle_mod.preserve_reason(
            wt,
            cutoff=self._cutoff(),
            e2e_cutoff=self._e2e_cutoff(),
            active_path=None,
            activity_window_minutes=30,
        )

    def test_non_running_state_cannot_proceed_is_kept(self) -> None:
        """A PROVISIONED row can't ``stop_services`` → kept (the can_proceed guard)."""
        wt = _running_worktree(ticket_number="910", state=Worktree.State.PROVISIONED)
        assert self._reason(wt) is not None

    def test_null_last_used_at_is_kept(self) -> None:
        wt = _running_worktree(ticket_number="911")
        wt.last_used_at = None
        with patch.object(idle_mod, "stack_activity", return_value=StackActivity.QUIET):
            assert self._reason(wt) is not None
