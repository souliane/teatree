"""Pre-start gate for ``max_concurrent_local_stacks`` (souliane/teatree#1397).

Caps the number of concurrent locally-running stacks per overlay so a
host with a 1-stack memory budget cannot OOM by spinning up a second
worktree's docker stack while the first is still serving. The gate is
opt-in (default ``0`` = unbounded, no behavior change), enforced at the
``Worktree.start_services()`` boundary, and per-overlay scoped so a heavy
overlay can cap to 1 while a cheap dogfood overlay stays unbounded.

Integration-first per the Test-Writing Doctrine: real Worktree rows
under TestCase, the production gate helper exercised directly, plus a
CLI-level ``call_command`` test that proves the gate refuses through
``t3 <overlay> worktree start``.
"""

import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import get_effective_settings, load_config
from teatree.core.gates import local_stack_gate as gate_mod
from teatree.core.gates.local_stack_gate import LocalStackLimitExceededError, check_local_stack_limit
from teatree.core.models import ConfigSetting, Ticket, Worktree


@pytest.fixture(autouse=True)
def _stacks_appear_live(request: pytest.FixtureRequest) -> "object":
    """Default every blocker to a live docker stack for the gate-behavior tests.

    The gate reconciles blocker rows against docker and demotes phantoms
    (zero running and zero existing containers). The gate-behavior tests
    model rows that are genuinely up, so default the running probe to
    "live"; the reconciliation/restart tests override this with their own
    ``patch.object``. The count-mapping tests exercise the probe helpers
    themselves, so they opt out — patching the helper would shadow what
    they assert.
    """
    if request.cls is TestContainerCountMapping:
        yield
        return
    with patch.object(gate_mod, "_running_container_count", return_value=1):
        yield


def _write_toml(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")


def _make_worktree(
    *,
    overlay: str,
    ticket_number: str,
    state: str,
    repo_path: str = "backend",
    worktree_path: str = "",
) -> Worktree:
    ticket = Ticket.objects.create(
        issue_url=f"https://example.com/{overlay}/issues/{ticket_number}",
        overlay=overlay,
    )
    extra: dict[str, str] = {}
    if worktree_path:
        extra["worktree_path"] = worktree_path
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path=repo_path,
        branch=f"{ticket_number}-feat",
        state=state,
        extra=extra,
    )


class TestConfigLoadsMaxConcurrentLocalStacks(TestCase):
    """``max_concurrent_local_stacks`` is DB-home (#1775): default 0, set in the store."""

    def test_default_is_zero_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / ".teatree.toml"
            _write_toml(config_path, "[teatree]\n")
            assert load_config(config_path).user.max_concurrent_local_stacks == 0

    def test_global_setting_resolves_from_db_store(self) -> None:
        # DB-home under the partition: a GLOBAL ``ConfigSetting`` row supplies the
        # value (a ``[teatree]`` key would be ignored on read).
        ConfigSetting.objects.set_value("max_concurrent_local_stacks", 1)
        assert get_effective_settings().max_concurrent_local_stacks == 1


class TestLocalStackGateUnbounded(TestCase):
    """Default (limit=0) lets any number of stacks run."""

    def test_unbounded_zero_does_not_refuse(self) -> None:
        """With ``limit=0`` even N stacks already up cannot trigger refusal."""
        wt_a = _make_worktree(
            overlay="t3-heavy",
            ticket_number="1001",
            state=Worktree.State.SERVICES_UP,
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="1002",
            state=Worktree.State.PROVISIONED,
        )
        check_local_stack_limit(candidate, limit=0)
        # Cleanup: explicit so the assertion shape stays obvious.
        del wt_a


class TestLocalStackGateLimitOne(TestCase):
    """Limit 1: the second start is refused; first must teardown before second can start."""

    def test_first_start_passes(self) -> None:
        """A single in-flight start under limit=1 must pass — only the SECOND is refused."""
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2001",
            state=Worktree.State.PROVISIONED,
        )
        check_local_stack_limit(candidate, limit=1)

    def test_second_start_is_refused_with_blocker_named(self) -> None:
        blocker = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2010",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/2010-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2011",
            state=Worktree.State.PROVISIONED,
        )
        with pytest.raises(LocalStackLimitExceededError) as exc:
            check_local_stack_limit(candidate, limit=1)
        message = str(exc.value)
        # The blocker worktree's path is surfaced so the operator can act.
        assert "/ws/2010-feat/backend" in message
        # The teardown command is suggested explicitly.
        assert "teardown" in message
        # Cleanup
        del blocker

    def test_ready_state_counts_as_blocking(self) -> None:
        """A worktree in READY is just as much a stack as SERVICES_UP — both block."""
        blocker = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2020",
            state=Worktree.State.READY,
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2021",
            state=Worktree.State.PROVISIONED,
        )
        with pytest.raises(LocalStackLimitExceededError):
            check_local_stack_limit(candidate, limit=1)
        del blocker

    def test_provisioned_state_does_not_block(self) -> None:
        """PROVISIONED is dormant (no docker up yet) — must not count toward the limit."""
        dormant = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2030",
            state=Worktree.State.PROVISIONED,
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2031",
            state=Worktree.State.PROVISIONED,
        )
        check_local_stack_limit(candidate, limit=1)
        del dormant

    def test_candidate_itself_does_not_count(self) -> None:
        """A re-fire of the same worktree must not refuse against its own row."""
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2040",
            state=Worktree.State.SERVICES_UP,
        )
        # Re-firing start on the same row (idempotent FSM): the gate must
        # see "I am the only stack up" and pass.
        check_local_stack_limit(candidate, limit=1)

    def test_after_blocker_torn_down_second_can_start(self) -> None:
        blocker = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2050",
            state=Worktree.State.SERVICES_UP,
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="2051",
            state=Worktree.State.PROVISIONED,
        )
        with pytest.raises(LocalStackLimitExceededError):
            check_local_stack_limit(candidate, limit=1)
        # Operator runs teardown — model the post-teardown state.
        blocker.state = Worktree.State.CREATED
        blocker.save(update_fields=["state"])
        check_local_stack_limit(candidate, limit=1)


class TestLocalStackGateCrossOverlay(TestCase):
    """The limit is per-overlay: one overlay's stack count cannot block another's."""

    def test_other_overlay_stack_does_not_block(self) -> None:
        heavy_blocker = _make_worktree(
            overlay="t3-heavy",
            ticket_number="3001",
            state=Worktree.State.SERVICES_UP,
        )
        candidate = _make_worktree(
            overlay="t3-teatree",
            ticket_number="3002",
            state=Worktree.State.PROVISIONED,
        )
        check_local_stack_limit(candidate, limit=1)
        del heavy_blocker


class TestLocalStackGateMultiRepoTicket(TestCase):
    """A multi-repo ticket is one logical stack, not N (one per repo)."""

    def test_sibling_worktrees_of_same_ticket_are_not_blockers(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/t3-heavy/issues/5001",
            overlay="t3-heavy",
        )
        sibling = Worktree.objects.create(
            overlay="t3-heavy",
            ticket=ticket,
            repo_path="backend",
            branch="5001-feat",
            state=Worktree.State.SERVICES_UP,
        )
        candidate = Worktree.objects.create(
            overlay="t3-heavy",
            ticket=ticket,
            repo_path="frontend",
            branch="5001-feat",
            state=Worktree.State.PROVISIONED,
        )
        check_local_stack_limit(candidate, limit=1)
        del sibling


class TestWorktreeStartCliEnqueuesWhenLimitExceeded(TestCase):
    """End-to-end: at the cap ``t3 worktree start`` ENQUEUES — never SystemExit (#2190).

    The pre-#2190 behaviour was a hard ``SystemExit(1)``; #2190 replaces it
    with reap → retry → enqueue. The CLI must leave a ``LocalStackQueueItem``
    row, leave the candidate's FSM un-advanced (still PROVISIONED), and exit 0.
    """

    def test_start_enqueues_when_another_stack_running(self) -> None:
        from teatree.core.management.commands import worktree as worktree_cmd  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            wt_dir = Path(tmp) / "backend"
            wt_dir.mkdir()
            # Blocking ticket (separate ticket, already SERVICES_UP).
            blocker_ticket = Ticket.objects.create(
                overlay="t3-heavy",
                issue_url="https://example.com/t3-heavy/issues/6001",
            )
            Worktree.objects.create(
                overlay="t3-heavy",
                ticket=blocker_ticket,
                repo_path="backend",
                branch="6001-feat",
                extra={"worktree_path": "/ws/6001-feat/backend"},
                state=Worktree.State.SERVICES_UP,
            )
            # Candidate ticket — PROVISIONED, about to start.
            candidate_ticket = Ticket.objects.create(
                overlay="t3-heavy",
                issue_url="https://example.com/t3-heavy/issues/6002",
            )
            candidate = Worktree.objects.create(
                overlay="t3-heavy",
                ticket=candidate_ticket,
                repo_path="backend",
                branch="6002-feat",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.PROVISIONED,
            )

            from teatree.core.gates import local_stack_gate as gate_mod  # noqa: PLC0415
            from teatree.core.models import LocalStackQueueItem  # noqa: PLC0415

            # Real gate end-to-end: limit=1, the blocker stays live, and no idle
            # stack is reapable (the blocker has no last_used_at). The CLI must
            # NOT raise SystemExit — it enqueues and returns.
            with (
                patch.object(worktree_cmd, "resolve_worktree", return_value=candidate),
                patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            ):
                call_command("worktree", "start", path=str(wt_dir))

            candidate.refresh_from_db()
            assert candidate.state == Worktree.State.PROVISIONED
            item = LocalStackQueueItem.objects.get(worktree=candidate)
            assert item.status == LocalStackQueueItem.Status.QUEUED


class TestLocalStackGateDockerReconciliation(TestCase):
    """A ``SERVICES_UP`` row with no live docker stack is a phantom — it must not block.

    The DB FSM state can lie after a docker restart / OOM / manual
    ``compose down``: the row still reads ``SERVICES_UP`` but holds no
    real stack. Counting it refuses every legitimate start forever
    (the 8568 Kletterrate blocker). The gate reconciles against
    ``docker ps`` and demotes such phantoms before counting.
    """

    def test_phantom_blocker_with_zero_live_containers_does_not_count(self) -> None:
        phantom = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7010",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/7010-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7011",
            state=Worktree.State.PROVISIONED,
        )
        with (
            patch.object(gate_mod, "_running_container_count", return_value=0),
            patch.object(gate_mod, "_existing_container_count", return_value=0),
        ):
            check_local_stack_limit(candidate, limit=1)
        phantom.refresh_from_db()
        assert phantom.state == Worktree.State.PROVISIONED

    def test_running_stack_still_counts_and_refuses(self) -> None:
        live = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7020",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/7020-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7021",
            state=Worktree.State.PROVISIONED,
        )
        with (
            patch.object(gate_mod, "_running_container_count", return_value=2),
            pytest.raises(LocalStackLimitExceededError),
        ):
            check_local_stack_limit(candidate, limit=1)
        live.refresh_from_db()
        assert live.state == Worktree.State.SERVICES_UP

    def test_unverifiable_docker_fails_safe_and_keeps_counting(self) -> None:
        """When docker liveness can't be verified (-1) the row stays counted."""
        blocker = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7030",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/7030-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7031",
            state=Worktree.State.PROVISIONED,
        )
        with (
            patch.object(gate_mod, "_running_container_count", return_value=-1),
            pytest.raises(LocalStackLimitExceededError),
        ):
            check_local_stack_limit(candidate, limit=1)
        blocker.refresh_from_db()
        assert blocker.state == Worktree.State.SERVICES_UP

    def test_phantom_among_real_blockers_drops_only_the_phantom(self) -> None:
        """With limit=1, demoting one phantom while another stack is live still refuses."""
        phantom = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7040",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/7040-feat/backend",
        )
        live = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7041",
            state=Worktree.State.READY,
            worktree_path="/ws/7041-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="7042",
            state=Worktree.State.PROVISIONED,
        )

        def counts(project: str) -> int:
            return 0 if "wt7040" in project else 1

        with (
            patch.object(gate_mod, "_running_container_count", side_effect=counts),
            patch.object(gate_mod, "_existing_container_count", return_value=0),
            pytest.raises(LocalStackLimitExceededError) as exc,
        ):
            check_local_stack_limit(candidate, limit=1)
        phantom.refresh_from_db()
        live.refresh_from_db()
        assert phantom.state == Worktree.State.PROVISIONED
        assert live.state == Worktree.State.READY
        # The phantom must not appear in the refusal message; the live one must.
        message = str(exc.value)
        assert "/ws/7040-feat/backend" not in message
        assert "/ws/7041-feat/backend" in message


class TestLocalStackGateRestartRace(TestCase):
    """A stack mid-restart (containers exist but momentarily not running) is not a phantom.

    ``docker ps`` (running only) reports zero during a ``docker compose
    restart`` or a Docker-daemon reboot of a live worktree, but the
    containers still exist (``docker ps -a`` lists them). Demoting such a
    row to ``PROVISIONED`` and excluding it would corrupt a genuinely-live
    stack's FSM and undercount the cap. Only a stack with zero containers
    *total* is a phantom; "running zero, exist N" stays counted (fail-safe).
    """

    def test_restarting_stack_with_existing_containers_is_not_demoted(self) -> None:
        restarting = _make_worktree(
            overlay="t3-heavy",
            ticket_number="8010",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/8010-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="8011",
            state=Worktree.State.PROVISIONED,
        )
        with (
            patch.object(gate_mod, "_running_container_count", return_value=0),
            patch.object(gate_mod, "_existing_container_count", return_value=2),
            pytest.raises(LocalStackLimitExceededError),
        ):
            check_local_stack_limit(candidate, limit=1)
        restarting.refresh_from_db()
        assert restarting.state == Worktree.State.SERVICES_UP

    def test_fully_gone_stack_is_still_demoted(self) -> None:
        phantom = _make_worktree(
            overlay="t3-heavy",
            ticket_number="8020",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/8020-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="8021",
            state=Worktree.State.PROVISIONED,
        )
        with (
            patch.object(gate_mod, "_running_container_count", return_value=0),
            patch.object(gate_mod, "_existing_container_count", return_value=0),
        ):
            check_local_stack_limit(candidate, limit=1)
        phantom.refresh_from_db()
        assert phantom.state == Worktree.State.PROVISIONED

    def test_unverifiable_existence_keeps_a_zero_running_row_counted(self) -> None:
        """When ``docker ps -a`` cannot be queried (-1), a zero-running row stays counted."""
        blocker = _make_worktree(
            overlay="t3-heavy",
            ticket_number="8030",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/8030-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="8031",
            state=Worktree.State.PROVISIONED,
        )
        with (
            patch.object(gate_mod, "_running_container_count", return_value=0),
            patch.object(gate_mod, "_existing_container_count", return_value=-1),
            pytest.raises(LocalStackLimitExceededError),
        ):
            check_local_stack_limit(candidate, limit=1)
        blocker.refresh_from_db()
        assert blocker.state == Worktree.State.SERVICES_UP


class TestContainerCountMapping(TestCase):
    """``_container_count`` maps docker output and exit code to a count.

    Pins the returncode→count contract the gate's fail-safe relies on:
    a non-zero docker exit yields ``-1`` (could-not-verify), while a clean
    run counts the non-blank container names (``0`` = empty, ``N`` = live).
    The ``include_stopped`` flag is what tells "gone" from "restarting":
    it adds ``docker ps -a`` so stopped/restarting containers count too.
    """

    @staticmethod
    def _result(returncode: int, stdout: str) -> "CompletedProcess[str]":
        return CompletedProcess(["docker", "ps"], returncode, stdout, "")

    def test_returns_minus_one_on_docker_failure(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=self._result(1, "")):
            assert gate_mod._container_count("backend-wt1", include_stopped=False) == -1

    def test_counts_nonblank_names(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=self._result(0, "c1\n\nc2\n")):
            assert gate_mod._container_count("backend-wt1", include_stopped=False) == 2

    def test_zero_for_empty_stdout(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=self._result(0, "")):
            assert gate_mod._container_count("backend-wt1", include_stopped=False) == 0

    def test_minus_one_for_blank_project(self) -> None:
        assert gate_mod._container_count("", include_stopped=False) == -1

    def test_running_count_omits_the_all_flag(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=self._result(0, "")) as run:
            gate_mod._running_container_count("backend-wt1")
        cmd = run.call_args.args[0]
        assert cmd[:2] == ["docker", "ps"]
        assert "-a" not in cmd

    def test_existing_count_passes_the_all_flag(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=self._result(0, "")) as run:
            gate_mod._existing_container_count("backend-wt1")
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["docker", "ps", "-a"]

    def test_existing_count_counts_all_states(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=self._result(0, "c1\nc2\nc3\n")):
            assert gate_mod._container_count("backend-wt1", include_stopped=True) == 3


class TestLocalStackGateMultipleBlockers(TestCase):
    """When limit>1 and multiple stacks already running, the gate names every blocker."""

    def test_error_lists_all_blockers(self) -> None:
        b1 = _make_worktree(
            overlay="t3-heavy",
            ticket_number="4001",
            state=Worktree.State.SERVICES_UP,
            worktree_path="/ws/4001-feat/backend",
        )
        b2 = _make_worktree(
            overlay="t3-heavy",
            ticket_number="4002",
            state=Worktree.State.READY,
            worktree_path="/ws/4002-feat/backend",
        )
        candidate = _make_worktree(
            overlay="t3-heavy",
            ticket_number="4003",
            state=Worktree.State.PROVISIONED,
        )
        with pytest.raises(LocalStackLimitExceededError) as exc:
            check_local_stack_limit(candidate, limit=2)
        message = str(exc.value)
        assert "/ws/4001-feat/backend" in message
        assert "/ws/4002-feat/backend" in message
        del b1, b2
