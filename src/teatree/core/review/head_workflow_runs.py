"""The live CI read at ONE head, off GitHub's workflow-run surface (#4554).

The authoritative surface for "was anything actually red at this commit?" is
``actions/runs?head_sha=``, not the ``check-runs`` tally: the tally has served false
``0 pending`` reads on this repo, and a naive read that reported a green which is not
green would make the refusal it feeds CONFIDENTLY wrong rather than merely unverified.

:meth:`~teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status` is deliberately
NOT reused. It answers a different question at a different commit: it is scoped to the
pull request's CURRENT head (the reviewed tree may have moved) and reads the check-runs
tally this module exists to avoid.

Eventual consistency runs in the direction that looks like success — the run rows may not
exist yet at a head whose CI is about to go red — so "no runs at all" is UNREADABLE, never
green. The mirror hazard is a stale failure: a workflow re-run green mints a fresh run row,
so the runs are deduped to the newest per ``(workflow_id, event)`` before classification.
"""

import json
import logging
import shutil
from typing import TypedDict, cast

from teatree.core.forge_pr_probe import forge_cli_env
from teatree.core.modelkit.forge_readability import LiveChecksRead
from teatree.core.models import MergeClear
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)


class WorkflowRun(TypedDict, total=False):
    """One ``workflow_runs[]`` entry from the GitHub REST API."""

    name: object
    workflow_id: object
    event: object
    status: object
    conclusion: object
    created_at: object
    run_started_at: object


#: GitHub pages this endpoint at 30 by default; ``--paginate`` follows every page regardless,
#: so a larger page is purely fewer round trips.
PAGE_SIZE = 100

#: A read on the refusal path must not hang the recorder waiting on it.
READ_TIMEOUT_SECONDS = 30.0

#: Conclusions that mean the tree was RED. ``cancelled`` and ``stale`` are absent on purpose:
#: they are superseded runs, not verdicts, and reading one as red resurrects a false refusal.
RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

#: Conclusions that raise no objection. ``skipped``/``neutral`` are a path-filtered workflow
#: declining to run, which is not a failure.
GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

_COMPLETED = "completed"


def workflow_runs_argv(*, slug: str, head_sha: str, page_size: int = PAGE_SIZE) -> list[str]:
    """The ``gh api`` args for every workflow run at *head_sha*, across ALL pages.

    Excludes the ``gh`` binary so a caller with its own resolved path can prepend it.
    ``--slurp`` is required rather than preferred: bare ``--paginate`` emits concatenated
    JSON documents ``json.loads`` rejects.
    """
    query = f"head_sha={head_sha}&per_page={page_size}&exclude_pull_requests=true"
    return ["api", "--paginate", "--slurp", f"repos/{slug}/actions/runs?{query}"]


def parse_workflow_run_pages(out: str) -> list[WorkflowRun] | None:
    """Flatten a ``--paginate --slurp`` workflow-runs response, or ``None`` on no evidence.

    A payload carrying literally zero runs is indeterminate — nothing has reported on that
    commit yet — and ``None`` keeps that apart from a list a caller could read as "nothing
    is failing, therefore green".
    """
    try:
        pages = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list):
        return None
    runs = [
        cast("WorkflowRun", run)
        for page in pages
        if isinstance(page, dict)
        for run in page.get("workflow_runs", [])
        if isinstance(run, dict)
    ]
    return runs or None


def _started_at(run: WorkflowRun) -> str:
    return str(run.get("run_started_at") or run.get("created_at") or "")


def _run_name(run: WorkflowRun) -> str:
    return str(run.get("name") or "")


def _conclusion(run: WorkflowRun) -> str:
    return str(run.get("conclusion") or "").lower()


def newest_run_per_workflow(runs: list[WorkflowRun]) -> list[WorkflowRun]:
    """One run per ``(workflow_id, event)`` — the newest, so a re-run supersedes its failure."""
    newest: dict[tuple[str, str], WorkflowRun] = {}
    for run in runs:
        key = (str(run.get("workflow_id") or _run_name(run)), str(run.get("event") or ""))
        current = newest.get(key)
        if current is None or _started_at(run) >= _started_at(current):
            newest[key] = run
    return list(newest.values())


def classify_workflow_runs(runs: list[WorkflowRun]) -> LiveChecksRead:
    """The head's CI verdict: red > pending > unreadable > green, evidence attached."""
    if not runs:
        return LiveChecksRead.unreadable("the forge reported no workflow run at all for this head")
    live = newest_run_per_workflow(runs)
    red = [run for run in live if _conclusion(run) in RED_CONCLUSIONS]
    if red:
        return LiveChecksRead(
            status=MergeClear.VerifyResult.FAILED.value,
            detail=f"failing workflow run(s): {', '.join(sorted(_run_name(run) for run in red))}",
        )
    unfinished = [run for run in live if str(run.get("status") or "").lower() != _COMPLETED]
    if unfinished:
        return LiveChecksRead(
            status=MergeClear.VerifyResult.PENDING.value,
            detail=f"still running: {', '.join(sorted(_run_name(run) for run in unfinished))}",
        )
    unknown = [run for run in live if _conclusion(run) not in GREEN_CONCLUSIONS]
    if unknown:
        concluded = sorted({_conclusion(run) for run in unknown})
        return LiveChecksRead.unreadable(f"workflow run(s) concluded {', '.join(concluded)} — neither red nor green")
    return LiveChecksRead(
        status=MergeClear.VerifyResult.GREEN.value,
        detail=f"{len(live)} workflow run(s) concluded green",
    )


def live_checks_at(*, slug: str, head_sha: str) -> LiveChecksRead:
    """Read CI at *head_sha* on *slug*; every failure to read is UNREADABLE, never a verdict.

    Satisfies :class:`~teatree.core.modelkit.forge_readability.LiveChecksProbe`. A repo on a
    forge with no workflow-run surface reads unreadable through the same path a down forge
    does, which is the honest answer: this probe can corroborate a red, never refute one.
    """
    gh = shutil.which("gh")
    if gh is None:
        return LiveChecksRead.unreadable("no `gh` on PATH, so CI could not be read")
    try:
        result = run_allowed_to_fail(
            [gh, *workflow_runs_argv(slug=slug, head_sha=head_sha)],
            expected_codes=None,
            env=forge_cli_env(),
            timeout=READ_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, TimeoutExpired) as exc:
        return LiveChecksRead.unreadable(f"the workflow-run read did not complete ({type(exc).__name__})")
    if result.returncode != 0:
        logger.warning("head_workflow_runs: could not read %s@%s (rc=%d)", slug, head_sha[:8], result.returncode)
        return LiveChecksRead.unreadable(f"the workflow-run read failed (rc={result.returncode})")
    runs = parse_workflow_run_pages(result.stdout)
    if runs is None:
        return LiveChecksRead.unreadable("the workflow-run response carried no readable run")
    return classify_workflow_runs(runs)


__all__ = [
    "GREEN_CONCLUSIONS",
    "PAGE_SIZE",
    "READ_TIMEOUT_SECONDS",
    "RED_CONCLUSIONS",
    "WorkflowRun",
    "classify_workflow_runs",
    "live_checks_at",
    "newest_run_per_workflow",
    "parse_workflow_run_pages",
    "workflow_runs_argv",
]
