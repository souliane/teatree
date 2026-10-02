# test-path: cross-cutting
# Exercises the hooks/scripts/resume_admission.py hook leaf together with
# teatree.core.admission_governor and teatree.config.cold_reader — it spans the
# hook and both src packages.
"""SessionStart(resume) admission for the restored background fleet (#4108).

A harness restart restores every previously-running background agent at once. The
stagger the orchestrator applied belonged to the DISPATCH, not to the agents, so it is
not replayed — a fleet ramped up over many minutes comes back in a single step, and the
restore is not a dispatch, so no dispatch-side gate can see it.

These pin the whole path: which agents count as restored (dispatched MINUS terminated),
that the bound is the LIVE machine reading, that the advisory rides the one SessionStart
stdout write, and that every degraded read fails open rather than breaking SessionStart.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import resume_admission
from hooks.scripts.resume_admission import (
    handle_subagent_stop_track_agent,
    live_restored_agents,
    resume_admission_advisory,
)
from hooks.scripts.state_files import read_lines
from teatree.config import cold_reader
from teatree.core import admission_governor
from teatree.core.admission_governor import MachineSignal

_SESSION = "s-4108"


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path) -> None:
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)


def _machine(*, cores: int = 8, load1: float = 0.0) -> MachineSignal:
    return MachineSignal(cores=cores, load1=load1, ram_available_gb=None)


def _on_box(monkeypatch: pytest.MonkeyPatch, machine: MachineSignal) -> None:
    monkeypatch.setattr(admission_governor, "read_machine_signal", lambda **_kwargs: machine)


def _dispatched(*agent_ids: str) -> None:
    path = router._state_file(_SESSION, "agents")
    path.write_text("".join(f"{agent_id}\tdid a thing\n" for agent_id in agent_ids), encoding="utf-8")


def _fleet(size: int) -> None:
    _dispatched(*(f"a{i:04d}" for i in range(size)))


class TestRestoredFleetCount:
    """The restored set is what had NOT terminated — never the whole dispatch history."""

    def test_the_dispatch_ledger_is_the_starting_point(self) -> None:
        _fleet(5)
        assert live_restored_agents(_SESSION) == 5

    def test_a_terminated_agent_is_not_restored(self) -> None:
        _dispatched("a1", "a2", "a3")
        for agent_id in ("a1", "a2"):
            handle_subagent_stop_track_agent({"session_id": _SESSION, "agent_id": agent_id})
        assert live_restored_agents(_SESSION) == 1

    def test_a_re_fired_stop_is_recorded_once(self) -> None:
        _dispatched("a1", "a2")
        for _ in range(3):
            handle_subagent_stop_track_agent({"session_id": _SESSION, "agent_id": "a1"})
        assert read_lines(router._state_file(_SESSION, "agents-stopped")) == ["a1"]
        assert live_restored_agents(_SESSION) == 1

    def test_a_stop_naming_an_agent_we_never_recorded_never_goes_negative(self) -> None:
        _dispatched("a1")
        handle_subagent_stop_track_agent({"session_id": _SESSION, "agent_id": "unknown-id"})
        assert live_restored_agents(_SESSION) == 1

    def test_a_main_agent_stop_carries_no_agent_id_and_records_nothing(self) -> None:
        _dispatched("a1")
        handle_subagent_stop_track_agent({"session_id": _SESSION})
        assert live_restored_agents(_SESSION) == 1

    def test_no_ledger_at_all_is_an_empty_fleet(self) -> None:
        assert live_restored_agents(_SESSION) == 0


class TestResumeAdvisory:
    def test_an_over_ceiling_resume_names_the_count_and_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=20.0))
        _fleet(12)
        advisory = resume_admission_advisory(_SESSION, "resume")
        assert "12" in advisory
        assert "4" in advisory
        assert "shed" in advisory.lower()

    def test_a_resume_on_an_idle_host_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=0.0))
        _fleet(3)
        assert resume_admission_advisory(_SESSION, "resume") == ""

    def test_the_bound_is_the_live_reading_not_the_fleet_size_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same restored fleet, same cores — only the live load differs, and only one warns."""
        _fleet(6)
        _on_box(monkeypatch, _machine(cores=8, load1=0.0))
        assert resume_admission_advisory(_SESSION, "resume") == ""
        _on_box(monkeypatch, _machine(cores=8, load1=30.0))
        assert resume_admission_advisory(_SESSION, "resume") != ""

    @pytest.mark.parametrize("source", ["startup", "compact", "clear", ""])
    def test_only_a_resume_is_gated(self, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
        """A compact keeps the SAME process — it restores nothing, so it must stay silent."""
        _on_box(monkeypatch, _machine(cores=8, load1=58.0))
        _fleet(12)
        assert resume_admission_advisory(_SESSION, source) == ""

    def test_terminated_agents_do_not_warn_on_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The append-only dispatch ledger outlives the agents; only the live set restores."""
        _on_box(monkeypatch, _machine(cores=8, load1=20.0))
        _fleet(12)
        for i in range(10):
            handle_subagent_stop_track_agent({"session_id": _SESSION, "agent_id": f"a{i:04d}"})
        assert resume_admission_advisory(_SESSION, "resume") == ""

    def test_the_kill_switch_silences_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=58.0))
        _fleet(12)
        monkeypatch.setattr(cold_reader, "bool_setting", lambda *_a, **_kw: False)
        assert resume_admission_advisory(_SESSION, "resume") == ""

    def test_an_unreadable_ledger_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=58.0))
        _fleet(12)

        def _boom(*_args: object, **_kwargs: object) -> set[str]:
            raise OSError

        monkeypatch.setattr(resume_admission, "_ledger_ids", _boom)
        assert resume_admission_advisory(_SESSION, "resume") == ""

    def test_an_empty_session_id_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=58.0))
        assert resume_admission_advisory("", "resume") == ""


class TestWiring:
    def test_the_stop_tracker_joins_the_no_commit_handler(self) -> None:
        """Additive: the #1205 no-commit recorder keeps its registration and runs first."""
        registered = router._HANDLERS["SubagentStop"]
        assert registered[0] is router.handle_subagent_stop_no_commit
        assert handle_subagent_stop_track_agent in registered

    def test_the_advisory_rides_the_one_session_start_write(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=58.0))
        _fleet(12)
        monkeypatch.setattr(router, "_claim_session_handover", lambda _s: None)
        monkeypatch.setattr(router, "_autocompact_kill_switch_advisory", lambda: "")
        monkeypatch.setattr(router, "_account_switch_advisory", lambda: "")
        monkeypatch.setattr(router, "_mcp_connectivity_advisory", lambda: "")

        merged = router._merge_session_start_context("orientation", _SESSION, "resume")
        router._emit_session_start_context(merged)

        payload = json.loads(capsys.readouterr().out)
        emitted = payload["hookSpecificOutput"]["additionalContext"]
        assert "orientation" in emitted
        assert "RESTORED FLEET OVER CEILING" in emitted

    def test_a_non_resume_start_merges_exactly_as_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on_box(monkeypatch, _machine(cores=8, load1=58.0))
        _fleet(12)
        monkeypatch.setattr(router, "_claim_session_handover", lambda _s: None)
        monkeypatch.setattr(router, "_autocompact_kill_switch_advisory", lambda: "")
        monkeypatch.setattr(router, "_account_switch_advisory", lambda: "")
        monkeypatch.setattr(router, "_mcp_connectivity_advisory", lambda: "")
        assert router._merge_session_start_context("orientation", _SESSION, "startup") == "orientation"


_COLD_PROBE = textwrap.dedent(
    """
    import json, pathlib, sys

    repo = pathlib.Path(sys.argv[1]).resolve()
    src = (repo / "src").resolve()
    sys.path.insert(0, str(repo))
    sys.path[:] = [p for p in sys.path if p and pathlib.Path(p).resolve() != src]

    def purge():
        for name in [n for n in sys.modules if n == "teatree" or n.startswith("teatree.")]:
            del sys.modules[name]

    purge()
    control_failed = False
    try:
        import teatree.core.admission_governor
    except Exception:
        control_failed = True
    purge()

    from hooks.scripts.resume_admission import _governor_enabled, _shed_directive

    try:
        directive = _shed_directive(10000)
    except Exception as exc:
        directive = f"RAISED {type(exc).__name__}"
    probe = {
        "control_import_failed": control_failed,
        "directive": directive,
        "kill_switch_read": _governor_enabled(),
    }
    print(json.dumps(probe))
    """
)


class TestColdHookImports:
    """The hook runs in the user's session shell with no guarantee `teatree` imports (#1314).

    Without the shared `teatree_src_on_path` bootstrap the fail-open path swallows the
    ImportError and the gate is silently dead in exactly the environment it ships into —
    the failure a test run (where `src/` is already importable) cannot see. The probe
    carries its own CONTROL: it asserts a bare `import teatree...` FAILS under the same
    `sys.path` it then runs the module's own imports against, so a green here cannot mean
    "the harness never removed anything".
    """

    def test_the_teatree_imports_resolve_with_src_off_sys_path(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", _COLD_PROBE, str(repo_root)],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_root,
        )
        probe = json.loads(result.stdout)
        assert probe["control_import_failed"], "control did not reproduce the cold-hook path"
        assert "10000" in probe["directive"]
        assert isinstance(probe["kill_switch_read"], bool)
