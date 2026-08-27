"""Orchestration for ``e2e write-test-plan`` — the ticket's plan file.

Resolves the ticket, validates the run's captures, merges this run's side(s)
over what the plan file already records, and rewrites that one file. The pure
string/JSON layer — the manifest parse, the persisted :class:`PlanState`, the
merge, and the side-by-side render — lives in :mod:`.render`; where the file
lives and how it is read/written lives in :mod:`.file_store`.

The plan is a file in the e2e repo, never a forge comment: it is reviewed with
the specs it describes and ships in the same merge request.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

from teatree.core.evidence import test_plan_validation as _tpv
from teatree.core.evidence import video_evidence as _vev
from teatree.core.evidence.test_plan_blocked_gate import BlockedTestPlanPostError, check_blocked_body_from_config
from teatree.core.intake.resolve import WorktreeNotFoundError, resolve_worktree
from teatree.core.management.commands._e2e_runners import ARTIFACTS_ENV
from teatree.core.management.commands._test_plan.committed_captures import (
    embed_side_captures,
    evidence_dir_for,
    refuse_invalid_committed_captures,
)
from teatree.core.management.commands._test_plan.file_store import plan_path_for_ticket, read_plan_state, write_plan
from teatree.core.management.commands._test_plan.render import (
    SideManifest,
    TestPlanManifest,
    TestPlanValidationError,
    WorkflowEmbed,
    merge_state,
    parse_manifest,
    render_body,
)
from teatree.core.models import Ticket, Worktree

__all__ = [
    "PlanWriteResult",
    "TestPlanFlags",
    "TestPlanResolutionError",
    "TestPlanValidationError",
    "TestPlanWrite",
    "artifact_ref",
    "build_validated_write",
    "resolve_ticket",
    "run_write_test_plan",
    "summary_line",
    "write_test_plan",
]

_log = logging.getLogger(__name__)


class TestPlanResolutionError(TestPlanValidationError):
    """The ticket whose plan file should be written could not be resolved.

    A subclass of :class:`TestPlanValidationError` so the command's single
    ``except TestPlanValidationError`` arm catches resolution and validation
    failures alike — both must exit non-zero with nothing written.
    """

    __test__ = False  # not a pytest test class (name starts with 'Test')


class PlanWriteResult(TypedDict):
    """Return shape of ``e2e write-test-plan`` — the plan file this run wrote.

    Named without the ``Test`` prefix on purpose: a ``TypedDict`` body accepts no
    ``__test__`` opt-out, so a ``Test*`` name would be collected by pytest.

    ``action`` is ``"created"`` when the ticket had no plan file yet and
    ``"updated"`` when the existing one was rewritten in place. ``envs`` lists
    the environment column(s) this run contributed.
    """

    path: str
    envs: list[str]
    action: str


@dataclass(frozen=True, slots=True)
class TestPlanWrite:
    """Validated inputs for :func:`write_test_plan`."""

    __test__ = False  # not a pytest test class (name starts with 'Test')

    path: Path
    issue_url: str
    ticket_id: str
    title: str
    manifest: TestPlanManifest
    embed_captures: bool = False
    skip_validation: bool = False


@dataclass(frozen=True, slots=True)
class TestPlanFlags:
    """The raw CLI flags for ``e2e write-test-plan``, before validation.

    The plan's own content — title, MRs, template — is the manifest's, never a
    second CLI way to say the same thing. What is left is the run's inputs and
    the two user-authorised gate escapes. ``manifest_dir`` is the directory the
    manifest file was read from (empty when the manifest was an inline string):
    relative artifact paths resolve against it. ``skip_validation`` bypasses the
    capture preflight (red-box / duplicate / pre-roll gates) — the agent never
    sets it on its own. ``body_file`` is a path to a pre-authored markdown body
    written verbatim, mutually exclusive with ``manifest``. ``allow_no_video``
    is the escape for the stills-only gate. ``embed_captures`` commits the run's
    captures beside the plan instead of citing them — for a plan issued outside
    the repository, whose readers cannot reach the artifacts directory a
    citation names.
    """

    __test__ = False  # not a pytest test class (name starts with 'Test')

    ticket: str = ""
    manifest: str = ""
    manifest_dir: str = ""
    skip_validation: bool = False
    body_file: str = ""
    allow_no_video: bool = False
    embed_captures: bool = False


def resolve_ticket(ticket: str, worktree: Worktree | None, *, manifest_ticket: str = "") -> Ticket:
    """Resolve the Ticket whose plan file this run writes.

    Precedence: ``--ticket`` (a pk, issue number, or full issue URL) wins; then
    the resolved worktree's ticket; then the manifest's own top-level ``ticket``
    field. Raises :class:`TestPlanResolutionError` when none resolves.
    """
    ref = ticket or (manifest_ticket if worktree is None or worktree.ticket is None else "")
    if ref:
        try:
            return Ticket.objects.resolve(ref)
        except Ticket.DoesNotExist:
            msg = f"No ticket matching {ref!r} (looked up by pk and issue_url)."
            raise TestPlanResolutionError(msg) from None
    if worktree is not None and worktree.ticket is not None:
        return worktree.ticket
    msg = (
        "Could not determine the ticket: pass --ticket <pk|number|url>, "
        "set a top-level 'ticket' in the manifest, or run from inside a worktree."
    )
    raise TestPlanResolutionError(msg)


def artifact_ref(path: Path, *, root: Path | None = None) -> str:
    """The plan's durable reference to one capture: its artifacts-root-relative path.

    Captures stay out of every working tree, so the plan cites them rather than
    embedding them. The reference is relative to the run's artifacts root — an
    explicit *root*, else ``T3_E2E_ARTIFACTS_DIR`` — so a file committed to a
    customer repo never carries a host-absolute path.
    """
    resolved = root or (Path(env) if (env := os.environ.get(ARTIFACTS_ENV, "").strip()) else None)
    if resolved is not None:
        try:
            return f"`{path.relative_to(resolved)}`"
        except ValueError:
            pass
    return f"`{path.name}`"


def _reference_side(side: SideManifest) -> dict[str, WorkflowEmbed]:
    """This side's per-workflow capture references, persisted into the plan state."""
    return {
        name: {
            "video_md": artifact_ref(wf.video) if wf.video is not None else "",
            "image_md": [artifact_ref(img) for img in wf.images],
        }
        for name, wf in side.workflows.items()
    }


def _incoming_captures(write: TestPlanWrite) -> list[Path]:
    """Every image this run would commit beside the plan."""
    sides = (side for side in (write.manifest.dev, write.manifest.local) if side.present)
    return [image for side in sides for wf in side.workflows.values() for image in wf.images]


def _side_embeds(write: TestPlanWrite, *, env: str, evidence_dir: Path) -> dict[str, WorkflowEmbed]:
    """One side's capture references — committed beside the plan, or cited by path."""
    side = write.manifest.dev if env == "dev" else write.manifest.local
    if not side.present:
        return {}
    if write.embed_captures:
        return embed_side_captures(evidence_dir, side=side)
    return _reference_side(side)


def _preflight_captures(manifest: TestPlanManifest, *, skip: bool, allow_no_video: bool) -> None:
    """Run the deterministic capture preflight; re-raise a hard failure for the single catch arm.

    Refuses (fail-loud) on a missing red box, a byte-identical duplicate, a
    stills-only manifest, or a video with excessive blank/static pre-roll — so
    the command exits non-zero before anything is written. ``skip`` bypasses the
    image AND video gates; ``allow_no_video`` is the stills-only escape (both
    user-authorised).
    """
    wfs = [wf for side in (manifest.dev, manifest.local) if side.present for wf in side.workflows.values()]
    try:
        warnings = _tpv.validate_test_plan_images([img for wf in wfs for img in wf.images], skip=skip)
        _tpv.refuse_stills_only(
            has_image=any(wf.images for wf in wfs), has_video=any(wf.video for wf in wfs), allow_no_video=allow_no_video
        )
        _vev.validate_manifest_videos([wf.video for wf in wfs if wf.video is not None], skip=skip)
    except (_tpv.TestPlanImageValidationError, _vev.VideoEvidenceError) as exc:
        raise TestPlanValidationError(str(exc)) from exc
    for warning in warnings:
        _log.warning(warning)


def build_validated_write(flags: TestPlanFlags) -> TestPlanWrite:
    """Run every validator in order and return a fully-validated :class:`TestPlanWrite`.

    Order: manifest parse + per-file existence/media-kind → capture preflight
    (red-box / duplicate / pre-roll) → ticket resolvable → plan path resolvable.
    Any hard failure raises :class:`TestPlanValidationError` (or one of its
    :class:`TestPlanResolutionError` / :class:`TestPlanLocationError`
    subclasses) so the caller exits non-zero with nothing written.
    """
    base_dir = Path(flags.manifest_dir) if flags.manifest_dir else None
    manifest = parse_manifest(flags.manifest, base_dir=base_dir)
    _preflight_captures(manifest, skip=flags.skip_validation, allow_no_video=flags.allow_no_video)
    ticket = resolve_ticket(flags.ticket, _resolve_worktree_or_none(), manifest_ticket=manifest.ticket)
    return TestPlanWrite(
        path=plan_path_for_ticket(ticket),
        issue_url=str(ticket.issue_url),
        ticket_id=ticket.ticket_number,
        title=manifest.title or str(ticket.issue_url) or ticket.ticket_number,
        manifest=manifest,
        embed_captures=flags.embed_captures,
        skip_validation=flags.skip_validation,
    )


def write_test_plan(write: TestPlanWrite) -> PlanWriteResult:
    """Merge this run over the plan file's prior state and rewrite that one file.

    The merge model: the plan file carries a hidden state blob that is the
    source of truth. This run merges the side(s) it carries over the recovered
    prior state (freezing the other side), re-renders the full body, and writes
    it back to the same derived path — so a second run updates the plan rather
    than adding a second copy of it.

    Captures the plan carries in git are gated BEFORE anything is copied or
    written: the set the repo would hold after this run must pass the same
    red-box and duplicate checks the manifest's own captures passed, so a
    hand-placed screenshot in the evidence directory can no longer reach a
    reviewer unvalidated.

    The blocked-body gate does NOT run here: a manifest's ``blocked_workflows``
    renders a literal ``**Blocked:** <reason>`` line, which is the honest
    disclosure mechanism. Only the free-text ``--body-file`` path is scanned.
    """
    evidence_dir = evidence_dir_for(write.path)
    refuse_invalid_committed_captures(
        evidence_dir,
        incoming=_incoming_captures(write) if write.embed_captures else [],
        skip=write.skip_validation,
    )

    prior = read_plan_state(write.path)
    embeds = {env: _side_embeds(write, env=env, evidence_dir=evidence_dir) for env in ("dev", "local")}
    state = merge_state(prior, manifest=write.manifest, title=write.title, embeds=embeds)
    state["ticket"] = write.ticket_id
    body = render_body(state)

    action = "updated" if write.path.is_file() else "created"
    write_plan(write.path, body)
    envs = [env for env, side in (("dev", write.manifest.dev), ("local", write.manifest.local)) if side.present]
    return PlanWriteResult(path=str(write.path), envs=envs, action=action)


def summary_line(result: PlanWriteResult, *, source: str = "") -> str:
    """The one-line human view of a plan write — stderr's, never stdout's.

    Returned rather than written so the command owns the channel: stdout is the
    machine channel `teatree.core.machine_output.emit` reserves for the payload.
    *source* names a non-manifest origin (``from-seams``); empty derives the
    label from the envs the run contributed.
    """
    envs = source or ", ".join(result["envs"]) or "body-file"
    return f"  Test plan {result['action']} ({envs}): {result['path']}"


def run_write_test_plan(
    flags: TestPlanFlags,
    *,
    write_err: Callable[[str], None],
) -> PlanWriteResult:
    """Read the manifest (or body file), validate, and write-or-update the plan file.

    The full ``e2e write-test-plan`` orchestration, factored out of the CLI
    command so the thin command method stays a delegation. When
    ``flags.body_file`` is set, its content is written verbatim (no manifest);
    mutually exclusive with ``flags.manifest``. A
    :class:`TestPlanValidationError` is written to ``write_err`` and re-raised as
    ``SystemExit(1)``.
    """
    if flags.body_file and flags.manifest.strip():
        write_err("--body-file and --manifest are mutually exclusive; supply only one.")
        raise SystemExit(1)

    if not flags.body_file:
        manifest_json, manifest_dir = _read_manifest(flags.manifest, write_err=write_err)
        flags = replace(flags, manifest=manifest_json, manifest_dir=manifest_dir)
    try:
        return _write_body_file(flags) if flags.body_file else write_test_plan(build_validated_write(flags))
    except (TestPlanValidationError, BlockedTestPlanPostError) as err:
        write_err(str(err))
        raise SystemExit(1) from err


def _write_body_file(flags: TestPlanFlags) -> PlanWriteResult:
    """Write a pre-authored body verbatim to the ticket's plan file.

    A hand-authored body is exactly the path that let unvalidated captures reach
    a reviewer, so the captures already committed beside the plan are gated here
    too — the body's prose is the author's, its evidence is not.
    """
    body_path = Path(flags.body_file)
    body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
    if not body.strip():
        msg = f"--body-file {flags.body_file!r} is empty or does not exist."
        raise TestPlanValidationError(msg)
    resolved = resolve_ticket(flags.ticket, _resolve_worktree_or_none())
    check_blocked_body_from_config(body, str(resolved.issue_url))
    path = plan_path_for_ticket(resolved)
    refuse_invalid_committed_captures(evidence_dir_for(path), incoming=[], skip=flags.skip_validation)
    action = "updated" if path.is_file() else "created"
    write_plan(path, body)
    return PlanWriteResult(path=str(path), envs=[], action=action)


def _resolve_worktree_or_none() -> Worktree | None:
    """Resolve the current worktree, or ``None`` when not inside one."""
    try:
        return resolve_worktree()
    except WorktreeNotFoundError:
        return None


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
