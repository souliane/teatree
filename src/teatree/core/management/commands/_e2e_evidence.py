"""Validators and comment builder for ``e2e post-evidence``.

Split out of ``e2e.py`` (mirroring the ``_e2e_discovery`` split) so the
validation logic — env enum, artifact-presence, anti-fake before≠after,
commit known-and-clean — is independently unit-testable as pure functions.

The command method in ``e2e.py`` orchestrates: it calls these validators
in order (env → artifacts → before≠after → commit → ticket-resolvable),
catches the typed errors they raise, writes the message to stderr and
exits non-zero. None of these functions know about Typer or the CLI.

The evidence comment is posted on the **ticket** (work item / bug), never
on an MR — the deployed-environment proof belongs to the issue the work
closes, and stays attached even after the MR merges. Idempotency is keyed
on a hidden HTML-comment marker carrying ``(env, commit)`` so a re-run on
the same environment + commit edits the existing comment in place instead
of appending a new one.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from teatree.backends.protocols import CodeHostBackend
from teatree.core.models import Ticket, Worktree
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import WorktreeNotFoundError, resolve_worktree
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError

_ON_BEHALF_ACTION = "post_e2e_evidence"

# Mirror of ``backends/gitlab_sync_prs.py`` ``_E2E_EVIDENCE_RE`` in spirit:
# a single source-of-truth regex matching the hidden idempotency marker
# embedded at the top of every evidence comment. Named groups expose the
# env + commit so the idempotency lookup is a pure regex parse.
_E2E_MARKER_RE = re.compile(r"<!--\s*t3-e2e-evidence\s+env=(?P<env>\S+)\s+commit=(?P<commit>\S+)\s*-->")


class EvidenceEnv(StrEnum):
    """The only environments E2E evidence may come from.

    The dev/local gate is machine-enforced here (it used to be a prose-only
    rule in the e2e skill): a deployed dev environment or a teatree-managed
    local stack. Staging/prod evidence is out of scope for this command.
    """

    DEV = "dev"
    LOCAL = "local"


class EvidenceValidationError(ValueError):
    """A pre-post evidence validation failed — the comment must NOT be posted.

    Raised by the pure validators below; the command method catches it,
    writes ``str(error)`` to stderr and raises ``SystemExit(1)`` so no
    upload or comment side effect ever runs on invalid evidence.
    """


class EvidenceResolutionError(EvidenceValidationError):
    """The ticket the evidence should post on could not be resolved.

    A subclass of :class:`EvidenceValidationError` so the command's single
    ``except EvidenceValidationError`` arm catches resolution and validation
    failures alike — both must exit non-zero with no host side effect.
    """


def coerce_env(env: str) -> EvidenceEnv:
    """Coerce a ``--env`` string to :class:`EvidenceEnv` or raise.

    Empty or anything outside ``{dev, local}`` fails — the command requires
    an explicit, machine-checked environment.
    """
    try:
        return EvidenceEnv(env.strip().lower())
    except ValueError:
        allowed = ", ".join(e.value for e in EvidenceEnv)
        msg = f"--env must be one of {{{allowed}}}, got {env!r}."
        raise EvidenceValidationError(msg) from None


def validate_assertion(assertion: str) -> None:
    """The feature-claim text is required — empty evidence proves nothing."""
    if not assertion.strip():
        msg = "--assertion is required (the feature claim the evidence proves)."
        raise EvidenceValidationError(msg)


def validate_artifacts_present(*, before: str, after: str) -> tuple[Path, Path]:
    """Both before/after must be non-empty paths pointing at real files.

    Separate messages per side so the user knows which artifact is missing.
    Returns the resolved ``(before_path, after_path)`` for downstream checks.
    """
    if not before:
        msg = "--before is required (path to the before screenshot/artifact)."
        raise EvidenceValidationError(msg)
    if not after:
        msg = "--after is required (path to the after screenshot/artifact)."
        raise EvidenceValidationError(msg)
    before_path = Path(before)
    after_path = Path(after)
    if not before_path.is_file():
        msg = f"--before is not a file: {before}"
        raise EvidenceValidationError(msg)
    if not after_path.is_file():
        msg = f"--after is not a file: {after}"
        raise EvidenceValidationError(msg)
    return before_path, after_path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_before_differs_from_after(*, before: Path, after: Path) -> None:
    """Anti-fake gate: before and after must not be the same bytes.

    Same path, or two distinct paths whose bytes hash identically, both
    fail — a before/after pair that is byte-identical is not evidence of
    any change. For images this is a byte-level (not perceptual) compare:
    Pillow is intentionally not a dependency, so two visually-identical PNGs
    re-encoded differently would pass here; the byte-hash catches the common
    fake (the same file submitted twice).
    """
    if before.resolve() == after.resolve():
        msg = "--before and --after point at the same file; evidence must show a change."
        raise EvidenceValidationError(msg)
    if _file_sha256(before) == _file_sha256(after):
        msg = "--before and --after are byte-identical; evidence must show a change."
        raise EvidenceValidationError(msg)


def resolve_and_validate_commit(*, commit: str, repo: str) -> str:
    """Resolve the code-under-test SHA and confirm it is known + the tree is clean.

    Empty ``commit`` auto-detects via ``git.head_sha(repo)``; an empty result
    (not a git repo / no HEAD) fails. A supplied SHA must resolve via
    ``git rev-parse --verify <sha>^{commit}``. A dirty working tree
    (``git status --porcelain`` non-empty) fails — uncommitted changes mean
    the evidence is not reproducible from the recorded commit.

    Returns the full resolved SHA.
    """
    resolved = commit.strip()
    if not resolved:
        try:
            resolved = git.head_sha(repo=repo)
        except CommandFailedError:
            resolved = ""
        if not resolved:
            msg = f"Could not resolve a commit SHA (no --commit and git HEAD unavailable in {repo!r})."
            raise EvidenceValidationError(msg)
    else:
        # Expand the supplied SHA (often a short prefix) to the canonical
        # full 40-char form so the stored marker matches the auto-detect
        # path's ``git.head_sha`` — without this, a short-then-default
        # round trip would post a duplicate evidence comment because
        # ``find_matching_comment`` does raw string equality on the SHA.
        try:
            resolved = git.run_strict(repo=repo, args=["rev-parse", "--verify", f"{resolved}^{{commit}}"])
        except CommandFailedError:
            msg = f"--commit {commit!r} is not a known commit in {repo!r}."
            raise EvidenceValidationError(msg) from None

    if git.status_porcelain(repo=repo).strip():
        msg = f"Working tree in {repo!r} is dirty; commit or stash changes so the evidence is reproducible."
        raise EvidenceValidationError(msg)
    return resolved


def evidence_marker(*, env: EvidenceEnv, commit: str) -> str:
    """The hidden HTML-comment idempotency marker for ``(env, commit)``.

    Renders invisibly in GitLab/GitHub markdown; matched by
    :data:`_E2E_MARKER_RE` to find a prior evidence comment to update.
    """
    return f"<!-- t3-e2e-evidence env={env.value} commit={commit} -->"


def find_matching_comment(
    comments: list[RawAPIDict],
    *,
    env: EvidenceEnv,
    commit: str,
) -> int | None:
    """Return the id of an existing evidence comment matching THIS (env, commit).

    A comment whose marker carries a *different* env or commit is left alone
    (the caller posts a new comment). Returns the first matching comment's
    integer id, or ``None`` when none match.
    """
    for comment in comments:
        body = str(comment.get("body", ""))
        match = _E2E_MARKER_RE.search(body)
        if match is None:
            continue
        if match.group("env") == env.value and match.group("commit") == commit:
            raw_id = comment.get("id")
            if isinstance(raw_id, int):
                return raw_id
            if isinstance(raw_id, str) and raw_id.isdigit():
                return int(raw_id)
    return None


@dataclass(frozen=True, slots=True)
class EvidenceComment:
    """The data the evidence comment body is rendered from.

    Bundles the rendering inputs so :func:`build_evidence_body` stays a
    single-argument function (below the project's per-function arg cap) and
    the call site reads as one named record rather than six positionals.
    """

    env: EvidenceEnv
    commit: str
    before_md: str
    after_md: str
    assertion: str
    video_md: str = ""


def build_evidence_body(comment: EvidenceComment) -> str:
    """Render the evidence comment body.

    Order: hidden marker, environment banner, commit tested, the
    Before/After table (plus a Video row when a video embed is given),
    then the feature-claim assertion text.
    """
    lines = [
        evidence_marker(env=comment.env, commit=comment.commit),
        f"## E2E Evidence — environment: **{comment.env.value.upper()}**",
        "",
        f"Commit tested: `{comment.commit}`",
        "",
        "| Before | After |",
        "|---|---|",
        f"| {comment.before_md} | {comment.after_md} |",
    ]
    if comment.video_md:
        lines.append(f"| Video | {comment.video_md} |")
    lines.extend(["", comment.assertion])
    return "\n".join(lines)


class PostEvidenceResult(TypedDict):
    """Return shape of ``e2e post-evidence`` — the posted evidence comment.

    ``action`` is ``"created"`` when a new comment was posted and
    ``"updated"`` when an existing comment matching the same ``(env, commit)``
    marker was edited in place.
    """

    issue_url: str
    comment_id: int
    env: str
    commit: str
    action: str


@dataclass(frozen=True, slots=True)
class EvidencePost:
    """Validated inputs for :func:`post_evidence_comment`.

    Every field is a post-validation value: the resolved env enum, the
    resolved-and-clean commit SHA, the issue URL the evidence lands on, the
    on-disk artifact paths, and the feature-claim text. Bundled so the
    posting function takes one record rather than eight positionals.
    """

    issue_url: str
    repo: str
    env: EvidenceEnv
    commit: str
    before_path: Path
    after_path: Path
    assertion: str
    video: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceFlags:
    """The raw CLI flags for ``e2e post-evidence``, before validation.

    Mirrors the command's keyword-only parameters so the command method
    forwards one record into :func:`build_validated_post` rather than
    threading eight positionals through the validators.
    """

    ticket: str = ""
    env: str = ""
    commit: str = ""
    before: str = ""
    after: str = ""
    video: str = ""
    assertion: str = ""


def _resolve_worktree_or_none() -> Worktree | None:
    """Resolve the current worktree, or ``None`` when not inside one."""
    try:
        return resolve_worktree()
    except WorktreeNotFoundError:
        return None


def _resolve_issue_url(ticket: str, worktree: Worktree | None) -> str:
    """Resolve the issue URL the evidence posts on, from ``--ticket`` or the worktree.

    ``--ticket`` (a pk, issue number, or full issue URL) wins; otherwise the
    resolved worktree's ticket supplies it. Raises
    :class:`EvidenceResolutionError` when neither resolves to a ticket
    carrying an ``issue_url``.
    """
    if ticket:
        try:
            resolved = Ticket.objects.resolve(ticket)
        except Ticket.DoesNotExist:
            msg = f"No ticket matching {ticket!r} (looked up by pk and issue_url)."
            raise EvidenceResolutionError(msg) from None
    elif worktree is not None and worktree.ticket is not None:
        resolved = worktree.ticket
    else:
        msg = "Could not determine the ticket: pass --ticket <pk|number|url> or run from inside a worktree."
        raise EvidenceResolutionError(msg)
    if not resolved.issue_url:
        msg = f"Ticket {resolved} has no issue_url to post evidence on."
        raise EvidenceResolutionError(msg)
    return str(resolved.issue_url)


def build_validated_post(flags: EvidenceFlags) -> EvidencePost:
    """Run every validator in order and return a fully-validated :class:`EvidencePost`.

    Order: env → artifacts present → before≠after → commit known+clean →
    assertion present → ticket resolvable. Any failure raises
    :class:`EvidenceValidationError` (or its
    :class:`EvidenceResolutionError` subclass) so the caller exits non-zero
    before any host side effect. The repo for artifact upload comes from the
    overlay's CI project path; the commit repo comes from the resolved
    worktree's on-disk path (cwd fallback).
    """
    worktree = _resolve_worktree_or_none()
    repo_dir = worktree.worktree_path if worktree is not None and worktree.worktree_path else "."

    env = coerce_env(flags.env)
    before_path, after_path = validate_artifacts_present(before=flags.before, after=flags.after)
    validate_before_differs_from_after(before=before_path, after=after_path)
    commit = resolve_and_validate_commit(commit=flags.commit, repo=repo_dir)
    validate_assertion(flags.assertion)

    return EvidencePost(
        issue_url=_resolve_issue_url(flags.ticket, worktree),
        repo=get_overlay().metadata.get_ci_project_path(),
        env=env,
        commit=commit,
        before_path=before_path,
        after_path=after_path,
        assertion=flags.assertion,
        video=flags.video,
    )


def _comment_id(result: RawAPIDict) -> int:
    """Extract the integer comment id from a host create response."""
    raw = result.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _upload_artifact(host: CodeHostBackend, *, repo: str, filepath: str, label: str) -> str:
    """Upload one artifact and return its markdown embed.

    Falls back to a bare ``[label](filepath)`` link when the host's upload
    returns no markdown (keeps the table well-formed).
    """
    result = host.upload_file(repo=repo, filepath=filepath)
    markdown = str(result.get("markdown", ""))
    return markdown or f"[{label}]({filepath})"


def post_evidence_comment(host: CodeHostBackend, post: EvidencePost) -> PostEvidenceResult:
    """Gate, upload artifacts, build the body, then create-or-update the comment.

    Runs only after every validator passed. The on-behalf gate is the last
    check before any side effect: a BLOCK with no recorded approval raises
    :class:`OnBehalfPostBlockedError`, which the command surfaces as a
    non-zero exit rather than publishing unattended. The non-consuming peek
    raises *before* any artifact upload; the consume then happens atomically
    with the comment post (#1879), so a failed post burns no approval and
    writes no lying audit. Idempotency is keyed on the hidden ``(env, commit)``
    marker: a matching prior comment is edited in place (``action="updated"``);
    otherwise a new comment is created.
    """
    blocked = on_behalf_block_message(post.issue_url, _ON_BEHALF_ACTION)
    if blocked:
        raise OnBehalfPostBlockedError(post.issue_url, _ON_BEHALF_ACTION)

    before_md = _upload_artifact(host, repo=post.repo, filepath=str(post.before_path), label="before")
    after_md = _upload_artifact(host, repo=post.repo, filepath=str(post.after_path), label="after")
    video_md = _upload_artifact(host, repo=post.repo, filepath=post.video, label="video") if post.video else ""

    body = build_evidence_body(
        EvidenceComment(
            env=post.env,
            commit=post.commit,
            before_md=before_md,
            after_md=after_md,
            assertion=post.assertion,
            video_md=video_md,
        ),
    )

    match_id = find_matching_comment(
        host.list_issue_comments(issue_url=post.issue_url),
        env=post.env,
        commit=post.commit,
    )
    if match_id is not None:
        result = require_on_behalf_approval(
            target=post.issue_url,
            action=_ON_BEHALF_ACTION,
            publish=lambda: host.update_issue_comment(issue_url=post.issue_url, comment_id=match_id, body=body),
        )
        action = "updated"
        comment_id = match_id
    else:
        result = require_on_behalf_approval(
            target=post.issue_url,
            action=_ON_BEHALF_ACTION,
            publish=lambda: host.post_issue_comment(issue_url=post.issue_url, body=body),
        )
        action = "created"
        comment_id = _comment_id(result)

    notify_user_on_behalf_post(
        target=post.issue_url,
        action=_ON_BEHALF_ACTION,
        destination=post.issue_url,
        artifact_url=str(result.get("web_url") or result.get("html_url") or post.issue_url),
        summary=f"E2E evidence ({post.env.value}, {post.commit[:8]}) on {post.issue_url}",
    )
    return PostEvidenceResult(
        issue_url=post.issue_url,
        comment_id=comment_id,
        env=post.env.value,
        commit=post.commit,
        action=action,
    )
