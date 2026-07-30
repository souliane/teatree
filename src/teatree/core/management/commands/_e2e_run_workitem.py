"""The ``e2e run <work-item>`` keystone: resolve → ladder → run → record (#794).

Split out of :mod:`.e2e` (the runner-owns-its-concerns split of #3331) so the
work-item orchestration — resolve the Ticket, apply the environment ladder,
dispatch the runner, and record ``{sha, result, artifacts_dir}`` to the durable
recipe — lives in one module the thin ``run`` command delegates to.
"""

import os
from collections.abc import Callable
from dataclasses import replace

from teatree.core.intake.e2e_workitem import record_run, resolve_environment, resolve_run_provenance
from teatree.core.management.commands._e2e_runners import e2e_artifacts_root
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git


def run_work_item(
    *,
    work_item: str,
    at: str,
    test_path: str,
    dispatch: Callable[[], str],
    write_err: Callable[[str], None],
) -> str:
    """Resolve the work item, run its e2e via *dispatch*, and record run provenance.

    Deterministic outcome: either the e2e result, or a precise readiness failure
    naming the exact provisioning gap (which repo at which ref). *dispatch* runs
    the resolved runner (the caller binds the runner + flags); the recorded
    ``last_run`` carries the SHA-set, the run provenance (#272), and the run's
    out-of-repo artifacts root (#3331) so a later ``--from-seams`` assembly needs
    no re-derivation.
    """
    try:
        ticket = Ticket.objects.resolve(work_item)
    except Ticket.DoesNotExist:
        write_err(
            f"No work item matching {work_item!r} (looked up by pk and issue_url). "
            "Provision it first: t3 <overlay> workspace ticket <issue_url>",
        )
        raise SystemExit(2) from None

    resolution = resolve_environment(ticket, at=at)
    if resolution.rung != "existing":
        refs = ", ".join(f"{repo}@{ref}" for repo, ref in sorted(resolution.provision_at.items()))
        write_err(
            f"E2E readiness failed for {ticket}: workspace not present on disk.\n"
            f"Ladder rung '{resolution.rung}' requires provisioning: {refs or '(no repos in recipe)'}.\n"
            "Provision the work item first: t3 <overlay> workspace ticket <issue_url>",
        )
        raise SystemExit(1)

    per_repo_shas: dict[str, str] = {}
    for repo, wt_path in resolution.repo_dirs.items():
        try:
            per_repo_shas[repo] = git.head_sha(repo=wt_path)
        except Exception:  # noqa: BLE001 — an unresolvable head SHA degrades to empty, never aborts discovery
            per_repo_shas[repo] = ""

    first_repo_dir = next(iter(resolution.repo_dirs.values()))
    os.environ["T3_ORIG_CWD"] = first_repo_dir
    artifacts_dir = str(e2e_artifacts_root(first_repo_dir))
    provenance = replace(resolve_run_provenance(get_overlay(), test_path), artifacts_dir=artifacts_dir)

    try:
        result = dispatch()
    except SystemExit as exc:
        record_run(ticket, result="red", per_repo_shas=per_repo_shas, provenance=provenance)
        raise SystemExit(exc.code) from exc
    record_run(ticket, result="green", per_repo_shas=per_repo_shas, provenance=provenance)
    return result
