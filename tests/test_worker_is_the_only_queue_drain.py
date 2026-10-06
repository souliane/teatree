# test-path: cross-cutting
# Drives hooks/scripts/hook_router.py main() and the worker's drain in teatree.core.tasks; no single src mirror.
"""The ``t3 worker`` is the only task-queue drain; no session drains, ticks or arms a loop.

A loop-owner session's Stop ends the turn with pending tasks queued and starts no ``t3 loop``
process, while the worker claims those rows as ``task-worker`` and records the attempts. No
SessionStart or PreCompact text asks a session to register a native loop, run a tick or claim.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.db.models.signals import post_save
from django.test import TestCase, override_settings

import hooks.scripts.hook_router as router
from hooks.scripts.stop_snapshot_slot import _MARKER_PREFIX
from teatree import answer_handback
from teatree.core import agent_runner
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.signals import _auto_enqueue_task
from teatree.core.tasks import drain_queue_body
from tests.teatree_core.conftest import CommandOverlay
from tests.teatree_hooks.test_standing_directives_delivery import registration_instructions

_PLUGIN_ROOT = Path(router.__file__).resolve().parents[2]
_OWNER = "the-loop-owner"
_PENDING = [{"task_id": 7, "subagent": "t3:coder", "phase": "coding", "issue_url": "https://example.test/issues/7"}]

_DRIVER = r"""
import sys
sys.path.insert(0, sys.argv[1])
import hooks.scripts.hook_router as router
sys.argv = ["hook_router.py", "--event", "Stop"]
router.main()
"""


@pytest.fixture
def owner_env(tmp_path: Path) -> dict[str, str]:
    """The live loop-registry owner, with a ``t3`` on PATH that records every call and reports pending work."""
    state, registry, bin_dir = tmp_path / "state", tmp_path / "registry", tmp_path / "bin"
    for directory in (state, registry, bin_dir):
        directory.mkdir()
    owner = {router._OWNER_LOOP: {"session_id": _OWNER, "pid": os.getpid()}}
    (registry / "loop-registry.json").write_text(json.dumps(owner), encoding="utf-8")
    (state / f"{_MARKER_PREFIX}{_OWNER}.stamp").write_text(str(time.time()), encoding="utf-8")
    calls = tmp_path / "t3-calls.log"
    fake_t3 = bin_dir / "t3"
    fake_t3.write_text(
        f"#!/bin/sh\necho \"$@\" >> {calls}\necho '{json.dumps(_PENDING)}'\n",
        encoding="utf-8",
    )
    fake_t3.chmod(0o755)
    return {
        "T3_HOOK_STATE_DIR": str(state),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state),
        "T3_LOOP_REGISTRY_DIR": str(registry),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "T3_CALLS_LOG": str(calls),
    }


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.jsonl"
    entry = {"type": "user", "origin": {"kind": "human"}, "timestamp": "2026-10-06T12:00:00Z"}
    path.write_text(json.dumps({**entry, "message": {"role": "user", "content": "carry on"}}) + "\n", encoding="utf-8")
    return path


def _stop(env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, str(_PLUGIN_ROOT)],
        input=json.dumps({"session_id": _OWNER, "transcript_path": str(_transcript(tmp_path))}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **env},
    )


def _t3_loop_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["T3_CALLS_LOG"])
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return [line for line in lines if line.split()[:1] == ["loop"]]


def test_the_loop_owner_ends_its_turn_with_pending_work_and_starts_no_loop_process(
    owner_env: dict[str, str], tmp_path: Path
) -> None:
    result = _stop(owner_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert '"decision": "block"' not in result.stdout
    assert "SELF-PUMP" not in result.stdout
    assert _t3_loop_calls(owner_env) == []


def test_control_the_same_stop_chain_still_hands_back_a_waiting_answer(
    owner_env: dict[str, str], tmp_path: Path
) -> None:
    answer_handback.post(session_id=_OWNER, question_id=7, answer="use postgres-1", env=owner_env)

    result = _stop(owner_env, tmp_path)

    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "use postgres-1" in decision["reason"]


class TheWorkerDrainsThePendingRows(TestCase):
    def setUp(self) -> None:
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.claimants: list[str] = []

        def _runner(task: Task, *, phase: str = "", overlay_skill_metadata: object = None) -> TaskAttempt:
            self.claimants.append(task.claimed_by)
            return task.complete_with_attempt(exit_code=0, result={"summary": "stubbed"})

        patcher = pytest.MonkeyPatch()
        patcher.setattr(agent_runner, "_runner", _runner)
        patcher.setattr("teatree.core.overlay_loader._discover_overlays", lambda: {"test": CommandOverlay()})
        self.addCleanup(patcher.undo)

    @override_settings(TASKS={"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}})
    def test_every_pending_row_is_claimed_by_the_task_worker_and_recorded(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        tasks = [Task.objects.create(ticket=ticket, session=session, phase="coding") for _ in range(2)]

        result = drain_queue_body()

        assert result["enqueued"] == [task.pk for task in tasks]
        assert self.claimants == ["task-worker", "task-worker"]
        assert TaskAttempt.objects.filter(task__in=tasks).count() == 2


# ── nothing asks a session to arm, tick or claim ─────────────────────


def _loop_arming_asks(rendered: str) -> list[str]:
    asks = registration_instructions(rendered)
    return asks + [phrase for phrase in ("loops tick", "claim-next") if phrase in rendered]


@pytest.fixture
def hook_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setattr(router, "_TMP_DIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "registry").mkdir()
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setattr(router, "_teatree_active", lambda session_id: True)
    monkeypatch.setattr(router, "_autoload_enabled", lambda: True)
    return state


def _session_start(capsys: pytest.CaptureFixture[str], session_id: str, source: str = "startup") -> str:
    router.handle_session_start_bootstrap({"session_id": session_id, "source": source})
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


def _owned_by(session_id: str) -> None:
    router._write_loop_registry({router._OWNER_LOOP: {"session_id": session_id, "pid": os.getpid()}})


@pytest.mark.usefixtures("hook_state")
class TestNoSessionIsAskedToArmALoop:
    def test_the_owner_is_told_the_worker_runs_the_loops(self, capsys: pytest.CaptureFixture[str]) -> None:
        context = _session_start(capsys, "owner-1")

        assert "t3 worker" in context
        assert _loop_arming_asks(context) == []

    def test_a_non_owner_is_told_the_worker_runs_the_loops(self, capsys: pytest.CaptureFixture[str]) -> None:
        _owned_by("owner-1")

        context = _session_start(capsys, "second-2")

        assert "owner-1" in context
        assert "t3 worker" in context
        assert _loop_arming_asks(context) == []

    def test_a_compacted_owner_recovers_its_slot_without_a_re_arm(self, capsys: pytest.CaptureFixture[str]) -> None:
        _owned_by("owner-1")
        router.handle_pre_compact({"session_id": "owner-1"})
        capsys.readouterr()

        context = _session_start(capsys, "owner-1", source="compact")

        assert "## Loop assignment" in context
        assert _loop_arming_asks(context) == []

    def test_the_pre_compact_snapshot_asks_for_no_re_arm(self, hook_state: Path) -> None:
        _owned_by("owner-1")

        router.handle_pre_compact({"session_id": "owner-1"})

        body = (hook_state / f"{router._T3_TEMP_PREFIX}owner-1-precompact.md").read_text(encoding="utf-8")
        assert "## Loop assignment" in body
        assert _loop_arming_asks(body) == []

    def test_control_a_re_added_registration_ask_is_caught(self, capsys: pytest.CaptureFixture[str]) -> None:
        context = _session_start(capsys, "owner-1")

        mutant = f"{context}\n\nEnsure each enabled loop's `/loop` is registered for this session."

        assert _loop_arming_asks(mutant) != []
