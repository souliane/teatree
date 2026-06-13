"""Host-facing orchestration for ``e2e post-test-plan`` (teatree #272, #2165).

The ORM + code-host side of the one-note-per-ticket test-plan model. The pure
string/JSON layer — the manifest parse, the persisted :class:`TestPlanState`,
the merge, and the side-by-side render — lives in :mod:`._test_plan_render`;
this module resolves the ticket, uploads the artifacts (embedding the relative
``/uploads/<secret>/<file>`` reference GitLab claims on save; #2165), merges
this run's side(s) over the prior state, and creates-or-updates the single note.

The note is posted on the **ticket** (work item / bug), never on an MR — the
deployed-environment proof belongs to the issue the work closes and stays
attached after the MR merges.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.management.commands._test_plan_render import (
    SideManifest,
    TestPlanManifest,
    TestPlanState,
    TestPlanValidationError,
    WorkflowArtifacts,
    WorkflowEmbed,
    empty_state,
    find_ticket_marker,
    merge_state,
    parse_manifest,
    parse_state_blob,
    render_body,
    render_mrs_line,
    test_plan_marker,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.resolve import WorktreeNotFoundError, resolve_worktree
from teatree.core.test_plan_validation import TestPlanImageValidationError, validate_test_plan_images
from teatree.types import RawAPIDict

# Re-exports so callers/tests import the test-plan surface from one module.
__all__ = [
    "PostTestPlanResult",
    "SideManifest",
    "TestPlanFlags",
    "TestPlanManifest",
    "TestPlanMediaError",
    "TestPlanPost",
    "TestPlanResolutionError",
    "TestPlanState",
    "TestPlanValidationError",
    "WorkflowArtifacts",
    "WorktreeNotFoundError",
    "build_validated_post",
    "find_existing_note",
    "merge_state",
    "parse_manifest",
    "parse_state_blob",
    "post_test_plan_comment",
    "render_body",
    "render_mrs_line",
    "run_post_test_plan",
    "run_retract_evidence",
    "test_plan_marker",
]

_ON_BEHALF_ACTION = "post_e2e_evidence"

_log = logging.getLogger(__name__)


class TestPlanResolutionError(TestPlanValidationError):
    """The ticket the evidence should post on could not be resolved.

    A subclass of :class:`TestPlanValidationError` so the command's single
    ``except TestPlanValidationError`` arm catches resolution and validation
    failures alike — both must exit non-zero with no host side effect.
    """


class TestPlanMediaError(TestPlanValidationError):
    """An uploaded artifact would not render in the posted note.

    Raised by the post-upload existence gate when an embedded media URL does
    not resolve (non-200) or the fetched bytes are not the expected medium —
    so "posted" can never mean "returned 201 but referenced a missing upload".
    A subclass of :class:`TestPlanValidationError` so the command's existing
    arm surfaces it as a non-zero exit; the gate runs before the post, so a
    failure burns no on-behalf approval and writes no note.
    """


@dataclass(frozen=True, slots=True)
class ExistingNote:
    """THIS ticket's prior test-plan note: its comment id and recovered state."""

    comment_id: int
    state: TestPlanState


def find_existing_note(comments: list[RawAPIDict], *, ticket_id: str) -> ExistingNote | None:
    """Return THIS ticket's existing test-plan note (matched on the ticket marker), or ``None``.

    There is one note per ticket, so the first comment whose marker carries
    this ticket id wins. The recovered hidden-JSON state backs the merge.
    """
    for comment in comments:
        body = str(comment.get("body", ""))
        if not find_ticket_marker(body, ticket_id=ticket_id):
            continue
        comment_id = _comment_id(comment)
        if comment_id:
            return ExistingNote(comment_id=comment_id, state=parse_state_blob(body))
    return None


class PostTestPlanResult(TypedDict):
    """Return shape of ``e2e post-test-plan`` — the posted test-plan note.

    ``action`` is ``"created"`` when a new note was posted and ``"updated"``
    when the ticket's existing note was edited in place. ``envs`` lists the
    environment column(s) this run wrote.
    """

    issue_url: str
    comment_id: int
    envs: list[str]
    action: str


@dataclass(frozen=True, slots=True)
class TestPlanPost:
    """Validated inputs for :func:`post_test_plan_comment`.

    No ``repo`` field: the artifact-upload project is NOT a free input — it is
    resolved at post time from ``issue_url`` (the note's own project) so every
    upload lands in the same project's ``/uploads`` namespace the note is
    created on. A note renders only the uploads its OWN project claims, so the
    upload target must follow the note, never the manifest's MRs / CI project.
    """

    issue_url: str
    ticket_id: str
    title: str
    manifest: TestPlanManifest


@dataclass(frozen=True, slots=True)
class TestPlanFlags:
    """The raw CLI flags for ``e2e post-test-plan``, before validation.

    ``manifest_dir`` is the directory the manifest file was read from (empty when
    the manifest was an inline string): relative artifact paths resolve against
    it. ``skip_validation`` is the user-authorised bypass of the image preflight
    (red-box / duplicate gates) — the agent never sets it on its own.
    """

    ticket: str = ""
    manifest: str = ""
    title: str = ""
    mrs: tuple[str, ...] = field(default_factory=tuple)
    manifest_dir: str = ""
    skip_validation: bool = False


def _resolve_worktree_or_none() -> Worktree | None:
    """Resolve the current worktree, or ``None`` when not inside one."""
    try:
        return resolve_worktree()
    except WorktreeNotFoundError:
        return None


def _resolve_ticket(ticket: str, worktree: Worktree | None, *, manifest_ticket: str = "") -> Ticket:
    """Resolve the Ticket the evidence posts on, from ``--ticket``, the worktree, or the manifest.

    Precedence: ``--ticket`` (a pk, issue number, or full issue URL) wins; then
    the resolved worktree's ticket; then the manifest's own top-level ``ticket``
    field (so a manifest that names its ticket needs no ``--ticket`` flag). Raises
    :class:`TestPlanResolutionError` when none resolves to a ticket carrying an
    ``issue_url``.
    """
    ref = ticket or (manifest_ticket if worktree is None or worktree.ticket is None else "")
    if ref:
        try:
            resolved = Ticket.objects.resolve(ref)
        except Ticket.DoesNotExist:
            msg = f"No ticket matching {ref!r} (looked up by pk and issue_url)."
            raise TestPlanResolutionError(msg) from None
    elif worktree is not None and worktree.ticket is not None:
        resolved = worktree.ticket
    else:
        msg = (
            "Could not determine the ticket: pass --ticket <pk|number|url>, "
            "set a top-level 'ticket' in the manifest, or run from inside a worktree."
        )
        raise TestPlanResolutionError(msg)
    if not resolved.issue_url:
        msg = f"Ticket {resolved} has no issue_url to post test plan on."
        raise TestPlanResolutionError(msg)
    return resolved


def _manifest_image_paths(manifest: TestPlanManifest) -> list[Path]:
    """Every screenshot path the manifest references, across both sides + all workflows."""
    return [
        Path(image) for side in (manifest.dev, manifest.local) for wf in side.workflows.values() for image in wf.images
    ]


def _preflight_images(manifest: TestPlanManifest, *, skip: bool) -> None:
    """Run the deterministic image preflight; re-raise a hard failure for the single catch arm.

    Refuses (fail-loud) on a missing red box or a byte-identical duplicate by
    re-raising the :class:`TestPlanImageValidationError` as an
    :class:`TestPlanValidationError` so the command's existing single
    ``except TestPlanValidationError`` arm exits non-zero before any upload.
    Staleness warnings never refuse — they are logged loudly and the post
    proceeds. ``skip`` is the user-authorised bypass (runs nothing dangerous).
    """
    try:
        warnings = validate_test_plan_images(_manifest_image_paths(manifest), skip=skip)
    except TestPlanImageValidationError as exc:
        raise TestPlanValidationError(str(exc)) from exc
    for warning in warnings:
        _log.warning(warning)


def build_validated_post(flags: TestPlanFlags) -> TestPlanPost:
    """Run every validator in order and return a fully-validated :class:`TestPlanPost`.

    Order: manifest parse + per-file existence/media-kind → image preflight
    (red-box / duplicate / staleness) → ticket resolvable. Any hard failure
    raises :class:`TestPlanValidationError` (or its
    :class:`TestPlanResolutionError` subclass) so the caller exits non-zero
    before any host side effect. Relative artifact paths resolve against
    ``flags.manifest_dir``; ``--ticket`` falls back to the manifest's ``ticket``
    field. The marker id is the resolved ticket number; the title falls back to
    the issue URL. ``--mrs`` supplements the manifest's MRs. The artifact-upload
    project is NOT decided here — it is resolved from ``issue_url`` at post time
    (see :class:`TestPlanPost`).
    """
    worktree = _resolve_worktree_or_none()
    base_dir = Path(flags.manifest_dir) if flags.manifest_dir else None
    manifest = parse_manifest(flags.manifest, base_dir=base_dir)
    _preflight_images(manifest, skip=flags.skip_validation)
    ticket = _resolve_ticket(flags.ticket, worktree, manifest_ticket=manifest.ticket)
    issue_url = str(ticket.issue_url)

    mrs = manifest.mrs or _normalize_mrs(list(flags.mrs))
    merged = TestPlanManifest(
        ticket=manifest.ticket,
        mrs=tuple(mrs),
        dev=manifest.dev,
        local=manifest.local,
        steps=manifest.steps,
    )
    return TestPlanPost(
        issue_url=issue_url,
        ticket_id=ticket.ticket_number,
        title=flags.title.strip() or issue_url,
        manifest=merged,
    )


def _read_manifest(manifest: str, *, write_err: Callable[[str], None]) -> tuple[str, str]:
    """Return ``(manifest JSON text, base_dir)`` — a path read with its parent as base dir.

    A non-path value is an inline JSON string with an empty base dir; an empty
    ``--manifest`` writes an error and exits non-zero.
    """
    if not manifest.strip():
        write_err("--manifest is required (a path to, or inline string of, the test-plan manifest JSON).")
        raise SystemExit(1)
    path = Path(manifest)
    if path.is_file():
        return path.read_text(encoding="utf-8"), str(path.resolve().parent)
    return manifest, ""


def run_post_test_plan(  # noqa: PLR0913 — the CLI flags map 1:1 to a single shared entry point.
    *,
    manifest: str,
    ticket: str,
    title: str,
    mrs: list[str],
    skip_validation: bool,
    write_out: Callable[[str], None],
    write_err: Callable[[str], None],
) -> PostTestPlanResult:
    """Read the manifest, resolve the host, validate, and post-or-update the note.

    The full ``e2e post-test-plan`` orchestration, factored out of the CLI command
    so the thin command method and its deprecated ``post-evidence`` alias share one
    body. Reads the manifest, resolves the overlay code host, builds and validates
    the post, then creates-or-updates the single note, writing one success line via
    ``write_out``. A pre-post :class:`TestPlanValidationError` /
    :class:`OnBehalfPostBlockedError` is written to ``write_err`` and re-raised as
    ``SystemExit(1)``; a missing code host exits the same way.
    """
    manifest_json, manifest_dir = _read_manifest(manifest, write_err=write_err)
    flags = TestPlanFlags(
        ticket=ticket,
        manifest=manifest_json,
        title=title,
        mrs=tuple(mrs or ()),
        manifest_dir=manifest_dir,
        skip_validation=skip_validation,
    )
    host = code_host_from_overlay()
    if host is None:
        write_err("No code host configured (check overlay GitLab/GitHub token).")
        raise SystemExit(1)
    try:
        post = build_validated_post(flags)
        result = post_test_plan_comment(host, post)
    except (TestPlanValidationError, OnBehalfPostBlockedError) as err:
        write_err(str(err))
        raise SystemExit(1) from err
    write_out(
        f"  Test plan {result['action']} ({', '.join(result['envs'])}) "
        f"on {post.issue_url} (comment {result['comment_id']}).",
    )
    return result


def _normalize_mrs(raw_mrs: list[str]) -> list[str]:
    """Flatten the repeatable/comma-separated ``--mrs`` fallback inputs to clean refs."""
    refs: list[str] = []
    for entry in raw_mrs:
        for part in entry.split(","):
            ref = part.strip()
            if ref:
                refs.append(ref)
    return refs


def _comment_id(result: RawAPIDict) -> int:
    """Extract the integer comment id from a host create response."""
    raw = result.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _verified_embed(host: CodeHostBackend, *, repo: str, filepath: str, label: str) -> str:
    """Upload one artifact, existence-check it, and return its relative-ref embed.

    The existence gate (#2156): after uploading, the host fetches the artifact
    back through its token-authenticated route and magic-byte-checks the
    content. A non-200 fetch, wrong media bytes, or an unparsable upload
    response raises :class:`TestPlanMediaError` naming the broken artifact — so
    the note is never posted referencing a missing upload. The returned
    markdown embeds the **relative** ``/uploads/<secret>/<file>`` reference
    (#2165): GitLab's reference scanner recognises that relative form in the
    saved note markdown and *claims* the upload, so it renders. The absolute
    ``/-/project/<id>/uploads/...`` / ``https://`` form is NOT claimed and 404s
    in a browser. GitLab renders the same ``![label](url)`` syntax as an
    ``<img>`` for an image extension and a ``<video controls>`` for a video
    extension, so one embed form serves both; no re-encoding is involved (the
    recorded VP8/WebM plays natively in a Chromium browser).
    """
    upload = host.upload_file(repo=repo, filepath=filepath)
    verification = host.verify_upload(repo=repo, upload=upload)
    if not verification.ok:
        msg = f"Test plan {label} ({Path(filepath).name}) failed the upload check: {verification.detail}"
        raise TestPlanMediaError(msg)
    return f"![{label}]({verification.embed_url})"


def _embed_side(host: CodeHostBackend, *, repo: str, side: SideManifest) -> dict[str, WorkflowEmbed]:
    """Upload + existence-check every workflow's artifacts for one side, returning embeds.

    Returns the per-workflow rendered embeds persisted into the state blob so a
    later single-env run re-renders this side without re-uploading.
    """
    out: dict[str, WorkflowEmbed] = {}
    for name, wf in side.workflows.items():
        video_md = ""
        if wf.video is not None:
            video_md = _verified_embed(host, repo=repo, filepath=str(wf.video), label=f"{name} — video")
        image_md = [
            _verified_embed(host, repo=repo, filepath=str(img), label=f"{name} — {img.name}") for img in wf.images
        ]
        out[name] = {"video_md": video_md, "image_md": image_md}
    return out


def post_test_plan_comment(host: CodeHostBackend, post: TestPlanPost) -> PostTestPlanResult:
    """Gate, upload artifacts, merge over prior state, then create-or-update the note.

    Runs only after every validator passed. The on-behalf gate is the last
    check before any side effect: a BLOCK with no recorded approval raises
    :class:`OnBehalfPostBlockedError`, which the command surfaces as a non-zero
    exit rather than publishing unattended. The non-consuming peek raises
    *before* any artifact upload; the consume then happens atomically with the
    post (#1879), so a failed post burns no approval and writes no lying audit.

    The merge model (teatree #272): the ticket's single note carries a hidden
    state blob that is the source of truth. This run uploads only the side(s) it
    carries, merges them over the recovered prior state (freezing the other
    side), re-renders the full side-by-side body, and writes back both the
    hidden blob and the rendered markdown — update in place, or create when
    absent.

    The artifact-upload project is the note's OWN project, resolved from
    ``issue_url`` via :meth:`CodeHostBackend.repo_for_issue_url` — never the
    manifest's MRs or the overlay's CI project. GitLab serves a note's relative
    ``/uploads/<secret>/<file>`` reference from the note's project namespace, so
    an upload that landed on a different repo (e.g. the manifest's second/CI
    repo) 404s in the rendered note. Uploading to the note's project keeps
    upload-target == note-project by construction, regardless of how many repos
    the manifest references.
    """
    blocked = on_behalf_block_message(post.issue_url, _ON_BEHALF_ACTION)
    if blocked:
        raise OnBehalfPostBlockedError(post.issue_url, _ON_BEHALF_ACTION)

    upload_repo = host.repo_for_issue_url(post.issue_url)
    existing = find_existing_note(host.list_issue_comments(issue_url=post.issue_url), ticket_id=post.ticket_id)
    prior = existing.state if existing else empty_state(ticket=post.ticket_id, title=post.title)

    embeds: dict[str, dict[str, WorkflowEmbed]] = {
        "dev": _embed_side(host, repo=upload_repo, side=post.manifest.dev) if post.manifest.dev.present else {},
        "local": _embed_side(host, repo=upload_repo, side=post.manifest.local) if post.manifest.local.present else {},
    }
    state = merge_state(prior, manifest=post.manifest, title=post.title, embeds=embeds)
    state["ticket"] = post.ticket_id
    body = render_body(state)

    envs = [env for env, side in (("dev", post.manifest.dev), ("local", post.manifest.local)) if side.present]
    match_id = existing.comment_id if existing else None
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
        summary=f"Test plan ({', '.join(envs)}) on {post.issue_url}",
    )
    return PostTestPlanResult(issue_url=post.issue_url, comment_id=comment_id, envs=envs, action=action)


def run_retract_evidence(
    *,
    ticket: str,
    write_out: Callable[[str], None],
    write_err: Callable[[str], None],
) -> None:
    host = code_host_from_overlay()
    if host is None:
        write_err("No code host configured (check overlay GitLab/GitHub token).")
        raise SystemExit(1)
    try:
        worktree = resolve_worktree()
    except WorktreeNotFoundError:
        worktree = None
    try:
        resolved = _resolve_ticket(ticket, worktree)
    except TestPlanResolutionError as err:
        write_err(str(err))
        raise SystemExit(1) from err
    issue_url = str(resolved.issue_url)
    comments = host.list_issue_comments(issue_url=issue_url)
    existing = find_existing_note(comments, ticket_id=resolved.ticket_number)
    if existing is None:
        write_err(f"No test-plan note found on {issue_url}.")
        raise SystemExit(1)
    host.delete_issue_comment(issue_url=issue_url, comment_id=existing.comment_id)
    write_out(f"  Test-plan note {existing.comment_id} retracted from {issue_url}.")
