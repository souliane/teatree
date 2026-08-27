"""What ``deploy/t3`` forwarded into the container, read once rather than per test file.

The wrapper forwards a BARE ``--env NAME``, which docker resolves from its own
environment — so the value is not in the argv, and a reader that scans the argv for
``NAME=value`` pairs sees nothing. Two test modules had their own copy of that reader;
when the wrapper stopped putting values in its argv, one was updated and the other
silently reported every forward missing. Same reason ``_deploy_wrapper_paths`` exists:
knowledge about the wrapper belongs in one place, or the copies drift.

``ENV_REPORT`` is appended to a docker stub so the stub resolves each forward the way
docker itself would — a ``NAME=value`` pair from the argv, a bare ``NAME`` from the
environment. Assertions built on :func:`forwarded` therefore measure DELIVERY and stay
honest across a change of mechanism, while :func:`argv` keeps the argv itself readable
for the assertion that a credential must never appear there.
"""

import subprocess

ENV_REPORT = r"""
while [ "$#" -gt 0 ]; do
    if [ "$1" = --env ] || [ "$1" = -e ]; then
        case "$2" in
        *=*) printf 'ENV %s\n' "$2" ;;
        *)
            name="$2"
            printf 'ENV %s=%s\n' "$name" "${!name-}"
            ;;
        esac
        shift
    fi
    shift
done
"""


def argv(proc: subprocess.CompletedProcess[str]) -> list[str]:
    """The argv the final ``docker`` hop was handed — what the host process table shows."""
    return [line.removeprefix("ARG ") for line in proc.stdout.splitlines() if line.startswith("ARG ")]


def forwarded(proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """The ``NAME``/value pairs the CONTAINER ends up with, however they crossed."""
    pairs = (line.removeprefix("ENV ") for line in proc.stdout.splitlines() if line.startswith("ENV "))
    return dict(pair.split("=", 1) for pair in pairs if "=" in pair)
