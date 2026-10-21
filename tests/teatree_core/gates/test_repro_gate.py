"""Forced-repro gate on ``ship()`` for FIX tickets (#118).

The gate refuses a FIX-ticket ship unless a provenance-verified RED->GREEN
``ReproEvidence`` pair exists (or a human-authorized ``ReproWaiver``). It is a
pure decision over durable state.

Symmetric corpus: must-BLOCK is a FIX ship with no/partial/invalid repro;
must-ALLOW is a FEATURE ticket, a valid pair, and a waiver. The FSM
integration proves the pure verdict actually gates the real ``ship()``
transition and is load-bearing.
"""

import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from teatree.agents.repro_phase_recorder import GREEN_REPLAY_OUTPUT_MAX_BYTES, _run_green_replay, record_phase_repro
from teatree.core.gates import dod_gate
from teatree.core.gates.repro_gate import ForcedReproGateError, check_forced_repro, check_red_repro
from teatree.core.models import ConfigSetting, HarnessRun, ReproEvidence, ReproWaiver, Session, Task, Ticket, Worktree
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from tests._loop_principal_env import pinned_loop_principal
from tests.factories import record_test_plan
from tests.teatree_core.models._shared import _advance_ticket_to_tested, _complete_phase_task, _init_repo_with_branch

_SHA_RED = "a" * 40
_SHA_GREEN = "b" * 40
_CMD = "uv run pytest tests/x.py::test_bug"


def test_green_replay_runs_real_bash_with_scrubbed_environment_and_deadline(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"REPLAY_SECRET": "worker-credential"}):
        passing = _run_green_replay(
            'test -n "$BASH_VERSION" && test -z "$REPLAY_SECRET" && printf passed && pwd', str(tmp_path), _SHA_GREEN
        )
        failing = _run_green_replay("printf failed; exit 7", str(tmp_path), _SHA_GREEN)
        timed_out = _run_green_replay("while :; do :; done", str(tmp_path), _SHA_GREEN, timeout=0.02)

    assert (passing.exit_code, passing.output) == (0, f"passed{tmp_path}\n")
    assert (failing.exit_code, failing.output) == (7, "failed")
    assert timed_out.exit_code == 124
    assert "timed out" in timed_out.output


def test_green_replay_records_shell_start_failure(tmp_path: Path) -> None:
    with patch("teatree.agents.repro_phase_recorder.run_bounded_group", side_effect=FileNotFoundError("bash")):
        replay = _run_green_replay("true", str(tmp_path), _SHA_GREEN)
    assert replay.exit_code == 127
    assert "could not start" in replay.output


def test_green_replay_preserves_worker_uv_python_and_tmpdir(tmp_path: Path) -> None:
    python_dir = tmp_path / "uv-python"
    with patch.dict(
        "os.environ", {"UV_PYTHON_INSTALL_DIR": str(python_dir), "TMPDIR": str(tmp_path), "REPLAY_SECRET": "hidden"}
    ):
        command = (
            f'test "$UV_PYTHON_INSTALL_DIR" = {shlex.quote(str(python_dir))} && '
            f'test "$TMPDIR" = {shlex.quote(str(tmp_path))} && test -z "$REPLAY_SECRET"'
        )
        replay = _run_green_replay(command, str(tmp_path), _SHA_GREEN)
    assert replay.exit_code == 0


def test_green_replay_caps_captured_output(tmp_path: Path) -> None:
    replay = _run_green_replay("head -c 100000 /dev/zero | tr '\\0' x", str(tmp_path), _SHA_GREEN)
    assert len(replay.output.encode()) <= GREEN_REPLAY_OUTPUT_MAX_BYTES


def test_green_replay_keeps_the_tail_where_a_test_run_summarises(tmp_path: Path) -> None:
    replay = _run_green_replay(
        "head -c 100000 /dev/zero | tr '\\0' x; printf '\\n=== 1 passed in 0.01s ===\\n'", str(tmp_path), _SHA_GREEN
    )

    assert len(replay.output.encode()) <= GREEN_REPLAY_OUTPUT_MAX_BYTES
    assert replay.output.endswith("=== 1 passed in 0.01s ===\n")


def test_a_stderr_flood_never_pushes_out_the_stdout_summary(tmp_path: Path) -> None:
    replay = _run_green_replay(
        "printf '=== 1 passed in 0.01s ===\\n'; head -c 100000 /dev/zero | tr '\\0' e >&2; printf 'ERR-END' >&2",
        str(tmp_path),
        _SHA_GREEN,
    )

    assert len(replay.output.encode()) <= GREEN_REPLAY_OUTPUT_MAX_BYTES
    assert "=== 1 passed in 0.01s ===" in replay.output
    assert replay.output.endswith("ERR-END")


def test_a_short_stderr_leaves_the_rest_of_the_room_to_stdout(tmp_path: Path) -> None:
    replay = _run_green_replay(
        "head -c 100000 /dev/zero | tr '\\0' o; printf 'ERR-ONLY' >&2", str(tmp_path), _SHA_GREEN
    )

    assert replay.output.endswith("ERR-ONLY")
    assert replay.output.count("o") > GREEN_REPLAY_OUTPUT_MAX_BYTES // 2
    assert len(replay.output.encode()) == GREEN_REPLAY_OUTPUT_MAX_BYTES


def test_a_timed_out_replay_keeps_its_reason_and_the_tail(tmp_path: Path) -> None:
    replay = _run_green_replay(
        "head -c 100000 /dev/zero | tr '\\0' x; printf 'LAST-LINE'; sleep 5", str(tmp_path), _SHA_GREEN, timeout=1
    )

    assert replay.exit_code == 124
    assert replay.output.startswith("GREEN replay timed out after 1s\n")
    assert replay.output.endswith("LAST-LINE")
    assert len(replay.output.encode()) <= GREEN_REPLAY_OUTPUT_MAX_BYTES


def test_a_uv_run_command_replays_for_real(tmp_path: Path) -> None:
    # The recorded RED is typically a `uv run ...` line; the scrubbed environment must still run one.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "replay-probe"\nversion = "0"\nrequires-python = ">=3.8"\ndependencies = []\n',
        encoding="utf-8",
    )
    command = "uv run --offline --no-project python -c 'import sys; print(\"replayed\", sys.version_info[0])'"

    replay = _run_green_replay(command, str(tmp_path), _SHA_GREEN, timeout=60)

    assert replay.exit_code == 0, replay.output
    assert replay.output.splitlines()[-1] == "replayed 3"


def _fix_ticket(**kwargs: object) -> Ticket:
    return Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX, **kwargs)


def _valid_pair(ticket: Ticket) -> None:
    ReproEvidence.record_red(ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="boom"))
    ReproEvidence.record_green(
        ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_GREEN, exit_code=0, output="ok"), red_is_ancestor=True
    )


class TestGateVerdict(TestCase):
    def test_coding_transition_refuses_fix_without_pre_fix_red(self) -> None:
        ticket = Ticket.objects.create(kind=Ticket.Kind.FIX, state=Ticket.State.PLAN_RECORDED)

        def gate(name: str):
            return check_red_repro if name == "red_repro" else lambda _ticket: None

        with (
            patch("teatree.core.models.ticket.get_gate", side_effect=gate),
            pytest.raises(ForcedReproGateError, match="record-red"),
        ):
            ticket.code()
        assert ticket.state == Ticket.State.PLAN_RECORDED

    def test_fix_with_no_evidence_under_flag_is_refused(self) -> None:
        # RED-1: a FIX ship with NO repro evidence and no waiver is refused.
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(_fix_ticket())

    def test_feature_ticket_is_never_gated(self) -> None:
        # GREEN-9: a FEATURE ticket is never gated.
        check_forced_repro(Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FEATURE))

    def test_valid_pair_passes(self) -> None:
        # GREEN-10: a provenance-verified RED->GREEN pair satisfies the gate.
        ticket = _fix_ticket()
        _valid_pair(ticket)
        check_forced_repro(ticket)  # does not raise

    def test_red_only_is_refused(self) -> None:
        # RED-6: a partial (red-only) row was never shown to go green.
        ticket = _fix_ticket()
        ReproEvidence.record_red(
            ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="boom")
        )
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(ticket)

    def test_hand_crafted_non_provenance_row_is_refused_at_the_gate(self) -> None:
        # RED-3 (gate layer): a directly-written row with both SHAs set but
        # provenance_ok=False must be rejected by the GATE itself, not only the
        # factory — the frozen ancestry proof is the gate's trust anchor.
        ticket = _fix_ticket()
        ReproEvidence.objects.create(
            ticket=ticket,
            command=_CMD,
            command_fingerprint="deadbeef",
            red_head_sha=_SHA_RED,
            red_exit_code=1,
            red_output_digest="x",
            green_head_sha=_SHA_GREEN,
            green_exit_code=0,
            green_output_digest="y",
            provenance_ok=False,
        )
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(ticket)

    def test_human_waiver_passes(self) -> None:
        # GREEN-11: a human-authorized waiver satisfies the gate with no evidence.
        ticket = _fix_ticket()
        ReproWaiver.record(ticket=ticket, approver_id="souliane", reason="hardware-timing race, not determinizable")
        check_forced_repro(ticket)  # does not raise

    def test_deny_message_names_both_remedies(self) -> None:
        with pytest.raises(ForcedReproGateError) as exc:
            check_forced_repro(_fix_ticket())
        message = str(exc.value)
        assert "record-red" in message
        assert "record-green" in message
        assert "waive" in message


class TestForcedReproGateSwitchedOff(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False, scope="acme")

    def test_coding_recorder_and_gate_log_one_exemption(self) -> None:
        ticket = _fix_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="tester")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        with self.assertLogs("teatree.core.gates.repro_gate", level="WARNING") as logs:
            assert record_phase_repro(task, phase="coding") == ""
            check_red_repro(ticket)
        assert len(logs.output) == 1
        assert ticket.extra["forced_repro_disabled_scopes"] == ["acme"]

    def test_alias_scope_is_reported_by_audit_and_status(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-acme", kind=Ticket.Kind.FIX)
        with self.assertLogs("teatree.core.gates.repro_gate", level="WARNING") as logs:
            check_red_repro(ticket)
        ticket.refresh_from_db()
        assert "scope acme" in logs.output[0]
        assert ticket.extra["forced_repro_disabled_scopes"] == ["acme"]
        status = call_command("repro", "status", str(ticket.pk))
        assert "forced-repro disabled by: acme" in status
        assert "disabled scopes recorded: acme" in status

    def test_the_ship_gate_passes_a_fix_with_no_repro(self) -> None:
        ConfigSetting.objects.filter(key="forced_repro_gate_enabled", scope="acme").delete()
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        ticket = _fix_ticket()
        with self.assertLogs("teatree.core.gates.repro_gate", level="WARNING"):
            check_forced_repro(ticket)
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False, scope="acme")
        check_forced_repro(ticket)
        ticket.refresh_from_db()
        assert not ReproEvidence.objects.filter(ticket=ticket).exists()
        assert ticket.extra["forced_repro_disabled_scopes"] == ["global", "acme"]

    def test_the_coding_gate_passes_a_fix_with_no_red(self) -> None:
        ticket = _fix_ticket()
        record_test_plan(ticket)
        ticket.code_direct()
        ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert not ReproEvidence.objects.filter(ticket=ticket).exists()
        assert ticket.extra["forced_repro_disabled_scopes"] == ["acme"]

    def test_another_overlays_fix_is_still_refused_by_both_gates(self) -> None:
        acme = _fix_ticket()
        other = Ticket.objects.create(overlay="other", kind=Ticket.Kind.FIX)
        check_red_repro(acme)
        check_forced_repro(acme)
        with pytest.raises(ForcedReproGateError):
            check_red_repro(other)
        with pytest.raises(ForcedReproGateError):
            check_forced_repro(other)

    def test_off_marker_survives_turning_the_setting_back_on(self) -> None:
        ticket = _fix_ticket()
        check_red_repro(ticket)
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=True, scope="acme")
        ticket.refresh_from_db()
        assert ticket.extra["forced_repro_disabled_scopes"] == ["acme"]
        with pytest.raises(ForcedReproGateError):
            check_red_repro(ticket)

    def test_a_fix_reaches_tested_with_no_evidence_when_switched_off_globally(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        ticket = Ticket.objects.create(kind=Ticket.Kind.FIX)
        _advance_ticket_to_tested(ticket)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        assert not ReproEvidence.objects.filter(ticket=ticket).exists()
        assert ticket.extra["forced_repro_disabled_scopes"] == ["global"]

    def test_the_unattended_loop_cannot_switch_it_off(self) -> None:
        with pinned_loop_principal(LOOP_RUNNER_SESSION_ID), pytest.raises(ValidationError, match="authorization"):
            ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False, scope="other")
        assert not ConfigSetting.objects.filter(key="forced_repro_gate_enabled", scope="other").exists()

    def test_existing_red_waiver_and_valid_pair_do_not_mark_a_disabled_gate(self) -> None:
        with_red = _fix_ticket()
        ReproEvidence.record_red(
            ticket=with_red, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="failed")
        )
        check_red_repro(with_red)
        with_red.refresh_from_db()
        assert "forced_repro_disabled_scopes" not in with_red.extra

        waived = _fix_ticket()
        ReproWaiver.record(ticket=waived, approver_id="souliane", reason="hardware race")
        check_red_repro(waived)
        check_forced_repro(waived)
        waived.refresh_from_db()
        assert "forced_repro_disabled_scopes" not in waived.extra

        paired = _fix_ticket()
        _valid_pair(paired)
        check_red_repro(paired)
        check_forced_repro(paired)
        paired.refresh_from_db()
        assert "forced_repro_disabled_scopes" not in paired.extra

    def test_global_override_is_the_recorded_scope_for_an_overlay_ticket(self) -> None:
        ConfigSetting.objects.filter(key="forced_repro_gate_enabled", scope="acme").delete()
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        ticket = _fix_ticket()
        check_red_repro(ticket)
        ticket.refresh_from_db()
        assert ticket.extra["forced_repro_disabled_scopes"] == ["global"]
        with patch.object(Ticket, "merge_extra", side_effect=AssertionError("duplicate marker write")):
            check_forced_repro(ticket)

    def test_malformed_value_refuses_instead_of_passing(self) -> None:
        ConfigSetting.objects.filter(key="forced_repro_gate_enabled", scope="acme").update(value="not-a-bool")
        with pytest.raises(ForcedReproGateError, match="Cannot decide forced-repro requirement"):
            check_red_repro(_fix_ticket())


class TestShipTransitionReproGate(TestCase):
    """The real FSM ``ship()`` path enforces the gate (#118)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _reviewed_fix_ticket(self, *, with_repro: bool = True) -> Ticket:
        ticket = Ticket.objects.create(kind=Ticket.Kind.FIX)
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=1)
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        if with_repro:
            _valid_pair(ticket)
        _advance_ticket_to_tested(ticket)
        if with_repro:
            ReproEvidence.objects.filter(ticket=ticket).delete()
        ticket.record_review_context(
            work_item="https://example.test/issues/1",
            documents=["https://example.test/spec"],
            analysis="Compared the fix against the recorded issue and spec",
        )
        ticket.record_anti_vacuity_attestation("a" * 40, "The fix covers the issue AC", [], no_new_tests=True)
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SELF_REVIEWED
        return ticket

    def test_ship_refused_for_fix_without_repro_under_flag(self) -> None:
        # RED-1 (FSM): the block rolls back — ticket stays SELF_REVIEWED.
        ticket = self._reviewed_fix_ticket()
        with patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]), pytest.raises(ForcedReproGateError):
            ticket.ship()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_turning_the_gate_back_on_refuses_a_fix_coded_without_red(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        ticket = self._reviewed_fix_ticket(with_repro=False)
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=True)
        with patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]), pytest.raises(ForcedReproGateError):
            ticket.refuse_unless_shippable()
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_ship_proceeds_with_valid_repro_under_flag(self) -> None:
        # GREEN-10 (FSM): a provenance-verified pair lets the FIX ship.
        ticket = self._reviewed_fix_ticket()
        _valid_pair(ticket)
        with (
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.ship()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PR_OPENED

    def test_testing_phase_executes_green_and_fix_ships_without_manual_green_record(self) -> None:
        ticket = self._reviewed_fix_ticket()
        ReproEvidence.record_red(
            ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="pre-fix failure")
        )
        session = Session.objects.create(ticket=ticket, agent_id="tester")
        task = Task.objects.create(ticket=ticket, session=session, phase="testing", execution_reason="test the fix")
        with (
            patch("teatree.agents.repro_phase_recorder.git.head_sha", return_value=_SHA_GREEN),
            patch("teatree.agents.repro_phase_recorder.git.check", return_value=True),
            patch(
                "teatree.agents.repro_phase_recorder.run_bounded_group",
                return_value=SimpleNamespace(returncode=0, stdout="green", stderr=""),
            ) as run,
        ):
            assert record_phase_repro(task, phase="testing") == ""
        run.assert_called_once()
        assert ReproEvidence.objects.has_valid_repro(ticket)
        with (
            patch.object(dod_gate, "frontend_repos_for_overlay", return_value=[]),
            self.captureOnCommitCallbacks(execute=False),
        ):
            ticket.ship()
            ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PR_OPENED

    def test_testing_phase_records_a_failed_replay_when_bash_cannot_start(self) -> None:
        ticket = self._reviewed_fix_ticket()
        red = ReproEvidence.record_red(
            ticket=ticket, command=_CMD, run=HarnessRun(head_sha=_SHA_RED, exit_code=1, output="pre-fix failure")
        )
        session = Session.objects.create(ticket=ticket, agent_id="tester")
        task = Task.objects.create(ticket=ticket, session=session, phase="testing", execution_reason="test the fix")
        with (
            patch("teatree.agents.repro_phase_recorder.git.head_sha", return_value=_SHA_GREEN),
            patch("teatree.agents.repro_phase_recorder.run_bounded_group", side_effect=FileNotFoundError("bash")),
        ):
            refusal = record_phase_repro(task, phase="testing")
        red.refresh_from_db()
        assert "could not start" in refusal
        assert red.green_exit_code == 127
        assert red.provenance_ok is False
