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

Forge calls (``fetch_live_head_sha`` / ``fetch_required_checks_status``) are the
only external boundary; the rest is a DB read.
"""

import json
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.merge_execution import fetch_live_head_sha, fetch_required_checks_status
from teatree.core.models import Finding, ReviewVerdict, ReviewVerdictError, Ticket
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
        self._trigger_sweep(recorded)
        return {
            "recorded": True,
            "verdict_id": int(recorded.pk),
            "pr_id": int(recorded.pr_id),
            "slug": recorded.slug,
            "verdict": recorded.verdict,
            "findings_count": len(findings),
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
        import os  # noqa: PLC0415

        from teatree.loop.sweep_on_demand import trigger_sweep_for_verdict  # noqa: PLC0415

        attempt = trigger_sweep_for_verdict(
            slug=recorded.slug,
            pr_id=int(recorded.pr_id),
            overlay=os.environ.get("T3_OVERLAY_NAME", ""),
        )
        if attempt is not None and attempt.merged:
            self.stdout.write(f"  pr_sweep merged {attempt.slug}#{attempt.pr_id} @ {attempt.merged_sha[:8]}")

    def _emit_review_done_signal(self, recorded: ReviewVerdict) -> None:
        """Post the review-DONE Slack reaction set for *recorded* (#113/#88).

        ``:eyes:`` + the verdict emoji (``:white_check_mark:`` clean /
        ``:question:`` blocking) on the PR's review-broadcast message — the
        ONLY Slack signal a finished review produces, never an author DM.
        Best-effort: any failure logs and continues — a Slack outage must not
        turn a recorded verdict into a command failure.
        """
        from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415
        from teatree.loop.review_claim import emit_review_done_reactions  # noqa: PLC0415

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

        recorded = ReviewVerdict.objects.latest_for_pr(ref.slug, ref.number)
        if recorded is None:
            self.stdout.write(f"  no recorded verdict for {ref.slug}#{ref.number} — run a cold review first")
            return {"state": "no_verdict", "slug": ref.slug, "pr_id": ref.number}

        current_head = fetch_live_head_sha(ref.slug, ref.number, host_kind=ref.host_kind)
        if recorded.is_stale_at(current_head):
            self.stdout.write(
                f"  stale: verdict reviewed {recorded.reviewed_sha[:8]} but head moved to "
                f"{(current_head[:8] or '<unknown>')} — re-review needed ({ref.slug}#{ref.number})"
            )
            return {
                "state": "stale",
                "slug": ref.slug,
                "pr_id": ref.number,
                "verdict": recorded.verdict,
                "reviewed_sha": recorded.reviewed_sha,
                "current_head_sha": current_head,
            }

        live_checks = fetch_required_checks_status(ref.slug, ref.number, host_kind=ref.host_kind)
        if recorded.is_safe_to_approve_at(current_head, live_checks_status=live_checks):
            self.stdout.write(
                f"  safe-to-approve: {recorded.verdict} at {recorded.reviewed_sha[:8]}, checks green "
                f"({ref.slug}#{ref.number}, reviewer={recorded.reviewer_identity})"
            )
            state = "safe_to_approve"
        else:
            reason = "verdict is HOLD" if not recorded.is_merge_safe() else f"live checks {live_checks!r}"
            self.stdout.write(
                f"  not safe-to-approve at {recorded.reviewed_sha[:8]}: {reason} ({ref.slug}#{ref.number})"
            )
            state = "not_safe"
        return {
            "state": state,
            "slug": ref.slug,
            "pr_id": ref.number,
            "verdict": recorded.verdict,
            "reviewed_sha": recorded.reviewed_sha,
            "current_head_sha": current_head,
            "live_checks": live_checks,
            "reviewer_identity": recorded.reviewer_identity,
            "findings_count": len(recorded.findings),
        }
