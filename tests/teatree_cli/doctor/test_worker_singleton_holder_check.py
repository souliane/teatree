"""`t3 doctor check` compares the singleton's holder against who should hold it (#3976).

A `t3 worker` started outside the container holds the worker singleton over a lock file
the host and the deployment share, and the containerized service can then never start.
Every other surface reads healthy — `t3 loops list` shows loops ticking (the outside
worker drives them), `t3 worker status` reports RUNNING (the flock genuinely is held),
the service is Up (it had just restarted) — so this check is the only place the split
between "who holds it" and "who is supposed to hold it" becomes visible.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.doctor.checks_runtime import _check_worker_singleton_holder
from teatree.utils.singleton import WORKER_SINGLETON, ExecutionContext, singleton
from teatree.utils.singleton_refusals import ESCALATION_THRESHOLD, record_refusal

_IN_THE_DEPLOYMENT = {"TEATREE_ROLE": "worker"}
_OFF_A_DEPLOYMENT: dict[str, str] = {}


def _record(role: str, *, pid: int = 4321, namespace: str = "pid:[1]") -> str:
    context = ExecutionContext(pid_namespace=namespace, hostname="box", role=role)
    return f"{pid}\n{json.dumps(context.as_json())}\n"


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "worker.refusals.json"


class TestHolderAttribution:
    def test_fails_when_the_holder_is_outside_the_deployment(
        self, tmp_path: Path, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "worker.pid"
        with singleton(WORKER_SINGLETON, pid_path=path):
            path.write_text(_record(role=""), encoding="utf-8")
            verdict = _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger)
        output = capsys.readouterr().out
        assert verdict is False
        assert "FAIL" in output
        assert "PID 4321" in output
        assert "pid:[1]" in output

    def test_fails_when_a_sibling_deployment_service_holds_it(self, tmp_path: Path, ledger: Path) -> None:
        path = tmp_path / "worker.pid"
        with singleton(WORKER_SINGLETON, pid_path=path):
            path.write_text(_record(role="admin"), encoding="utf-8")
            assert _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger) is False

    def test_fails_when_the_holder_cannot_be_attributed(
        self, tmp_path: Path, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "worker.pid"
        with singleton(WORKER_SINGLETON, pid_path=path):
            path.write_text("4321\n", encoding="utf-8")  # a pre-record holder
            verdict = _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger)
        assert verdict is False
        assert "cannot be attributed" in capsys.readouterr().out

    def test_passes_for_the_deployed_worker(self, tmp_path: Path, ledger: Path) -> None:
        path = tmp_path / "worker.pid"
        with singleton(WORKER_SINGLETON, pid_path=path):
            path.write_text(_record(role="worker"), encoding="utf-8")
            assert _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger) is True

    def test_a_bare_host_worker_is_the_sanctioned_holder_off_a_deployment(self, tmp_path: Path, ledger: Path) -> None:
        # With no TEATREE_ROLE there is no deployment to be outside OF — a dev box's own
        # `t3 worker` is exactly who should hold it, so the check must stay silent.
        path = tmp_path / "worker.pid"
        with singleton(WORKER_SINGLETON, pid_path=path):
            path.write_text(_record(role=""), encoding="utf-8")
            assert _check_worker_singleton_holder(env=_OFF_A_DEPLOYMENT, pid_path=path, refusal_path=ledger) is True

    def test_a_free_flock_has_no_holder_to_judge(self, tmp_path: Path, ledger: Path) -> None:
        path = tmp_path / "worker.pid"
        path.write_text(_record(role=""), encoding="utf-8")
        assert _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger) is True


class TestRefusalStreak:
    def test_fails_on_a_standing_streak(self, tmp_path: Path, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        for _ in range(ESCALATION_THRESHOLD):
            record_refusal(WORKER_SINGLETON, fingerprint="foreign", path=ledger)
        path = tmp_path / "worker.pid"
        verdict = _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger)
        output = capsys.readouterr().out
        assert verdict is False
        assert f"{ESCALATION_THRESHOLD} times running" in output

    def test_a_streak_below_the_threshold_is_not_a_failure(self, tmp_path: Path, ledger: Path) -> None:
        record_refusal(WORKER_SINGLETON, fingerprint="foreign", path=ledger)
        path = tmp_path / "worker.pid"
        assert _check_worker_singleton_holder(env=_IN_THE_DEPLOYMENT, pid_path=path, refusal_path=ledger) is True

    def test_a_streak_is_reported_even_off_a_deployment(self, tmp_path: Path, ledger: Path) -> None:
        # The streak is evidence a worker HERE could not start, whatever this box is.
        for _ in range(ESCALATION_THRESHOLD):
            record_refusal(WORKER_SINGLETON, fingerprint="foreign", path=ledger)
        path = tmp_path / "worker.pid"
        assert _check_worker_singleton_holder(env=_OFF_A_DEPLOYMENT, pid_path=path, refusal_path=ledger) is False


class TestCrashProofing:
    def test_an_unreadable_probe_degrades_to_a_warning(
        self, tmp_path: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        unreadable = OSError("procfs unreadable")

        def _boom(*_args: object, **_kwargs: object) -> bool:
            raise unreadable

        monkeypatch.setattr("teatree.utils.singleton.flock_is_held", _boom)
        verdict = _check_worker_singleton_holder(
            env=_IN_THE_DEPLOYMENT, pid_path=tmp_path / "worker.pid", refusal_path=ledger
        )
        assert verdict is True
        assert "WARN" in capsys.readouterr().out
