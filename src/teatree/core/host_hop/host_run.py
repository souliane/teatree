"""Handing an overlay tool's host-side half to ``deploy/t3`` — the generic hop.

Some overlay tool leaves cannot finish inside the container: they need a binary, a
credential, a loopback port or a browser that only exist on the operator's machine.
The container half resolves everything that is decidable there and STAGES a plan; the
wrapper — the one layer executing on the host — carries it out.

The shape is ``t3 admin``'s and ``t3 peer``'s, generalised: the CLI writes, the host
runner reads, and the runner ships verbatim beside the tool that owns it so no
coordinate of any particular box enters this module.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

from teatree import paths
from teatree.utils.ports import running_in_container
from teatree.utils.run import run_streamed

#: The staging directory under the TRUE canonical data dir, which is bind-mounted, so
#: the host wrapper reads the bytes the container wrote. Spelled identically in
#: ``deploy/t3``; ``test_stack_plan_contract.py`` pins the two together.
PLAN_DIR = "host-run"

#: The runner copy the wrapper executes, and the plan it is handed.
RUNNER_NAME = "runner.sh"
PLAN_NAME = "plan"


def plan_dir() -> Path:
    return paths.PathHelpers.true_canonical_data_dir() / PLAN_DIR


def stage(runner: Path, plan: str, files: Mapping[str, str]) -> Path:
    """Write *runner*, *plan* and *files* into a freshly-emptied plan dir; answer where.

    Emptied first: a sibling left by a previous run is a credential file the next run
    never meant to ship, and the wrapper cannot tell one from the other.
    """
    target = plan_dir()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    staged_runner = target / RUNNER_NAME
    shutil.copyfile(runner, staged_runner)
    staged_runner.chmod(0o700)

    for name, body in {PLAN_NAME: plan, **files}.items():
        written = target / name
        written.write_text(body, encoding="utf-8")
        written.chmod(0o600)
    return target


def dispatch(runner: Path, plan: str, files: Mapping[str, str]) -> int:
    """Stage the plan, and run it here when here is the host.

    In a container the wrapper runs it after this process exits, so staging IS the
    whole action; a native install has no wrapper, so it runs the runner itself.
    """
    staged = stage(runner, plan, files)
    if running_in_container():
        return 0
    return run_streamed(["bash", str(staged / RUNNER_NAME), str(staged)], check=False)


__all__ = ["PLAN_DIR", "PLAN_NAME", "RUNNER_NAME", "dispatch", "plan_dir", "stage"]
