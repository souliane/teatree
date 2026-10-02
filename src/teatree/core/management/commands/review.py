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

A forge that could not be READ gets its own two words — ``head unreadable`` and
``checks unreadable`` — rather than borrowing ``stale``. The recorded verdict STANDS:
nothing about the PR changed, only our ability to look at it, so the answer is to
retry the read, never to spend a cold re-review on an untouched tree.

Forge calls (``CodeHostQuery.live_head_read`` / ``CodeHostQuery.required_checks_status``)
are the only external boundary; the rest is a DB read.
"""

from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import command, initialize

from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.management.commands import _review_impl
from teatree.core.management.commands._review_impl import (
    FindingsResult,
    PublishFindingsResult,
    RecordResult,
    StatusResult,
)
from teatree.core.management.commands._reviewer_policy_commands import ReviewerPolicyCommands
from teatree.core.management.refusal_exit import RefusalExitTyperCommand
from teatree.core.merge.conflict_only import rebind_clearance_after_conflict_only_merge
from teatree.core.models import MergeClear, MRReviewLock, ReviewEvidence, ReviewEvidenceError, Ticket
from teatree.project import find_project_root
from teatree.utils.url_slug import pr_ref_from_url


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


# #4234: `record`, `record-evidence` and `lock-acquire` RETURN their refusal so a caller
# can route on it; the base class is what restores the exit code for the shell.
class Command(ReviewerPolicyCommands, MachineOutputCommand, RefusalExitTyperCommand):
    """Review verdicts, evidence, comments and the per-MR review lock."""

    @initialize()
    def init(self) -> None:
        """``t3 <overlay> review`` group root."""

    def _emit(self, payload: object, human: str, *, json_output: bool) -> None:
        """Route one handler's output through the machine-output seam (stdout stays pure JSON)."""
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — django-typer command: every param maps 1:1 to a ReviewVerdict field, the arg list IS the public CLI surface (same rationale as `ticket clear`).
        self,
        pr_id: int,
        slug: Annotated[
            str,
            typer.Argument(
                help="repo slug owner/repo (e.g. acme/widgets), NEVER a branch name — "
                "the merge verdict lookup keys by the resolved repo slug"
            ),
        ],
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
        lock_holder: Annotated[
            str,
            typer.Option(
                "--lock-holder",
                help="Lock identity the MRReviewLock is held under (the --holder passed to "
                "`review lock-acquire`), when you know it. Omit when you do not: the verdict "
                "releases the lock either way, since a concluded review must never strand one. "
                "Naming a DIFFERENT identity releases nothing.",
            ),
        ] = "",
        merge_result_retake: Annotated[
            bool,
            typer.Option(
                "--merge-result-retake",
                help="Attest that every finding citing a file outside the PR's changed-file set was "
                "re-measured on the materialised MERGE RESULT (`t3 review merge-tree`), not the branch "
                "checkout alone. Without it such a finding cannot carry blocking severity (#4251).",
            ),
        ] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the record result as JSON on stdout.")] = False,
    ) -> RecordResult:
        """Persist a cold-review verdict for a PR at an exact reviewed SHA.

        The durable sibling of ``ticket clear``: where a CLEAR authorises one
        merge, this records the *judgment* so ``review status`` can answer
        "safe to approve at the current head?" without a fresh cold review.
        Refuses the same way ``MergeClear.issue`` does (full-SHA bind, known
        verdict/blast/verify, non-empty reviewer, no merge_safe-on-red-checks),
        plus the #4251 diff-scope refusal: the PR's changed-file set is read from
        the forge here, and a blocking finding citing a file outside it needs
        ``--merge-result-retake``.
        """
        result, human = _review_impl.record_result(
            self,
            _review_impl.RecordRequest(
                pr_id=pr_id,
                slug=slug,
                reviewed_sha=reviewed_sha,
                verdict=verdict,
                reviewer_identity=reviewer_identity,
                gh_verify_result=gh_verify_result,
                blast_class=blast_class,
                findings_json=findings_json,
                ticket_id=ticket_id,
                lock_holder=lock_holder,
                merge_result_retake=merge_result_retake,
            ),
        )
        self._emit(result, human, json_output=json_output)
        return result

    @command(name="record-evidence")
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record_evidence(  # noqa: PLR0913 — django-typer command: every param maps 1:1 to a ReviewEvidence field / CLI flag.
        self,
        ticket_id: int,
        *,
        kind: Annotated[str, typer.Option(help="cold_review / integration_review.")] = "cold_review",
        reviewer: Annotated[
            str,
            typer.Option(
                "--reviewer",
                help="Reviewer identity: must carry a reviewer role word "
                "(reviewer/cold/cr/critic/adjudicator/checker/codex), never maker/coding/loop.",
            ),
        ] = "",
        verdict: Annotated[str, typer.Option(help="Review verdict, e.g. merge_safe / hold / pass.")] = "",
        head_sha: Annotated[
            str, typer.Option("--head-sha", help="Full 40-char hex commit id of the reviewed tree.")
        ] = "",
        repos: Annotated[
            str,
            typer.Option("--repos", help="Comma-separated repos covered (≥2 required for integration_review)."),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the evidence record as JSON.")] = False,
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
            self._emit(
                {"recorded": False, "error": str(exc)}, f"  record-evidence refused: {exc}", json_output=json_output
            )
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
            self._emit(
                {"recorded": False, "error": str(exc)}, f"  record-evidence refused: {exc}", json_output=json_output
            )
            return {"recorded": False, "error": str(exc)}

        result: RecordEvidenceResult = {
            "recorded": True,
            "evidence_id": int(evidence.pk),
            "ticket_id": ticket_id,
            "kind": evidence.kind,
        }
        self._emit(
            result, f"  recorded {evidence.kind} evidence {evidence.pk} for ticket {ticket_id}", json_output=json_output
        )
        return result

    @command()
    def status(
        self,
        mr_url: str,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the full status record as JSON.")] = False,
    ) -> StatusResult:
        """Report whether *mr_url* is safe to approve at its CURRENT head (read-only).

        Parses the PR/MR URL, fetches the live head SHA, looks up the latest
        recorded verdict, and reports one of: ``safe-to-approve``, ``stale``
        (head moved — re-review needed), ``head unreadable`` / ``checks
        unreadable`` (the forge did not answer; the recorded verdict stands, so
        retry the READ), or ``no recorded verdict``. The point is to avoid
        re-deriving a full cold review when a fresh verdict already vouches for
        the current tree. The record carries the verdict's ``findings`` so a
        HOLD can be read and acted on, not just counted. An unrenderable
        findings row degrades that portion and names the reason in
        ``findings_error`` rather than blocking the verdict; ``review
        findings`` stays the strict read.
        """
        result, human = _review_impl.status_result(self, mr_url)
        self._emit(result, human, json_output=json_output)
        return result

    @command()
    def findings(
        self,
        mr_url: str,
        *,
        reviewed_sha: Annotated[
            str,
            typer.Option("--sha", help="Read the verdict recorded at this exact SHA (default: the latest verdict)."),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the findings record as JSON.")] = False,
    ) -> FindingsResult:
        """Print the recorded findings for *mr_url* — the surface a HOLD is acted on through.

        A HOLD asserts that N things are wrong; this is where the author, a
        later reviewer, or an operator reads WHAT they are. A findings payload
        that cannot be rendered is a loud refusal, never a count with nothing
        behind it.
        """
        result, human = _review_impl.findings_result(self, mr_url, reviewed_sha)
        self._emit(result, human, json_output=json_output)
        return result

    @command(name="publish-findings")
    def publish_findings(
        self,
        mr_url: str,
        *,
        reviewed_sha: Annotated[
            str,
            typer.Option("--sha", help="Publish the verdict recorded at this exact SHA (default: the latest)."),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the publish result as JSON.")] = False,
    ) -> PublishFindingsResult:
        """Post a recorded verdict's findings to its PR, so the author sees them where the work is.

        ``review record`` already attempts this; run it here to retry after an
        on-behalf approval lands, or to backfill a verdict recorded before the
        publish path existed. Idempotent — a re-run finds its own comment and
        skips rather than posting a duplicate.
        """
        result, human = _review_impl.publish_findings_result(self, mr_url, reviewed_sha)
        self._emit(result, human, json_output=json_output)
        return result

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
        base_branch: Annotated[
            str,
            typer.Option(
                "--base-branch",
                help="The PR base branch the merge's second parent must descend from (default: repo default branch).",
            ),
        ] = "",
    ) -> RebindClearanceResult:
        """Re-bind a CLEAR to a conflict-only merge commit — no re-review (PR-07).

        After ``origin/main`` is merged into a reviewed branch to resolve conflicts
        (merge, never rebase — §17.4), the head moves and the SHA-bind gate refuses
        it. This re-binds ONLY when the merge commit's first parent is the reviewed
        SHA, its SECOND parent is a forge-verified ancestor of the PR base branch
        (never an arbitrary unreviewed branch), AND the commit is
        conflict-resolution-only; the original independent verdict is carried forward
        to the merge SHA, so the merge preconditions pass at the new head. A
        substantive merge, or one that merged in a non-base branch, is refused — a
        fresh review is required.
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
        rebound = rebind_clearance_after_conflict_only_merge(
            clear=clear, merge_sha=merge_sha, repo_root=root, base_branch=base_branch
        )
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
