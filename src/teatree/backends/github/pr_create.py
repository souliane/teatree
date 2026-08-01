"""GitHub PR creation — the ``gh pr create`` call and its already-open-PR fallback.

Split out of ``client.py`` so the host stays focused on the cross-host Protocol surface
— the same shape as the sibling ``api`` / ``claims`` / ``payloads`` / ``pr_reads`` modules.
"""

import logging

from teatree.backends.github.api import _FORGE_READ_TIMEOUT_SECONDS, _run_gh
from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.forge_pr_probe import find_open_pr_for_branch
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.pr_body import pr_body_tempfile
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


def create_pr(spec: PullRequestSpec, *, token: str) -> RawAPIDict:
    """Open a PR for *spec*, adopting an already-open one rather than failing on the race.

    The no-orphan push-gate hook (``ensure-pr``) can open a PR for a branch on its FIRST
    push, before this ship's own create call runs — a race, not a conflict: the branch is
    real and the PR is real, only this create call is redundant.
    """
    repo_slug = git.remote_slug(repo=spec.repo)
    # Unique per-invocation body file the CLI owns — never a shared
    # ``/tmp/pr-body.md`` two concurrent shippers race (#3581).
    with pr_body_tempfile(spec.description) as body_path:
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--head",
            spec.branch,
            "--title",
            spec.title,
            "--body-file",
            str(body_path),
        ]
        if spec.target_branch:
            cmd.extend(["--base", spec.target_branch])
        if spec.labels:
            cmd.extend(["--label", ",".join(spec.labels)])
        if spec.assignee:
            cmd.extend(["--assignee", spec.assignee])
        if spec.draft:
            cmd.append("--draft")

        try:
            result = _run_gh(*cmd, token=token, timeout=_FORGE_READ_TIMEOUT_SECONDS)
        except CommandFailedError as exc:
            return _adopt_existing_pr_or_reraise(exc, spec)
    # #1222 / #1226: align with the cross-host canonical key (``web_url``) that
    # ``ShipExecutor`` reads — returning ``url`` silently produced empty PR rows because
    # the consumer never looked at that field. The producer also enforces the
    # verify-by-re-read invariant: an empty / non-URL stdout (e.g. the ``no commits
    # between`` pre-push race that exits 0) is rejected so ``ok=True`` never escapes with
    # no PR.
    url = result.stdout.strip()
    if not url.startswith(("http://", "https://")):
        raise CommandFailedError(
            cmd,
            result.returncode,
            result.stdout,
            f"gh pr create produced no PR URL (stdout={url!r})",
        )
    return {"web_url": url}


def _adopt_existing_pr_or_reraise(exc: CommandFailedError, spec: PullRequestSpec) -> RawAPIDict:
    """Adopt an already-open PR for *spec.branch* instead of failing the ship.

    Mirrors the idempotency ``execute_ship`` already documents for a REDELIVERED job
    re-finding its own recorded PR; here a DIFFERENT actor created it first, so nothing
    was recorded to re-find, and the discovery goes through the live forge instead.

    Fail-closed: only the exact ``already exists`` message is treated as this case, and
    the discovered PR must be independently CONFIRMED open — an ambiguous probe (auth
    trouble, a stale cache, a branch match on the wrong repo) re-raises the original
    error rather than silently reporting success for a PR that may not be the one this
    ticket wants.
    """
    if "already exists" not in exc.stderr:
        raise exc
    probe = find_open_pr_for_branch(spec.repo, spec.branch)
    if not probe.is_found:
        raise exc
    return {"web_url": probe.url}
