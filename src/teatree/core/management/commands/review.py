"""``t3 <overlay> review record`` / ``review status`` — persist + look up cold-review verdicts.

``review record`` writes a durable :class:`~teatree.core.models.review_verdict.ReviewVerdict`
for a PR at an exact reviewed SHA so the merge-safe/hold judgment is stored once
instead of being re-derived from scratch on every session. The CLEAR-issuing
path (``ticket clear``) records a ``merge_safe`` verdict as a by-product; this
command is the standalone seam for recording a verdict directly — notably a
HOLD, which a CLEAR can never carry.

``review status <mr-url>`` is the read-side payoff: a cheap lookup before
re-running a full cold review. It parses the PR URL, fetches the forge's live
head SHA, and reports against the *latest* recorded verdict — ``safe-to-approve``
(verdict is merge_safe, ``reviewed_sha`` still equals the live head, and the live
required-checks rollup is green), ``stale`` (a verdict exists but the head moved
off the reviewed tree, so a re-review is needed), or ``no recorded verdict``.

Forge calls (``CodeHostQuery.live_head_sha`` / ``CodeHostQuery.required_checks_status``)
are the only external boundary; the rest is a DB read.
"""

import json
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.merge import CodeHostQuery
from teatree.core.merge.conflict_only import rebind_clearance_after_conflict_only_merge
from teatree.core.models import (
    Finding,
    MergeClear,
    MRReviewLock,
    ReviewEvidence,
    ReviewEvidenceError,
    ReviewVerdict,
    ReviewVerdictError,
    Ticket,
)
from teatree.project import find_project_root
from teatree.utils.url_slug import pr_ref_from_url


class RecordResult(TypedDict, total=False):
    recorded: bool
    verdict_id: int
    pr_id: int
    slug: str
    verdict: str
    findings_count: int
    error: str


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
    error: str


class RecordEvidenceResult(TypedDict, total=False):
    recorded: bool
    evidence_id: int
    ticket_id: int
    kind: str
    error: str


class LockAcquireResult(TypedDict, total=False):
    acquired: bool
    slug: str
    pr_id: int
    state: str
    holder: str
    error: str


class LockStatusResult(TypedDict, total=False):
    slug: str
    pr_id: int
    locked: bool
    state: str
    holder: str
    error: str


class RebindClearanceResult(TypedDict, total=False):
    rebound: bool
    clear_id: int
    reviewed_sha: str
    merge_sha: str


def _project_root_or_cwd() -> str:
    """The cwd project root the merge commit is resolved against, or ``.`` when none resolves."""
    root = find_project_root()
    return str(root) if root is not None else "."


def _parse_findings(raw: str) -> list[Finding]:
    """Parse the ``--findings-json`` payload into structured :class:`Finding` objects.

    Expects a JSON array of ``{"severity", "summary", "file"?, "line"?}``
    objects. An empty string yields no findings (a clean verdict).
    """
    if not raw.strip():
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        msg = "--findings-json must be a JSON array of finding objects"
        raise TypeError(msg)
    return [Finding.from_dict(item) for item in data if isinstance(item, dict)]


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> review`` group root."""

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — django-typer command: every param maps 1:1 to a ReviewVerdict field, the arg list IS the public CLI surface (same rationale as `ticket clear`).
        self,
        pr_id: int,
        slug: str,
        *,
        reviewed_sha: Annotated[
            str, typer.Option("--reviewed-sha", help="Full 40-char hex commit id of the reviewed tree.")
        ] = "",
        verdict: Annotated[str, typer.Option(help="merge_safe / hold.")] = "merge_safe",
        reviewer_identity: Annotated[str, typer.Option(help="Identity of the reviewer who reached this verdict.")] = "",
        gh_verify_result: Annotated[
            str, typer.Option(help="Checks snapshot at review time: green / pending / failed.")
        ] = "green",
        blast_class: Annotated[str, typer.Option(help="Reviewer judgment: substrate / logic / docs.")] = "logic",
        findings_json: Annotated[
            str, typer.Option("--findings-json", help='JSON array of {"severity","summary","file","line"} findings.')
        ] = "",
        ticket_id: Annotated[int, typer.Option(help="Optional teatree Ticket id this verdict is for.")] = 0,
    ) -> RecordResult:
        """Persist a cold-review verdict for a PR at an exact reviewed SHA.

        The durable sibling of ``ticket clear``: where a CLEAR authorises one
        merge, this records the *judgment* so ``review status`` can answer
        "safe to approve at the current head?" without a fresh cold review.
        Refuses the same way ``MergeClear.issue`` does (full-SHA bind, known
        verdict/blast/verify, non-empty reviewer, no merge_safe-on-red-checks).
        """
        if not reviewed_sha.strip():
            self.stderr.write("  record refused: --reviewed-sha is required (full hex commit id of the reviewed tree)")
            raise SystemExit(1)
        try:
            require_current_schema()
        except SelfDbMigrationError as exc:
            self.stdout.write(f"  record refused: {exc}")
            return {"recorded": False, "error": str(exc)}

        resolved_ticket = None
        if ticket_id:
            try:
                resolved_ticket = Ticket.objects.get(pk=ticket_id)
            except Ticket.DoesNotExist:
                return {"recorded": False, "error": f"Ticket {ticket_id} not found"}

        try:
            findings = _parse_findings(findings_json)
        except (TypeError, ValueError) as exc:
            self.stdout.write(f"  record refused: {exc}")
            return {"recorded": False, "error": str(exc)}

        try:
            recorded = ReviewVerdict.record(
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=reviewed_sha,
                verdict=verdict,
                reviewer_identity=reviewer_identity,
                findings=findings,
                blast_class=blast_class,
                gh_verify_result=gh_verify_result,
                ticket=resolved_ticket,
            )
        except ReviewVerdictError as exc:
            self.stdout.write(f"  record refused: {exc}")
            return {"recorded": False, "error": str(exc)}

        self.stdout.write(
            f"  recorded {recorded.verdict} verdict {recorded.pk} for "
            f"{recorded.slug}#{recorded.pr_id}@{recorded.reviewed_sha[:8]} ({len(findings)} finding(s))"
        )
        self._emit_review_done_signal(recorded)
        self._advance_review_loop(recorded)
        self._trigger_sweep(recorded)
        return {
            "recorded": True,
            "verdict_id": int(recorded.pk),
            "pr_id": int(recorded.pr_id),
            "slug": recorded.slug,
            "verdict": recorded.verdict,
            "findings_count": len(findings),
        }

    @command(name="record-evidence")
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record_evidence(  # noqa: PLR0913 — django-typer command: every param maps 1:1 to a ReviewEvidence field / CLI flag.
        self,
        ticket_id: int,
        *,
        kind: Annotated[str, typer.Option(help="cold_review / integration_review.")] = "cold_review",
        reviewer: Annotated[str, typer.Option("--reviewer", help="Reviewer identity (not a maker/loop role).")] = "",
        verdict: Annotated[str, typer.Option(help="Review verdict, e.g. merge_safe / hold / pass.")] = "",
        head_sha: Annotated[
            str, typer.Option("--head-sha", help="Full 40-char hex commit id of the reviewed tree.")
        ] = "",
        repos: Annotated[
            str,
            typer.Option("--repos", help="Comma-separated repos covered (≥2 required for integration_review)."),
        ] = "",
    ) -> RecordEvidenceResult:
        """Record a PR-08 review-evidence artifact for a ticket.

        Two kinds share the surface: ``cold_review`` satisfies the review-request
        review-state gate; ``integration_review`` (with ≥ 2 ``--repos``)
        satisfies the cross-repo ticket-close gate. Refuses on a maker/loop
        reviewer, a blank verdict, a non-40-char SHA, or a single-repo
        integration review — the same ``ReviewEvidence.record`` contract.
        """
        try:
            require_current_schema()
        except SelfDbMigrationError as exc:
            self.stdout.write(f"  record-evidence refused: {exc}")
            return {"recorded": False, "error": str(exc)}
        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None

        repo_list = [chunk.strip() for chunk in repos.split(",") if chunk.strip()]
        try:
            evidence = ReviewEvidence.record(
                ticket=ticket,
                kind=kind,
                reviewer_identity=reviewer,
                verdict=verdict,
                head_sha=head_sha,
                repos=repo_list,
            )
        except ReviewEvidenceError as exc:
            self.stdout.write(f"  record-evidence refused: {exc}")
            return {"recorded": False, "error": str(exc)}

        self.stdout.write(f"  recorded {evidence.kind} evidence {evidence.pk} for ticket {ticket_id}")
        return {
            "recorded": True,
            "evidence_id": int(evidence.pk),
            "ticket_id": ticket_id,
            "kind": evidence.kind,
        }

    def _trigger_sweep(self, recorded: ReviewVerdict) -> None:
        """Run the pr_sweep merge decision for *recorded* PR now, not next tick (#2026).

        A ``merge_safe`` verdict is the artifact the sweep merges on; recording
        one for an own PR the sweep is waiting on must not idle a full ~12-min
        cadence (a parallel human keystone-merge wins that race — the incident
        this fixes). Only ``merge_safe`` verdicts trigger a merge attempt; a
        HOLD never merges. Best-effort: a failure logs inside the trigger and
        never turns verdict recording into a command failure; the periodic sweep
        is the unchanged backstop.
        """
        if not recorded.is_merge_safe():
            return
        import os  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.loop.sweep_on_demand import trigger_sweep_for_verdict  # noqa: PLC0415 — lazy command import

        attempt = trigger_sweep_for_verdict(
            slug=recorded.slug,
            pr_id=int(recorded.pr_id),
            overlay=os.environ.get("T3_OVERLAY_NAME", ""),
        )
        if attempt is not None and attempt.merged:
            self.stdout.write(f"  pr_sweep merged {attempt.slug}#{attempt.pr_id} @ {attempt.merged_sha[:8]}")

    def _advance_review_loop(self, recorded: ReviewVerdict) -> None:
        """Drive the open EXTERNAL ReviewLoop for *recorded* from its verdict (#2298).

        The pre-#2298 chokepoint left a HOLD inert (``_trigger_sweep`` only
        fires on a merge_safe verdict), so the punch-list never fed back to the
        author. Binding the verdict to the loop and calling the verdict-guarded
        transition makes a HOLD re-arm an author leg (or exhaust at the round
        cap) and a merge_safe terminate at PASSED. Non-loop PRs (no open
        EXTERNAL loop for the verdict's ticket) are unchanged. Best-effort: any
        failure logs and never turns verdict recording into a command failure;
        the periodic sweep/scan is the backstop.
        """
        ticket_id = recorded.ticket_id  # type: ignore[attr-defined]  # Django implicit FK id
        if ticket_id is None:
            return
        from teatree.core.models import ReviewLoop  # noqa: PLC0415 — deferred: ORM import needs the app registry

        loop = ReviewLoop.open_external_for_ticket(ticket_id)
        if loop is None:
            return
        try:
            loop.advance_from_recorded_verdict(recorded)
        except Exception:  # noqa: BLE001 — loop advance must never break verdict recording.
            return
        self.stdout.write(f"  advanced review-loop {loop.pk} to {loop.state}")

    def _emit_review_done_signal(self, recorded: ReviewVerdict) -> None:
        """Post the review-DONE Slack reaction set for *recorded* (#113/#88).

        ``:eyes:`` + the verdict emoji (``:white_check_mark:`` clean /
        ``:question:`` blocking) on the PR's review-broadcast message — the
        ONLY Slack signal a finished review produces, never an author DM.
        Best-effort: any failure logs and continues — a Slack outage must not
        turn a recorded verdict into a command failure.
        """
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
            return
        if posted:
            self.stdout.write(f"  posted review-DONE reaction(s) {', '.join(':' + e + ':' for e in posted)}")

    @command()
    def status(self, mr_url: str) -> StatusResult:
        """Report whether *mr_url* is safe to approve at its CURRENT head (read-only).

        Parses the PR/MR URL, fetches the live head SHA, looks up the latest
        recorded verdict, and prints one of: ``safe-to-approve``, ``stale``
        (head moved — re-review needed), or ``no recorded verdict``. The point
        is to avoid re-deriving a full cold review when a fresh verdict already
        vouches for the current tree.
        """
        ref = pr_ref_from_url(mr_url)
        if ref is None:
            self.stderr.write(f"  could not parse a PR/MR URL from {mr_url!r}")
            raise SystemExit(1)

        recorded = ReviewVerdict.objects.latest_for_pr(ref.slug, ref.pr_id)
        if recorded is None:
            self.stdout.write(f"  no recorded verdict for {ref.slug}#{ref.pr_id} — run a cold review first")
            return {"state": "no_verdict", "slug": ref.slug, "pr_id": ref.pr_id}

        query = CodeHostQuery.for_ref(ref)
        current_head = query.live_head_sha()
        if recorded.is_stale_at(current_head):
            self.stdout.write(
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
            }

        live_checks = query.required_checks_status()
        if recorded.is_safe_to_approve_at(current_head, live_checks_status=live_checks):
            self.stdout.write(
                f"  safe-to-approve: {recorded.verdict} at {recorded.reviewed_sha[:8]}, checks green "
                f"({ref.slug}#{ref.pr_id}, reviewer={recorded.reviewer_identity})"
            )
            state = "safe_to_approve"
        else:
            reason = "verdict is HOLD" if not recorded.is_merge_safe() else f"live checks {live_checks!r}"
            self.stdout.write(
                f"  not safe-to-approve at {recorded.reviewed_sha[:8]}: {reason} ({ref.slug}#{ref.pr_id})"
            )
            state = "not_safe"
        return {
            "state": state,
            "slug": ref.slug,
            "pr_id": ref.pr_id,
            "verdict": recorded.verdict,
            "reviewed_sha": recorded.reviewed_sha,
            "current_head_sha": current_head,
            "live_checks": live_checks,
            "reviewer_identity": recorded.reviewer_identity,
            "findings_count": len(recorded.findings),
        }

    @command(name="lock-acquire")
    def lock_acquire(
        self,
        mr_url: str,
        *,
        holder: Annotated[
            str, typer.Option(help="Identity of the dispatcher acquiring the lock (agent/session id).")
        ] = "",
    ) -> LockAcquireResult:
        """Acquire the per-MR review-dispatch lock BEFORE a manual Agent() reviewer dispatch (#1405).

        Run this before spawning a `t3:reviewer` sub-agent via the Agent tool.
        ``acquired: true`` means proceed with the dispatch — the lock is now
        held by ``holder``. ``acquired: false`` means a review is already in
        flight for this MR (state + holder are reported); skip the dispatch,
        the in-flight review already covers it.
        """
        if not holder.strip():
            self.stderr.write("  lock-acquire refused: --holder is required (identity of the dispatcher)")
            raise SystemExit(1)
        try:
            require_current_schema()
        except SelfDbMigrationError as exc:
            self.stdout.write(f"  lock-acquire refused: {exc}")
            return {"acquired": False, "error": str(exc)}

        ref = pr_ref_from_url(mr_url)
        if ref is None:
            self.stderr.write(f"  could not parse a PR/MR URL from {mr_url!r}")
            raise SystemExit(1)

        lock = MRReviewLock.acquire(slug=ref.slug, pr_id=ref.pr_id, holder=holder, mr_url=mr_url)
        if lock is not None:
            self.stdout.write(f"  acquired: {ref.slug}#{ref.pr_id} now held by {holder!r} — dispatch the reviewer")
            return {
                "acquired": True,
                "slug": ref.slug,
                "pr_id": ref.pr_id,
                "state": lock.state,
                "holder": lock.holder,
            }

        held = MRReviewLock.objects.filter(slug=ref.slug, pr_id=ref.pr_id).first()
        held_state = held.state if held is not None else ""
        held_by = held.holder if held is not None else ""
        self.stdout.write(
            f"  not acquired: {ref.slug}#{ref.pr_id} is already {held_state!r} held by {held_by!r} — "
            f"skip the dispatch, a review is in flight"
        )
        return {"acquired": False, "slug": ref.slug, "pr_id": ref.pr_id, "state": held_state, "holder": held_by}

    @command(name="lock-status")
    def lock_status(self, mr_url: str) -> LockStatusResult:
        """Report the current :class:`MRReviewLock` state for *mr_url* (read-only)."""
        ref = pr_ref_from_url(mr_url)
        if ref is None:
            self.stderr.write(f"  could not parse a PR/MR URL from {mr_url!r}")
            raise SystemExit(1)

        lock = MRReviewLock.objects.filter(slug=ref.slug, pr_id=ref.pr_id).first()
        if lock is None:
            self.stdout.write(f"  no lock recorded for {ref.slug}#{ref.pr_id} — idle")
            return {"slug": ref.slug, "pr_id": ref.pr_id, "locked": False, "state": "idle", "holder": ""}

        self.stdout.write(
            f"  {ref.slug}#{ref.pr_id}: state={lock.state!r} holder={lock.holder!r} locked={lock.is_locked()}"
        )
        return {
            "slug": ref.slug,
            "pr_id": ref.pr_id,
            "locked": lock.is_locked(),
            "state": lock.state,
            "holder": lock.holder,
        }

    @command(name="rebind-clearance")
    def rebind_clearance(
        self,
        clear_id: int,
        merge_sha: Annotated[str, typer.Option("--merge-sha", help="Full 40-char hex SHA of the merge commit.")] = "",
        repo_root: Annotated[
            str, typer.Option("--repo-root", help="Git clone the merge commit lives in (default: cwd project root).")
        ] = "",
    ) -> RebindClearanceResult:
        """Re-bind a CLEAR to a conflict-only merge commit — no re-review (PR-07).

        After ``origin/main`` is merged into a reviewed branch to resolve conflicts
        (merge, never rebase — §17.4), the head moves and the SHA-bind gate refuses
        it. This re-binds ONLY when the merge commit's first parent is the reviewed
        SHA AND the commit is conflict-resolution-only; the original independent
        verdict is carried forward to the merge SHA, so the merge preconditions pass
        at the new head. A substantive merge is refused — a fresh review is required.
        """
        if not merge_sha.strip():
            self.stderr.write("  rebind-clearance refused: --merge-sha is required (full 40-char hex SHA)")
            raise SystemExit(1)
        try:
            clear = MergeClear.objects.get(pk=clear_id)
        except MergeClear.DoesNotExist:
            self.stderr.write(f"  MergeClear {clear_id} not found")
            raise SystemExit(1) from None

        root = repo_root.strip() or _project_root_or_cwd()
        rebound = rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=root)
        clear.refresh_from_db()
        if rebound:
            self.stdout.write(f"  re-bound CLEAR {clear.pk} to conflict-only merge {merge_sha[:8]}")
        else:
            self.stdout.write(
                f"  CLEAR {clear.pk} NOT re-bound — {merge_sha[:8]} is not a conflict-only merge whose "
                f"first parent is the reviewed SHA; a fresh review is required"
            )
        return {
            "rebound": rebound,
            "clear_id": int(clear.pk),
            "reviewed_sha": clear.reviewed_sha,
            "merge_sha": merge_sha.strip().lower(),
        }
