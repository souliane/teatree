"""Host-facing orchestration for ``e2e post-evidence`` (teatree #272, #2165).

The ORM + code-host side of the one-note-per-ticket evidence model. The pure
string/JSON layer — the manifest parse, the persisted :class:`EvidenceState`,
the merge, and the side-by-side render — lives in :mod:`._e2e_evidence_render`;
this module resolves the ticket, uploads the artifacts (embedding the relative
``/uploads/<secret>/<file>`` reference GitLab claims on save; #2165), merges
this run's side(s) over the prior state, and creates-or-updates the single note.

The note is posted on the **ticket** (work item / bug), never on an MR — the
deployed-environment proof belongs to the issue the work closes and stays
attached after the MR merges.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.evidence_validation import EvidenceImageValidationError, validate_evidence_images
from teatree.core.management.commands._e2e_evidence_render import (
    EvidenceManifest,
    EvidenceState,
    EvidenceValidationError,
    SideManifest,
    WorkflowArtifacts,
    WorkflowEmbed,
    empty_state,
    evidence_marker,
    find_ticket_marker,
    merge_state,
    parse_manifest,
    parse_state_blob,
    render_body,
    render_mrs_line,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.resolve import WorktreeNotFoundError, resolve_worktree
from teatree.types import RawAPIDict

# Re-exports so callers/tests import the evidence surface from one module.
__all__ = [
    "EvidenceFlags",
    "EvidenceManifest",
    "EvidenceMediaError",
    "EvidencePost",
    "EvidenceResolutionError",
    "EvidenceState",
    "EvidenceValidationError",
    "PostEvidenceResult",
    "SideManifest",
    "WorkflowArtifacts",
    "build_validated_post",
    "evidence_marker",
    "find_existing_note",
    "merge_state",
    "parse_manifest",
    "parse_state_blob",
    "post_evidence_comment",
    "render_body",
    "render_mrs_line",
]

_ON_BEHALF_ACTION = "post_e2e_evidence"

_log = logging.getLogger(__name__)


class EvidenceResolutionError(EvidenceValidationError):
    """The ticket the evidence should post on could not be resolved.

    A subclass of :class:`EvidenceValidationError` so the command's single
    ``except EvidenceValidationError`` arm catches resolution and validation
    failures alike — both must exit non-zero with no host side effect.
    """


class EvidenceMediaError(EvidenceValidationError):
    """An uploaded artifact would not render in the posted note.

    Raised by the post-upload existence gate when an embedded media URL does
    not resolve (non-200) or the fetched bytes are not the expected medium —
    so "posted" can never mean "returned 201 but referenced a missing upload".
    A subclass of :class:`EvidenceValidationError` so the command's existing
    arm surfaces it as a non-zero exit; the gate runs before the post, so a
    failure burns no on-behalf approval and writes no note.
    """


@dataclass(frozen=True, slots=True)
class ExistingNote:
    """THIS ticket's prior evidence note: its comment id and recovered state."""

    comment_id: int
    state: EvidenceState


def find_existing_note(comments: list[RawAPIDict], *, ticket_id: str) -> ExistingNote | None:
    """Return THIS ticket's existing evidence note (matched on the ticket marker), or ``None``.

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


class PostEvidenceResult(TypedDict):
    """Return shape of ``e2e post-evidence`` — the posted evidence note.

    ``action`` is ``"created"`` when a new note was posted and ``"updated"``
    when the ticket's existing note was edited in place. ``envs`` lists the
    environment column(s) this run wrote.
    """

    issue_url: str
    comment_id: int
    envs: list[str]
    action: str


@dataclass(frozen=True, slots=True)
class EvidencePost:
    """Validated inputs for :func:`post_evidence_comment`.

    No ``repo`` field: the artifact-upload project is NOT a free input — it is
    resolved at post time from ``issue_url`` (the note's own project) so every
    upload lands in the same project's ``/uploads`` namespace the note is
    created on. A note renders only the uploads its OWN project claims, so the
    upload target must follow the note, never the manifest's MRs / CI project.
    """

    issue_url: str
    ticket_id: str
    title: str
    manifest: EvidenceManifest


@dataclass(frozen=True, slots=True)
class EvidenceFlags:
    """The raw CLI flags for ``e2e post-evidence``, before validation.

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
    :class:`EvidenceResolutionError` when none resolves to a ticket carrying an
    ``issue_url``.
    """
    ref = ticket or (manifest_ticket if worktree is None or worktree.ticket is None else "")
    if ref:
        try:
            resolved = Ticket.objects.resolve(ref)
        except Ticket.DoesNotExist:
            msg = f"No ticket matching {ref!r} (looked up by pk and issue_url)."
            raise EvidenceResolutionError(msg) from None
    elif worktree is not None and worktree.ticket is not None:
        resolved = worktree.ticket
    else:
        msg = (
            "Could not determine the ticket: pass --ticket <pk|number|url>, "
            "set a top-level 'ticket' in the manifest, or run from inside a worktree."
        )
        raise EvidenceResolutionError(msg)
    if not resolved.issue_url:
        msg = f"Ticket {resolved} has no issue_url to post evidence on."
        raise EvidenceResolutionError(msg)
    return resolved


def _manifest_image_paths(manifest: EvidenceManifest) -> list[Path]:
    """Every screenshot path the manifest references, across both sides + all workflows."""
    return [
        Path(image) for side in (manifest.dev, manifest.local) for wf in side.workflows.values() for image in wf.images
    ]


def _preflight_images(manifest: EvidenceManifest, *, skip: bool) -> None:
    """Run the deterministic image preflight; re-raise a hard failure for the single catch arm.

    Refuses (fail-loud) on a missing red box or a byte-identical duplicate by
    re-raising the :class:`EvidenceImageValidationError` as an
    :class:`EvidenceValidationError` so the command's existing single
    ``except EvidenceValidationError`` arm exits non-zero before any upload.
    Staleness warnings never refuse — they are logged loudly and the post
    proceeds. ``skip`` is the user-authorised bypass (runs nothing dangerous).
    """
    try:
        warnings = validate_evidence_images(_manifest_image_paths(manifest), skip=skip)
    except EvidenceImageValidationError as exc:
        raise EvidenceValidationError(str(exc)) from exc
    for warning in warnings:
        _log.warning(warning)


def build_validated_post(flags: EvidenceFlags) -> EvidencePost:
    """Run every validator in order and return a fully-validated :class:`EvidencePost`.

    Order: manifest parse + per-file existence/media-kind → image preflight
    (red-box / duplicate / staleness) → ticket resolvable. Any hard failure
    raises :class:`EvidenceValidationError` (or its
    :class:`EvidenceResolutionError` subclass) so the caller exits non-zero
    before any host side effect. Relative artifact paths resolve against
    ``flags.manifest_dir``; ``--ticket`` falls back to the manifest's ``ticket``
    field. The marker id is the resolved ticket number; the title falls back to
    the issue URL. ``--mrs`` supplements the manifest's MRs. The artifact-upload
    project is NOT decided here — it is resolved from ``issue_url`` at post time
    (see :class:`EvidencePost`).
    """
    worktree = _resolve_worktree_or_none()
    base_dir = Path(flags.manifest_dir) if flags.manifest_dir else None
    manifest = parse_manifest(flags.manifest, base_dir=base_dir)
    _preflight_images(manifest, skip=flags.skip_validation)
    ticket = _resolve_ticket(flags.ticket, worktree, manifest_ticket=manifest.ticket)
    issue_url = str(ticket.issue_url)

    mrs = manifest.mrs or _normalize_mrs(list(flags.mrs))
    merged = EvidenceManifest(
        ticket=manifest.ticket,
        mrs=tuple(mrs),
        dev=manifest.dev,
        local=manifest.local,
        steps=manifest.steps,
    )
    return EvidencePost(
        issue_url=issue_url,
        ticket_id=ticket.ticket_number,
        title=flags.title.strip() or issue_url,
        manifest=merged,
    )


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
    response raises :class:`EvidenceMediaError` naming the broken artifact — so
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
        msg = f"E2E evidence {label} ({Path(filepath).name}) failed the upload check: {verification.detail}"
        raise EvidenceMediaError(msg)
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


def post_evidence_comment(host: CodeHostBackend, post: EvidencePost) -> PostEvidenceResult:
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
        summary=f"E2E evidence ({', '.join(envs)}) on {post.issue_url}",
    )
    return PostEvidenceResult(issue_url=post.issue_url, comment_id=comment_id, envs=envs, action=action)
