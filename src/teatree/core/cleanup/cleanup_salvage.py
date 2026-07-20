"""Salvage primitive — capture an item's unique content to a PR, verify, then delete (#2763).

The reusable entry the judgment SKILL calls once it has decided an emitted item's
unique content is worth keeping and has CLEANED any banned terms. The CLI does
the mechanical capture→verify→delete; the skill owns the judgment and the
cleaning. The load-bearing invariant: the source item is deleted ONLY after the
forge confirms the PR landed — a failed push / open / verify leaves the source
intact, so salvage NEVER destroys the only copy of work on its own failure.

INTERFACE — ``salvage_item(request: SalvageRequest, hooks: SalvageHooks) -> SalvageResult``:

```
SalvageRequest(
    repo,                       # the git clone the source ref lives in
    source_ref,                 # the branch/ref carrying the unique content (e.g. "feat-x")
    salvage_branch,             # the fresh branch to capture onto (e.g. "salvage/feat-x")
    target="origin/main",       # the base the salvage PR opens against
    require_banned_clean=True,  # refuse to push if banned terms remain (final safety gate)
)
SalvageHooks(
    push,           # (repo, branch) -> bool         publish the salvage branch
    open_pr,        # (repo, branch, target) -> str  open the PR, return its url
    verify_landed,  # (repo, branch) -> bool         confirm the PR/branch is on the forge
    delete_source,  # () -> list[str]                delete the source item, return errors
)
```

The four side-effecting steps are injected (via :class:`SalvageHooks`) so the CLI
wires real ``git`` + ``gh`` while tests drive the capture-then-delete ordering
deterministically. The git capture itself (creating ``salvage_branch`` at
``source_ref``, scanning for banned terms) runs for real inside.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from teatree.core.cleanup.cleanup_emit import banned_terms_status
from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

logger = logging.getLogger(__name__)

type PushFn = Callable[[str, str], bool]
type OpenPrFn = Callable[[str, str, str], str]
type VerifyFn = Callable[[str, str], bool]
type DeleteFn = Callable[[], list[str]]


@dataclass(frozen=True, slots=True)
class SalvageHooks:
    """The four injected side-effecting steps of a salvage (the CLI wires real git+gh)."""

    push: PushFn
    open_pr: OpenPrFn
    verify_landed: VerifyFn
    delete_source: DeleteFn


@dataclass(frozen=True, slots=True)
class SalvageRequest:
    """The what-to-salvage inputs: the source ref, the fresh branch, and the policy."""

    repo: str
    source_ref: str
    salvage_branch: str
    target: str = "origin/main"
    require_banned_clean: bool = True


@dataclass(frozen=True, slots=True)
class SalvageResult:
    """The outcome of one :func:`salvage_item` run.

    ``salvaged`` — the unique content was captured onto a pushed branch with a PR.
    ``deleted`` — the source item was deleted (only ever ``True`` when ``salvaged``
    AND the forge verified the landing). ``pr_url`` / ``salvage_branch`` identify
    the captured work; ``errors`` carries any step failure (the source is kept on
    any error).
    """

    salvaged: bool
    deleted: bool
    pr_url: str = ""
    salvage_branch: str = ""
    errors: list[str] = field(default_factory=list)


def _unique_content_texts(repo: str, source_ref: str, target: str) -> list[str] | None:
    """The commit messages + diff of ``source_ref`` against ``target`` for the banned scan.

    Runs through the STRICT runner and returns ``None`` — NOT ``[]`` — when the
    content could not be read (unresolvable ``target``/ref, corrupt repo). With
    the LENIENT runner a failure degraded to ``["", ""]``, which the banned-terms
    scanner reads as "scanned clean" and lets the salvage PUSH the ref to a public
    PR completely unscanned (#F4.6 leak). ``None`` is the honest "could not scan"
    signal the caller MUST fail closed on.
    """
    try:
        return [
            git.run_strict(repo=repo, args=["log", f"{target}..{source_ref}", "--format=%B"]),
            git.run_strict(repo=repo, args=["diff", f"{target}...{source_ref}"]),
        ]
    except CommandFailedError as exc:
        logger.warning("salvage: could not read unique content of %s (%s)", source_ref, exc)
        return None


def _banned_clean_refusal(request: SalvageRequest) -> str | None:
    """The final safety-gate refusal reason, or ``None`` when it is safe to salvage.

    Fails CLOSED on an inconclusive scan (#F4.6 leak). ``contains`` names the
    banned terms; ``unknown`` — the content could not be READ (unreadable ref/repo)
    or resolved to nothing to scan — refuses rather than push an UNSCANNED branch
    to a public PR. ``clean`` (or ``require_banned_clean=False``) returns ``None``.
    """
    if not request.require_banned_clean:
        return None
    texts = _unique_content_texts(request.repo, request.source_ref, request.target)
    status, found = banned_terms_status(texts) if texts is not None else ("unknown", [])
    if status == "contains":
        return f"refused: banned terms present ({', '.join(found)}) — clean before salvage"
    if status == "unknown":
        return (
            "refused: could not read the source content to scan for banned terms — "
            "salvage aborted so unscanned content is never pushed to a public PR"
        )
    return None


def salvage_item(request: SalvageRequest, hooks: SalvageHooks) -> SalvageResult:
    """Capture ``source_ref``'s unique content to a PR, verify it landed, then delete the source.

    Ordered, fail-safe: (1) refuse if banned terms remain OR are unscannable (final
    gate); (2) create ``salvage_branch`` at ``source_ref`` (non-destructive — a new
    ref); (3) push; (4) open the PR; (5) verify the forge has it; (6) ONLY then
    delete the source. Any failure before a verified landing returns early WITHOUT
    deleting — the source work is never lost on a salvage failure.
    """
    repo, branch = request.repo, request.salvage_branch
    refusal = _banned_clean_refusal(request)
    if refusal is not None:
        return SalvageResult(salvaged=False, deleted=False, salvage_branch=branch, errors=[refusal])

    if not git.check(repo=repo, args=["branch", "-f", branch, request.source_ref]):
        return SalvageResult(
            salvaged=False, deleted=False, salvage_branch=branch, errors=["could not create salvage branch"]
        )
    if not hooks.push(repo, branch):
        return SalvageResult(salvaged=False, deleted=False, salvage_branch=branch, errors=["push failed — source kept"])

    pr_url = hooks.open_pr(repo, branch, request.target)
    if not pr_url:
        return SalvageResult(
            salvaged=False, deleted=False, salvage_branch=branch, errors=["PR open failed — source kept"]
        )
    if not hooks.verify_landed(repo, branch):
        return SalvageResult(
            salvaged=True,
            deleted=False,
            pr_url=pr_url,
            salvage_branch=branch,
            errors=["could not verify the PR landed — source kept, delete it manually once confirmed"],
        )

    errors = hooks.delete_source()
    return SalvageResult(
        salvaged=True,
        deleted=not errors,
        pr_url=pr_url,
        salvage_branch=branch,
        errors=errors,
    )


def _gh_push(repo: str, branch: str) -> bool:
    return run_allowed_to_fail(["git", "-C", repo, "push", "-u", "origin", branch], expected_codes=None).returncode == 0


def _gh_open_pr(repo: str, branch: str, target: str) -> str:
    base = target.removeprefix("origin/")
    result = run_allowed_to_fail(
        ["gh", "pr", "create", "--head", branch, "--base", base, "--fill"], cwd=repo, expected_codes=None
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _gh_verify_open(repo: str, branch: str) -> bool:
    result = run_allowed_to_fail(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
        cwd=repo,
        expected_codes=None,
    )
    if result.returncode != 0:
        return False
    try:
        return bool(json.loads(result.stdout or "[]"))
    except json.JSONDecodeError:
        return False


def default_salvage_hooks(*, source_branch: str, delete: DeleteFn) -> SalvageHooks:
    """Wire the real ``git`` + ``gh`` side effects for the ``workspace salvage`` CLI.

    ``push`` is ``git push -u origin``; ``open_pr`` is ``gh pr create --fill``;
    ``verify_landed`` is ``gh pr list --state open`` (the PR is on the forge);
    ``delete_source`` is supplied by the caller (branch delete, or full worktree
    teardown) since only it knows what kind of item the source is. ``gh`` absent /
    erroring fails SAFE (push/verify return false, open_pr returns ""), so the
    source is kept on any forge failure.
    """
    _ = source_branch  # documents the source the caller is salvaging; delete owns the removal
    return SalvageHooks(push=_gh_push, open_pr=_gh_open_pr, verify_landed=_gh_verify_open, delete_source=delete)
