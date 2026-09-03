"""Bodies of the ``review`` command group's verdict handlers (#4476).

Extracted from :mod:`teatree.core.management.commands.review` so that module
stays under the module-health LOC cap while the findings read + publish surface
lands. The docstrings stay on the methods — they are the ``--help`` text the
CLI-reference generator reads; only the bodies live here.

Each ``*_result`` function returns ``(payload, human)``: the typed payload the
handler returns to ``call_command``, and the human view the handler routes to
stderr through :func:`teatree.core.machine_output.emit`. The same split the
GitLab-side ``teatree.cli.review.post_impl`` uses.
"""

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, TypedDict

from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.merge import CodeHostQuery, _looks_like_owner_repo
from teatree.core.modelkit.forge_readability import CHECKS_UNREADABLE
from teatree.core.models import ReviewVerdict, ReviewVerdictError, Ticket
from teatree.core.models.review_verdict import Finding, FindingDict
from teatree.core.review.diff_scope_probe import changed_file_set_for_findings
from teatree.core.review.head_workflow_runs import live_checks_at
from teatree.core.review.verdict_findings import (
    FindingsRenderError,
    findings_payload,
    readable_findings,
    render_findings_text,
)
from teatree.core.review.verdict_findings_publish import FindingsPublishError, PublishOutcome, publish_verdict_findings
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from django_typer.management import TyperCommand


class RecordResult(TypedDict, total=False):
    recorded: bool
    verdict_id: int
    pr_id: int
    slug: str
    verdict: str
    findings_count: int
    findings_published: bool
    findings_publish_note: str


class StatusResult(TypedDict, total=False):
    state: str
    slug: str
    pr_id: int
    verdict: str
    reviewed_sha: str
    current_head_sha: str
    live_checks: str
    reviewer_identity: str
    findings_count: int
    findings: list[FindingDict]
    findings_error: str
    error: str


class FindingsResult(TypedDict, total=False):
    state: str
    slug: str
    pr_id: int
    verdict_id: int
    verdict: str
    reviewed_sha: str
    reviewer_identity: str
    findings_count: int
    findings: list[FindingDict]
    error: str


class PublishFindingsResult(TypedDict, total=False):
    slug: str
    pr_id: int
    verdict_id: int
    published: bool
    skipped_existing: bool
    comment_url: str
    blocked_reason: str
    note: str
    error: str


def parse_findings(raw: str) -> list[Finding]:
    """Parse the ``--findings-json`` payload into structured :class:`Finding` objects.

    Expects a JSON array of ``{"severity", "summary", "file"?, "line"?}``
    objects. An empty string yields no findings (a clean verdict). A non-object
    element is REFUSED rather than dropped: a silently-dropped finding leaves the
    recorded count standing in front of content nobody can read (#4476).
    """
    if not raw.strip():
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        msg = "--findings-json must be a JSON array of finding objects"
        raise TypeError(msg)
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            msg = f"--findings-json element {index} is {type(item).__name__}, not an object"
            raise TypeError(msg)
    return [Finding.from_dict(item) for item in data]


def resolve_ref(command: "TyperCommand", mr_url: str) -> PrRef:
    """The parsed PR/MR reference for *mr_url*, refusing an unrecognised URL."""
    ref = pr_ref_from_url(mr_url)
    if ref is None:
        command.stderr.write(f"  could not parse a PR/MR URL from {mr_url!r}")
        raise SystemExit(1)
    return ref


def _verdict_for(ref: PrRef, reviewed_sha: str) -> ReviewVerdict | None:
    if not reviewed_sha.strip():
        return ReviewVerdict.objects.latest_for_pr(ref.slug, ref.pr_id)
    return ReviewVerdict.objects.for_pr(ref.slug, ref.pr_id).filter(reviewed_sha=reviewed_sha.strip().lower()).first()


@dataclass(frozen=True, slots=True)
class RecordRequest:
    """The ``review record`` CLI flags as one value — the handler's whole input."""

    pr_id: int
    slug: str
    reviewed_sha: str = ""
    verdict: str = "merge_safe"
    reviewer_identity: str = ""
    gh_verify_result: str = "green"
    blast_class: str = "logic"
    findings_json: str = ""
    ticket_id: int = 0
    lock_holder: str = ""
    merge_result_retake: bool = False


def record_result(command: "TyperCommand", request: RecordRequest) -> tuple[RecordResult, str]:
    """Persist the verdict and publish its findings, returning the payload + human view."""
    _assert_recordable(command, request)
    try:
        require_current_schema()
    except SelfDbMigrationError as exc:
        _refuse(command, f"record refused: {exc}")

    resolved_ticket = None
    if request.ticket_id:
        try:
            resolved_ticket = Ticket.objects.get(pk=request.ticket_id)
        except Ticket.DoesNotExist:
            _refuse(command, f"record refused: Ticket {request.ticket_id} not found")

    try:
        findings = parse_findings(request.findings_json)
    except (TypeError, ValueError) as exc:
        _refuse(command, f"record refused: {exc}")

    try:
        recorded = ReviewVerdict.record(
            pr_id=request.pr_id,
            slug=request.slug,
            reviewed_sha=request.reviewed_sha,
            verdict=request.verdict,
            reviewer_identity=request.reviewer_identity,
            findings=findings,
            blast_class=request.blast_class,
            gh_verify_result=request.gh_verify_result,
            ticket=resolved_ticket,
            lock_holder=request.lock_holder,
            changed_files=changed_file_set_for_findings(findings, slug=request.slug, pr_id=request.pr_id),
            merge_result_retake=request.merge_result_retake,
            live_checks=live_checks_at,
        )
    except ReviewVerdictError as exc:
        _refuse(command, f"record refused: {exc}")

    published, publish_line = _publish_on_record(recorded)
    lines = [
        (
            f"  recorded {recorded.verdict} verdict {recorded.pk} for "
            f"{recorded.slug}#{recorded.pr_id}@{recorded.reviewed_sha[:8]} ({len(findings)} finding(s))"
        ),
        *emit_review_done_signal(recorded),
        *trigger_sweep(recorded),
        publish_line,
    ]
    payload: RecordResult = {
        "recorded": True,
        "verdict_id": int(recorded.pk),
        "pr_id": int(recorded.pr_id),
        "slug": recorded.slug,
        "verdict": recorded.verdict,
        "findings_count": len(findings),
        "findings_published": published,
        "findings_publish_note": publish_line.strip(),
    }
    return payload, "\n".join(line for line in lines if line)


def _refuse(command: "TyperCommand", reason: str) -> NoReturn:
    """Stop on *reason* with a nonzero exit — an ``{"error": …}`` return prints and exits 0 (#932)."""
    command.stderr.write(f"  {reason}")
    raise SystemExit(1)


def _assert_recordable(command: "TyperCommand", request: RecordRequest) -> None:
    """Refuse a slug that is not ``owner/repo``, or a verdict not bound to a full SHA."""
    if not _looks_like_owner_repo(request.slug):
        _refuse(
            command,
            f"record refused: slug must be owner/repo (got {request.slug!r}) — this looks like a branch "
            f"name; the review-verdict / merge lookup keys by repo slug",
        )
    if not request.reviewed_sha.strip():
        _refuse(command, "record refused: --reviewed-sha is required (full hex commit id of the reviewed tree)")


def _publish_on_record(recorded: ReviewVerdict) -> tuple[bool, str]:
    """Publish *recorded*'s findings to its PR, degrading loudly and never raising.

    A recorded verdict is the durable artifact; a forge outage or a withheld
    approval must not discard it. Every non-published case names its reason, so
    no caller can read silence as "there was nothing to post".
    """
    if not recorded.findings:
        return False, ""
    try:
        outcome = publish_verdict_findings(recorded)
    except (FindingsRenderError, FindingsPublishError) as exc:
        return False, f"  findings NOT posted to {recorded.slug}#{recorded.pr_id}: {exc}"
    except Exception as exc:  # noqa: BLE001 — a forge failure must not discard the recorded verdict
        return False, f"  findings NOT posted to {recorded.slug}#{recorded.pr_id}: {exc}"
    return outcome.published, _publish_line(recorded, outcome)


def _publish_line(recorded: ReviewVerdict, outcome: PublishOutcome) -> str:
    if outcome.published:
        return f"  posted findings to {recorded.slug}#{recorded.pr_id}: {outcome.comment_url}"
    if outcome.skipped_existing:
        return f"  findings already posted on {recorded.slug}#{recorded.pr_id} — not duplicated"
    if outcome.blocked_reason:
        return f"  findings NOT posted (gate): {outcome.blocked_reason}"
    return f"  {outcome.note}"


def trigger_sweep(recorded: ReviewVerdict) -> list[str]:
    """Run the pr_sweep merge decision for *recorded* PR now, not next tick (#2026)."""
    if not recorded.is_merge_safe():
        return []
    from teatree.loop.sweep_on_demand import trigger_sweep_for_verdict  # noqa: PLC0415 — lazy command import

    attempt = trigger_sweep_for_verdict(
        slug=recorded.slug,
        pr_id=int(recorded.pr_id),
        overlay=os.environ.get("T3_OVERLAY_NAME", ""),
    )
    if attempt is not None and attempt.merged:
        return [f"  pr_sweep merged {attempt.slug}#{attempt.pr_id} @ {attempt.merged_sha[:8]}"]
    return []


def emit_review_done_signal(recorded: ReviewVerdict) -> list[str]:
    """Post the review-DONE Slack reaction set for *recorded* (#113/#88), best-effort."""
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: lazy command import
    from teatree.loop.review_claim import emit_review_done_reactions  # noqa: PLC0415 — lazy command import

    try:
        posted = emit_review_done_reactions(
            slug=recorded.slug,
            pr_id=int(recorded.pr_id),
            emojis=recorded.done_reaction_emojis(),
            messaging=messaging_from_overlay(),
        )
    except Exception:  # noqa: BLE001 — the Slack signal must never break verdict recording.
        return []
    if posted:
        return [f"  posted review-DONE reaction(s) {', '.join(':' + e + ':' for e in posted)}"]
    return []


def status_result(command: "TyperCommand", mr_url: str) -> tuple[StatusResult, str]:
    """Report whether *mr_url* is safe to approve at its CURRENT head."""
    ref = resolve_ref(command, mr_url)

    recorded = ReviewVerdict.objects.latest_for_pr(ref.slug, ref.pr_id)
    if recorded is None:
        human = f"  no recorded verdict for {ref.slug}#{ref.pr_id} — run a cold review first"
        return {"state": "no_verdict", "slug": ref.slug, "pr_id": ref.pr_id}, human

    query = CodeHostQuery.for_ref(ref)
    # An UNREADABLE forge read is not a moved head (#4462). Collapsing the two would report
    # `stale` for a read that failed and send the caller to spend a cold re-review on a tree
    # nobody has shown to have changed; the recorded verdict still stands, so the fix is to
    # retry the READ. `live_head_sha()` flattens both to "", which is why this reads the
    # richer `live_head_read()` instead.
    head = query.live_head_read()
    stands = f"the recorded {recorded.verdict} at {recorded.reviewed_sha[:8]} STANDS — retry, do NOT re-review"
    if head.unreadable:
        human = f"  head unreadable: the forge named no head for {ref.slug}#{ref.pr_id} — {stands}"
        return {"state": "head_unreadable", "slug": ref.slug, "pr_id": ref.pr_id, "verdict": recorded.verdict}, human
    current_head = head.sha
    if recorded.is_stale_at(current_head):
        human = (
            f"  stale: verdict reviewed {recorded.reviewed_sha[:8]} but head moved to "
            f"{(current_head[:8] or '<unknown>')} — re-review needed ({ref.slug}#{ref.pr_id})"
        )
        return {
            "state": "stale",
            "slug": ref.slug,
            "pr_id": ref.pr_id,
            "verdict": recorded.verdict,
            "reviewed_sha": recorded.reviewed_sha,
            "current_head_sha": current_head,
        }, human

    live_checks = query.required_checks_status()
    if live_checks == CHECKS_UNREADABLE:
        human = f"  checks unreadable: the forge named no verdict for {ref.slug}#{ref.pr_id} — {stands}"
        return {"state": "checks_unreadable", "slug": ref.slug, "pr_id": ref.pr_id, "verdict": recorded.verdict}, human
    if recorded.is_safe_to_approve_at(current_head, live_checks_status=live_checks):
        human = (
            f"  safe-to-approve: {recorded.verdict} at {recorded.reviewed_sha[:8]}, checks green "
            f"({ref.slug}#{ref.pr_id}, reviewer={recorded.reviewer_identity})"
        )
        state = "safe_to_approve"
    else:
        reason = "verdict is HOLD" if not recorded.is_merge_safe() else f"live checks {live_checks!r}"
        human = f"  not safe-to-approve at {recorded.reviewed_sha[:8]}: {reason} ({ref.slug}#{ref.pr_id})"
        state = "not_safe"
    readable = readable_findings(recorded)
    result: StatusResult = {
        "state": state,
        "slug": ref.slug,
        "pr_id": ref.pr_id,
        "verdict": recorded.verdict,
        "reviewed_sha": recorded.reviewed_sha,
        "current_head_sha": current_head,
        "live_checks": live_checks,
        "reviewer_identity": recorded.reviewer_identity,
        "findings_count": readable.recorded_count if readable.error else len(readable.payload),
        "findings": readable.payload,
    }
    if readable.error:
        result["findings_error"] = readable.error
        unreadable = (
            f"  findings unreadable ({readable.recorded_count} recorded): {readable.error} — the verdict "
            f"above stands; read them with `t3 <overlay> review findings <pr-url>`"
        )
        return result, f"{human}\n{unreadable}"
    return result, "\n".join([human, render_findings_text(recorded)]) if readable.payload else human


def findings_result(command: "TyperCommand", mr_url: str, reviewed_sha: str) -> tuple[FindingsResult, str]:
    """The recorded findings for *mr_url* — the read surface a HOLD is acted on through."""
    ref = resolve_ref(command, mr_url)
    recorded = _verdict_for(ref, reviewed_sha)
    if recorded is None:
        at = f" at {reviewed_sha.strip()[:8]}" if reviewed_sha.strip() else ""
        human = f"  no recorded verdict for {ref.slug}#{ref.pr_id}{at} — run a cold review first"
        return {"state": "no_verdict", "slug": ref.slug, "pr_id": ref.pr_id, "findings_count": 0}, human

    payload = findings_payload(recorded)
    return {
        "slug": ref.slug,
        "pr_id": ref.pr_id,
        "verdict_id": int(recorded.pk),
        "verdict": recorded.verdict,
        "reviewed_sha": recorded.reviewed_sha,
        "reviewer_identity": recorded.reviewer_identity,
        "findings_count": len(payload),
        "findings": payload,
    }, render_findings_text(recorded)


def publish_findings_result(
    command: "TyperCommand", mr_url: str, reviewed_sha: str
) -> tuple[PublishFindingsResult, str]:
    """Post a recorded verdict's findings to its PR — the retry after an approval lands."""
    ref = resolve_ref(command, mr_url)
    recorded = _verdict_for(ref, reviewed_sha)
    if recorded is None:
        human = f"  no recorded verdict for {ref.slug}#{ref.pr_id} — nothing to publish"
        return {"slug": ref.slug, "pr_id": ref.pr_id, "published": False, "note": human.strip()}, human

    try:
        outcome = publish_verdict_findings(recorded, host_kind=ref.host_kind)
    except (FindingsRenderError, FindingsPublishError) as exc:
        return {
            "slug": ref.slug,
            "pr_id": ref.pr_id,
            "verdict_id": int(recorded.pk),
            "published": False,
            "error": str(exc),
        }, f"  publish-findings refused: {exc}"

    return {
        "slug": ref.slug,
        "pr_id": ref.pr_id,
        "verdict_id": int(recorded.pk),
        "published": outcome.published,
        "skipped_existing": outcome.skipped_existing,
        "comment_url": outcome.comment_url,
        "blocked_reason": outcome.blocked_reason,
        "note": outcome.note,
    }, _publish_line(recorded, outcome)
