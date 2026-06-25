"""Forward a ``t3 eval run`` invocation into the CI image for the metered lane.

Split out of :mod:`teatree.cli.eval.app` so the command module stays under the
module-health LOC cap. The metered ``api`` lane runs in-container, never on the
host; the container is ephemeral (``--rm``), so the durable-history flags
(``--baseline`` / ``--gate-regressions``) are unsupported and the in-container run
is forced ``--no-persist``.
"""

import dataclasses
from pathlib import Path

import typer

from teatree.cli.eval.docker import ARTIFACTS_MOUNT, DockerUnavailableError, run_eval_in_docker
from teatree.cli.eval.metered_routing import should_route_to_docker
from teatree.eval.backends import TRANSCRIPT_BACKEND
from teatree.eval.parallel import DEFAULT_PARALLEL


@dataclasses.dataclass(frozen=True)
class RunDockerArgs:
    """The ``t3 eval run`` flags forwarded into the CI image by ``--docker``."""

    name: str | None
    lane: str | None
    shard: str | None
    output_format: str
    max_turns: int | None
    max_budget_usd: float
    effort: str
    trials: int
    require: str
    models: str | None
    backend: str
    require_executed: bool
    parallel: int
    transcript_html: Path | None = None
    summary_md: Path | None = None
    benchmark: bool = False
    model: str | None = None
    escalate_on_fail: bool = False
    escalate_trials: int = 3

    def _container_transcript_path(self) -> str:
        """The in-container path the transcript artifact is written to.

        The host ``--transcript-html`` path's PARENT is bind-mounted writable at
        :data:`ARTIFACTS_MOUNT`, so the in-container run writes to
        ``/artifacts/<filename>`` and the file lands back on the host. ``""``
        when no artifact was requested.
        """
        if self.transcript_html is None:
            return ""
        return f"{ARTIFACTS_MOUNT}/{self.transcript_html.name}"

    def _container_summary_path(self) -> str:
        """The in-container path the sanitized summary markdown is written to.

        Like the transcript artifact, the host ``--summary-md`` file lands in the
        single writable bind-mount, so it is redirected to ``/artifacts/<filename>``
        in-container and lands back on the host. The artifacts dir is the shared
        parent of the transcript and summary (the workflows put both in
        ``$RUNNER_TEMP``), so the one bind-mount carries both.
        """
        if self.summary_md is None:
            return ""
        return f"{ARTIFACTS_MOUNT}/{self.summary_md.name}"

    def _artifacts_dir(self) -> Path | None:
        if self.transcript_html is not None:
            return self.transcript_html.parent
        if self.summary_md is not None:
            return self.summary_md.parent
        return None

    def _leading_optionals(self) -> list[list[str]]:
        """Non-default flag groups that precede the always-present budget/effort flags."""
        return [
            [self.name] if self.name is not None else [],
            ["--lane", self.lane] if self.lane is not None else [],
            ["--shard", self.shard] if self.shard is not None else [],
            ["--benchmark"] if self.benchmark else [],
            ["--model", self.model] if self.model is not None else [],
            ["--format", self.output_format] if self.output_format != "text" else [],
            ["--max-turns", str(self.max_turns)] if self.max_turns is not None else [],
        ]

    def _trailing_optionals(self) -> list[list[str]]:
        """Non-default flag groups that follow the always-present budget/effort flags."""
        return [
            ["--trials", str(self.trials), "--require", self.require] if self.trials != 1 else [],
            ["--models", self.models] if self.models is not None else [],
            ["--backend", self.backend] if self.backend != TRANSCRIPT_BACKEND else [],
            ["--require-executed"] if self.require_executed else [],
            ["--parallel", str(self.parallel)] if self.parallel != DEFAULT_PARALLEL else [],
            ["--transcript-html", self._container_transcript_path()] if self.transcript_html is not None else [],
            ["--summary-md", self._container_summary_path()] if self.summary_md is not None else [],
            ["--escalate-on-fail", "--escalate-trials", str(self.escalate_trials)] if self.escalate_on_fail else [],
        ]

    def passthrough(self) -> list[str]:
        # --max-budget-usd / --effort are ALWAYS passed so the in-container run is
        # deterministic regardless of the container's env (the host resolved the
        # defaults); they sit between the leading and trailing optional groups.
        always = ["--max-budget-usd", str(self.max_budget_usd), "--effort", self.effort]
        groups = [*self._leading_optionals(), always, *self._trailing_optionals()]
        return ["run", *(arg for group in groups for arg in group), "--no-persist"]

    def dispatch(self) -> None:
        artifacts_dir = self._artifacts_dir()
        try:
            raise typer.Exit(code=run_eval_in_docker(self.passthrough(), artifacts_dir=artifacts_dir))
        except DockerUnavailableError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None


def run_in_docker_or_exit(
    args: RunDockerArgs,
    *,
    baseline: bool,
    gate_regressions: bool,
    gate_cost_regression: bool,
    gate_cost_bounds: bool,
) -> None:
    if baseline or gate_regressions or gate_cost_regression or gate_cost_bounds:
        typer.echo(
            "--docker runs in an ephemeral container, so it cannot update or read the durable "
            "run-history the gates consume; drop --baseline/--gate-regressions/"
            "--gate-cost-regression/--gate-cost-bounds or run on the host.",
            err=True,
        )
        raise typer.Exit(code=2)
    args.dispatch()


# ast-grep-ignore: ac-django-no-complexity-suppressions
def route_to_docker_if_needed(  # noqa: PLR0913 — each kwarg is one durable-history flag forwarded with the run args.
    args: RunDockerArgs,
    *,
    docker: bool,
    local: bool,
    metered: bool,
    baseline: bool,
    gate_regressions: bool,
    gate_cost_regression: bool,
    gate_cost_bounds: bool,
) -> None:
    """Forward the run into the CI image when the metered lane (or ``--docker``) requires it.

    A no-op on the host path. When routing, the durable-history flags are
    rejected first (:func:`run_in_docker_or_exit`) because the in-container run is
    ``--no-persist``, then the run is dispatched into the container (which exits
    the process).
    """
    if not (docker or should_route_to_docker(metered=metered, local=local)):
        return
    run_in_docker_or_exit(
        args,
        baseline=baseline,
        gate_regressions=gate_regressions,
        gate_cost_regression=gate_cost_regression,
        gate_cost_bounds=gate_cost_bounds,
    )
