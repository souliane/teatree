"""``t3 eval ci-trigger`` — dispatch the ``eval-ci-heal`` workflow against a PR branch.

Wraps ``gh workflow run eval-ci-heal.yml`` through :class:`GhCiEvalClient` (never a
raw ``gh``) and reports the head SHA the run keys on, so a caller (operator now,
the heal loop later) records the ``(branch, head_sha)`` the monitor resolves
against. Non-blocking: it dispatches and returns.
"""

import dataclasses
import json

import typer

from teatree.backends.github.ci_eval_client import (
    DEFAULT_CI_EVAL_REPO,
    EVAL_CI_HEAL_WORKFLOW,
    GhCiEvalClient,
    build_ci_eval_client,
)
from teatree.types import RawAPIDict

_CREDENTIALS = ("subscription_oauth", "metered_api_key")


@dataclasses.dataclass(frozen=True)
class CiTriggerReport:
    """The dispatch record the monitor keys the run on — ``(ref, head_sha)`` plus the inputs."""

    triggered: bool
    workflow: str
    ref: str
    head_sha: str
    scenarios: str
    credential: str

    def as_json(self) -> RawAPIDict:
        return dataclasses.asdict(self)


def _require_credential(credential: str) -> str:
    if credential not in _CREDENTIALS:
        typer.echo(f"unknown --credential {credential!r}; use one of {', '.join(_CREDENTIALS)}", err=True)
        raise typer.Exit(code=2)
    return credential


def trigger_ci_eval(client: GhCiEvalClient, *, ref: str, scenarios: str, credential: str) -> CiTriggerReport:
    """Dispatch the workflow for *ref* and return the record the monitor keys on.

    ``scenarios`` is the comma-joined red subset (empty = full suite); it is passed
    verbatim as the workflow's ``scenarios`` input. The head SHA is resolved after
    dispatch so the run can be matched by ``(branch, head_sha)`` on the next poll.
    """
    client.trigger_workflow(
        EVAL_CI_HEAL_WORKFLOW,
        ref=ref,
        inputs={"scenarios": scenarios, "credential": credential, "pr_ref": ref},
    )
    return CiTriggerReport(
        triggered=True,
        workflow=EVAL_CI_HEAL_WORKFLOW,
        ref=ref,
        head_sha=client.resolve_head_sha(ref),
        scenarios=scenarios,
        credential=credential,
    )


def ci_trigger(
    ref: str = typer.Option(..., "--ref", help="PR branch to run the behavioral eval against in CI."),
    scenarios: str = typer.Option(
        "",
        "--scenarios",
        help="Comma-joined scenario names to run (the red subset). Empty (default) = the full suite.",
    ),
    credential: str = typer.Option(
        "subscription_oauth",
        "--credential",
        help="Eval credential: subscription_oauth (default, no per-token bill) | metered_api_key.",
    ),
    repo: str = typer.Option(DEFAULT_CI_EVAL_REPO, "--repo", help="owner/repo the eval-ci-heal workflow lives in."),
) -> None:
    """Dispatch ``eval-ci-heal.yml`` for a PR branch and print the head SHA it keys on."""
    _require_credential(credential)
    report = trigger_ci_eval(build_ci_eval_client(repo), ref=ref, scenarios=scenarios, credential=credential)
    typer.echo(json.dumps(report.as_json(), indent=2))
