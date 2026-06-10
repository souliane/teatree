"""Forward a ``t3 eval run`` invocation into the CI image for the metered lane.

Split out of :mod:`teatree.cli.eval.app` so the command module stays under the
module-health LOC cap. The metered ``sdk`` lane runs in-container, never on the
host; the container is ephemeral (``--rm``), so the durable-history flags
(``--baseline`` / ``--gate-regressions``) are unsupported and the in-container run
is forced ``--no-persist``.
"""

import dataclasses

import typer

from teatree.cli.eval.docker import DockerUnavailableError, run_eval_in_docker
from teatree.eval.backends import SUBSCRIPTION_BACKEND
from teatree.eval.parallel import DEFAULT_PARALLEL


@dataclasses.dataclass(frozen=True)
class RunDockerArgs:
    """The ``t3 eval run`` flags forwarded into the CI image by ``--docker``."""

    name: str | None
    output_format: str
    max_turns: int | None
    trials: int
    require: str
    models: str | None
    backend: str
    require_executed: bool
    parallel: int

    def passthrough(self) -> list[str]:
        args = ["run"]
        if self.name is not None:
            args.append(self.name)
        if self.output_format != "text":
            args += ["--format", self.output_format]
        if self.max_turns is not None:
            args += ["--max-turns", str(self.max_turns)]
        if self.trials != 1:
            args += ["--trials", str(self.trials), "--require", self.require]
        if self.models is not None:
            args += ["--models", self.models]
        if self.backend != SUBSCRIPTION_BACKEND:
            args += ["--backend", self.backend]
        if self.require_executed:
            args.append("--require-executed")
        if self.parallel != DEFAULT_PARALLEL:
            args += ["--parallel", str(self.parallel)]
        args.append("--no-persist")
        return args

    def dispatch(self) -> None:
        try:
            raise typer.Exit(code=run_eval_in_docker(self.passthrough()))
        except DockerUnavailableError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None


def run_in_docker_or_exit(
    args: RunDockerArgs, *, baseline: bool, gate_regressions: bool, gate_cost_regression: bool
) -> None:
    if baseline or gate_regressions or gate_cost_regression:
        typer.echo(
            "--docker runs in an ephemeral container, so it cannot update or compare the "
            "durable baseline; drop --baseline/--gate-regressions/--gate-cost-regression or run on the host.",
            err=True,
        )
        raise typer.Exit(code=2)
    args.dispatch()
