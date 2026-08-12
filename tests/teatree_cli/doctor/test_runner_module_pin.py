"""``run_doctor_checks`` and the checks it calls by name live in ONE module.

The aggregate resolves ~50 probes as bare names in its own module globals, so a test
patching them on any OTHER module patches a name nobody reads — the check runs for
real and the test passes vacuously. Moving the runner between modules is therefore a
silent-green hazard, not just a relocation, and this pins the module the patch targets
must name.
"""

import teatree.cli.doctor.run_checks as runner_module
from teatree.cli.doctor.run_checks import run_doctor_checks

_RUNNER_MODULE = "teatree.cli.doctor.run_checks"

#: Names the aggregate references that are no probe — deferred module imports it binds
#: inside its own body, the exception it catches, and its output builtins.
_NOT_A_PROBE = frozenset(
    {
        "django",
        "teatree.core",
        "teatree.core.gates.schema_guard",
        "teatree.core.gates.clone_guard",
        "teatree.cli.doctor.self_heal",
        "teatree.cli.update",
        "doctor_check_self_db_migrations",
        "doctor_check_clone_currency",
        "run_self_heal_checks",
        "_collect_repos",
        "ImportError",
        "typer",
        "echo",
        "all",
    }
)


class TestTheRunnerOwnsItsProbeNamespace:
    def test_the_aggregate_is_defined_in_the_runner_module(self) -> None:
        assert run_doctor_checks.__module__ == _RUNNER_MODULE, (
            f"patch targets across the doctor tests name `{_RUNNER_MODULE}.<check>`; moving "
            "the runner strands every one of them on a name nobody reads"
        )

    def test_every_probe_it_calls_resolves_callable_on_that_module(self) -> None:
        stranded = sorted(
            name
            for name in run_doctor_checks.__code__.co_names
            if name not in _NOT_A_PROBE and not callable(getattr(runner_module, name, None))
        )

        assert not stranded, (
            f"these names are called by the aggregate but do not resolve on {_RUNNER_MODULE}: {stranded} — "
            "a half-moved import leaves the check unpatchable and the isolation vacuous"
        )
