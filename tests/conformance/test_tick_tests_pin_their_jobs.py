"""No test runs a DEFAULT tick, because a default tick scans the machine it runs on.

``build_default_jobs`` wires the REAL global scanners — self-update walks every
editable clone, the idle-stack reaper shells ``docker ps``, intake talks to the
forge — and ``scan_phase`` fans them out across a worker pool. A pool thread owns
its own DB connection, so whatever those scanners write is COMMITTED outside the
test's transaction and survives into the tests that follow: the leaked ``BotPing``
and ``LocalStackReaperMarker`` rows behind souliane/teatree#4756 / #4758, which
turned the shuffle lane red on seeds 1 and 7 while every file passed alone.

A tick test therefore pins the jobs it means to run: ``TickRequest(scanners=[...])``
or the ``jobs_builder`` seam. This walks the test tree and holds that line.
"""

import ast
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_TICK_MODULE = "teatree.loop.tick"


def _imports_run_tick(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == _TICK_MODULE
        and any(alias.name == "run_tick" for alias in node.names)
        for node in ast.walk(tree)
    )


def _pins_its_jobs(call: ast.Call) -> bool:
    if any(kw.arg == "jobs_builder" for kw in call.keywords):
        return True
    requests = [arg for arg in call.args if isinstance(arg, ast.Call)]
    requests += [kw.value for kw in call.keywords if kw.arg == "request" and isinstance(kw.value, ast.Call)]
    return any(kw.arg == "scanners" for request in requests for kw in request.keywords)


def unpinned_tick_calls(source: str, label: str) -> list[str]:
    tree = ast.parse(source)
    if not _imports_run_tick(tree):
        return []
    return [
        f"{label}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_tick"
        and not _pins_its_jobs(node)
    ]


class TestEveryTickTestPinsItsJobs:
    def test_no_test_runs_the_default_job_set(self) -> None:
        offenders = [
            offender
            for path in sorted(_TESTS_ROOT.rglob("test_*.py"))
            for offender in unpinned_tick_calls(path.read_text(encoding="utf-8"), str(path.relative_to(_TESTS_ROOT)))
        ]
        assert offenders == [], (
            "these run_tick calls build the DEFAULT job set, so they scan this machine's "
            f"clones and docker stacks and commit rows from pool threads: {offenders}"
        )

    def test_the_detector_flags_an_unpinned_call(self) -> None:
        source = "from teatree.loop.tick import TickRequest, run_tick\nrun_tick(TickRequest(backends=[]))\n"

        assert unpinned_tick_calls(source, "sample.py") == ["sample.py:2"]

    def test_both_pinning_forms_are_accepted(self) -> None:
        pinned = (
            "from teatree.loop.tick import TickRequest, run_tick\n"
            "run_tick(TickRequest(scanners=[]))\n"
            "run_tick(TickRequest(backends=[]), jobs_builder=builder)\n"
        )

        assert unpinned_tick_calls(pinned, "sample.py") == []
