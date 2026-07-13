"""``t3 eval ci-status`` — resolve one ``eval-ci-heal`` run and return its verdict.

Non-blocking: one ``gh run view`` (resolving the newest run for the branch, or an
explicit ``--run`` id) yields the structured verdict (status / conclusion /
head_sha / url). On a completed FAILURE it also downloads the publish-safe
``eval-heal-<sha>`` JSON and surfaces each red scenario with the ``triage_class``
:func:`teatree.eval.triage.classify_red` embedded at render time — the loop never
re-derives it. A download that fails is surfaced loud (a note), never a silent
empty red set.
"""

import dataclasses
import json
import tempfile
from pathlib import Path
from typing import cast

import typer

from teatree.backends.github.ci_eval_client import (
    DEFAULT_CI_EVAL_REPO,
    EVAL_CI_HEAL_WORKFLOW,
    GhCiEvalClient,
    build_ci_eval_client,
)
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError


@dataclasses.dataclass(frozen=True)
class RedScenario:
    name: str
    lane: str
    triage_class: str


@dataclasses.dataclass(frozen=True)
class CiStatusReport:
    """The resolved verdict of one run, plus the reds parsed from its JSON artifact."""

    found: bool
    ref: str
    run_id: int | None
    status: str
    conclusion: str
    head_sha: str
    url: str
    #: ``None`` when reds do not apply (not a failure) or could not be fetched;
    #: an (possibly empty) tuple when the JSON artifact was parsed.
    reds: tuple[RedScenario, ...] | None = None
    #: A loud note when a failure's reds could not be downloaded — never silently
    #: collapsed to an empty red set.
    note: str = ""

    def as_json(self) -> RawAPIDict:
        return {
            "found": self.found,
            "ref": self.ref,
            "run_id": self.run_id,
            "status": self.status,
            "conclusion": self.conclusion,
            "head_sha": self.head_sha,
            "url": self.url,
            "reds": None if self.reds is None else [dataclasses.asdict(r) for r in self.reds],
            "note": self.note,
        }


def _resolve_run(client: GhCiEvalClient, *, ref: str, run_id: str | None) -> tuple[int | None, RawAPIDict | None]:
    """Resolve to (run_id, run-json). An explicit id wins; else the newest branch run."""
    if run_id is not None:
        return int(run_id), client.view_run(run_id)
    runs = client.list_runs(EVAL_CI_HEAL_WORKFLOW, branch=ref)
    if not runs:
        return None, None
    database_id = runs[0].get("databaseId")
    resolved = int(database_id) if isinstance(database_id, int) else None
    return resolved, client.view_run(database_id)  # type: ignore[arg-type]


def _reds_from_payload(payload: RawAPIDict) -> tuple[RedScenario, ...]:
    """The scenarios carrying a non-null ``triage_class`` — the reds, verbatim from the JSON."""
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return ()
    reds: list[RedScenario] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        record = cast("RawAPIDict", scenario)
        triage_class = record.get("triage_class")
        if triage_class is None:
            continue
        reds.append(
            RedScenario(
                name=str(record.get("name", "")),
                lane=str(record.get("lane", "")),
                triage_class=str(triage_class),
            )
        )
    return tuple(reds)


def _fetch_reds(
    client: GhCiEvalClient, *, run_id: int | None, head_sha: str
) -> tuple[tuple[RedScenario, ...] | None, str]:
    """Download the ``eval-heal-<sha>`` JSON and parse its reds, or a loud note on failure."""
    if run_id is None or not head_sha:
        return None, "no run id / head SHA to resolve the eval-heal artifact"
    with tempfile.TemporaryDirectory() as scratch:
        dest = Path(scratch)
        try:
            client.download_artifact(run_id, name=f"eval-heal-{head_sha}", dest_dir=dest)
        except (CommandFailedError, FileNotFoundError) as exc:
            return None, f"could not download eval-heal-{head_sha}: {exc}"
        artifacts = sorted(dest.rglob("*.json"))
        if not artifacts:
            return None, f"eval-heal-{head_sha} carried no JSON"
        # A full-suite run drops one JSON; a targeted subset loop drops one per
        # scenario — read every file so no red is missed.
        reds = tuple(red for artifact in artifacts for red in _reds_from_payload(_load_payload(artifact)))
    return reds, ""


def _load_payload(artifact: Path) -> RawAPIDict:
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def resolve_ci_status(client: GhCiEvalClient, *, ref: str, run_id: str | None) -> CiStatusReport:
    """Resolve the run and, on a completed failure, its reds — one view, non-blocking."""
    resolved_id, run = _resolve_run(client, ref=ref, run_id=run_id)
    if run is None:
        return CiStatusReport(found=False, ref=ref, run_id=None, status="", conclusion="", head_sha="", url="")
    conclusion = str(run.get("conclusion") or "")
    head_sha = str(run.get("headSha") or "")
    reds: tuple[RedScenario, ...] | None = None
    note = ""
    if conclusion == "failure":
        reds, note = _fetch_reds(client, run_id=resolved_id, head_sha=head_sha)
    return CiStatusReport(
        found=True,
        ref=ref,
        run_id=resolved_id,
        status=str(run.get("status") or ""),
        conclusion=conclusion,
        head_sha=head_sha,
        url=str(run.get("url") or ""),
        reds=reds,
        note=note,
    )


def _render_text(report: CiStatusReport) -> str:
    if not report.found:
        return f"no eval-ci-heal run found for {report.ref!r}"
    lines = [f"{report.ref}: status={report.status} conclusion={report.conclusion or '-'} ({report.url})"]
    if report.note:
        lines.append(f"  note: {report.note}")
    lines.extend(f"  RED {red.name} [{red.lane}] -> {red.triage_class}" for red in report.reds or ())
    return "\n".join(lines)


def ci_status(
    ref: str = typer.Option(..., "--ref", help="PR branch whose newest eval-ci-heal run to resolve."),
    run: str | None = typer.Option(None, "--run", help="Explicit run id (else the newest run for --ref)."),
    output_json: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False, "--json", help="Emit the structured verdict as JSON."
    ),
    repo: str = typer.Option(DEFAULT_CI_EVAL_REPO, "--repo", help="owner/repo the eval-ci-heal workflow lives in."),
) -> None:
    """Resolve one eval-ci-heal run's verdict (and, on failure, its triaged reds)."""
    report = resolve_ci_status(build_ci_eval_client(repo), ref=ref, run_id=run)
    typer.echo(json.dumps(report.as_json(), indent=2) if output_json else _render_text(report))
