"""GitHub PR creation — the ``gh pr create`` call and its already-open-PR fallback.

Split out of ``client.py`` so the host stays focused on the cross-host Protocol surface
— the same shape as the sibling ``api`` / ``claims`` / ``payloads`` / ``pr_reads`` modules.
"""

import logging

from teatree.backends.github.api import _FORGE_READ_TIMEOUT_SECONDS, _run_gh
from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.forge_pr_probe import find_open_pr_for_branch
from teatree.core.review.mr_metadata import lacks_rationale
from teatree.core.verify_by_reread import verify_by_reread
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.pr_body import pr_body_tempfile
from teatree.utils.run import CommandFailedError, TimeoutExpired

logger = logging.getLogger(__name__)

# Every way a ``gh`` call can fail without the body question being answered: a non-zero
# exit, a missing binary / broken pipe, or a hung request the caller's timeout cut off.
_FORGE_CALL_FAILURES = (CommandFailedError, OSError, TimeoutExpired)


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
            return _adopt_existing_pr_or_reraise(exc, spec, token=token)
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


def _adopt_existing_pr_or_reraise(exc: CommandFailedError, spec: PullRequestSpec, *, token: str) -> RawAPIDict:
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
    _adopt_body(probe.url, spec, token=token)
    return {"web_url": probe.url}


def _adopt_body(url: str, spec: PullRequestSpec, *, token: str) -> None:
    """Write this ship's description onto an adopted PR still carrying the hook's placeholder (#3991).

    Taking the URL alone left the change to merge with whatever body the no-orphan hook
    wrote from the commit — a ``## Why`` that is a TODO, and a reviewer who cannot tell
    "no rationale" from "rationale written somewhere the PR did not inherit".

    Conditional on purpose: adoption also covers a redelivered ship re-finding its own
    full-bodied PR and a human-opened one on the same branch, so only a body
    :func:`lacks_rationale` recognises as the hook's own is replaced. An UNREADABLE body
    is left alone — a blind overwrite would clobber a rationale we simply could not see.
    """
    current = _read_pr_body(url, token=token)
    if current is None:
        logger.warning("adopted PR %s: body unreadable — leaving whatever is there", url)
        return
    if not lacks_rationale(current):
        return
    _write_pr_body(url, spec.description, token=token)
    outcome = verify_by_reread(
        label=f"pr-body {url}",
        reread=lambda: _body_changed(url, current, token=token),
    )
    if not outcome.confirmed:
        raise CommandFailedError(
            ["gh", "pr", "edit", url, "--body-file"],
            1,
            "",
            f"adopted PR {url} still carries the auto-created placeholder body — the ship's "
            f"description never landed ({outcome.reason}). Write it before shipping.",
        )


def _body_changed(url: str, previous: str, *, token: str) -> bool:
    """Whether a re-read shows the body is no longer *previous* — i.e. the write landed.

    Deliberately NOT ``not lacks_rationale(new_body)``: a ship description built from a
    commit with no ``## Why`` gets a bare header appended by ``ensure_standard_body``, so
    that predicate would fail a ship whose write landed perfectly. What is owed here is
    that the hook's body is gone, not that the replacement is a good one — a thin ship
    body is the description generator's problem, and the same on a non-adopted PR.
    """
    now = _read_pr_body(url, token=token)
    return now is not None and now.strip() != previous.strip()


def _read_pr_body(url: str, *, token: str) -> str | None:
    """The PR's current body, or ``None`` when the forge could not be asked."""
    try:
        result = _run_gh(
            "gh", "pr", "view", url, "--json", "body", "--jq", ".body", token=token, timeout=_FORGE_READ_TIMEOUT_SECONDS
        )
    except _FORGE_CALL_FAILURES:
        return None
    return result.stdout


def _write_pr_body(url: str, description: str, *, token: str) -> None:
    """Replace the PR's body; a failure here is surfaced by the caller's re-read check."""
    with pr_body_tempfile(description) as body_path:
        try:
            _run_gh(
                "gh", "pr", "edit", url, "--body-file", str(body_path), token=token, timeout=_FORGE_READ_TIMEOUT_SECONDS
            )
        except _FORGE_CALL_FAILURES as exc:
            logger.warning("adopted PR %s: body write failed: %s", url, exc)
