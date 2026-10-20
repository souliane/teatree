"""Guard: every CI pytest reaches a shared worker-pool root, or ``tests/conftest.py`` refuses it."""

from tests._actions_workflow import load


def _pytest_steps() -> list[tuple[str, str]]:
    return [
        (f"{job_name}: {step.get('name', '<unnamed>')}", str(step["run"]))
        for job_name, job in load()["jobs"].items()
        for step in job.get("steps", [])
        if "pytest" in str(step.get("run", ""))
    ]


class TestPytestWorkerPoolRoot:
    def test_host_steps_inherit_the_runner_local_root(self) -> None:
        assert load()["env"]["TEATREE_PYTEST_WORKER_POOL_ROOT"] == "/tmp"
        assert "T3_LOOP_REGISTRY_DIR" not in load()["env"]

    def test_every_dockerized_pytest_forwards_the_root(self) -> None:
        dockerized = [(name, run) for name, run in _pytest_steps() if "docker run" in run]
        assert dockerized
        missing = [name for name, run in dockerized if "-e TEATREE_PYTEST_WORKER_POOL_ROOT=/tmp" not in run]
        assert missing == []
