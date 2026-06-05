"""Lifecycle and session phase operations."""

import logging
from typing import Annotated, TypedDict

import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command, initialize

from teatree.core.db_anchor import assert_lifecycle_db_is_canonical
from teatree.core.models import Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.merge_clear import is_non_reviewer_role
from teatree.core.phases import normalize_phase, phase_transition
from teatree.core.review_context_gate import ReviewContextError, check_review_context
from teatree.core.review_skill_gate import ReviewSkillEvidenceError, check_review_skill_evidence

logger = logging.getLogger(__name__)

__all__ = ["Command", "ReviewContextError", "ReviewSkillEvidenceError", "ReviewerAttestationError"]


class RecordE2ERunResult(TypedDict, total=False):
    """Result of ``lifecycle record-e2e-run`` (#1967)."""

    recorded: bool
    error: str
    ticket_id: int
    head_sha: str
    result: str
    posted_url: str


class ReviewerAttestationError(RuntimeError):
    """A ``reviewing`` phase visit was attempted without a valid reviewer identity."""


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="visit-phase")
    def visit_phase(
        self,
        ticket_id: str,
        phase: str,
        agent_id: Annotated[
            str,
            typer.Option(help="Recording agent identity stamped into phase_visits (audit trail)."),
        ] = "",
    ) -> str:
        """Mark a phase as visited and advance the ticket FSM if applicable.

        ``ticket_id`` accepts the same identifier set as ``pr create`` — DB
        pk, forge issue number, or full issue URL (#694). The phase is
        normalized to the canonical vocabulary so both the short verbs the
        skills emit (``code``, ``test``, ``review``, ``ship``, ``retro``,
        ``scope``) and the older gerunds advance the FSM. The resulting
        ``ticket.state`` is included in the output so a skipped or refused
        transition is visible rather than silently swallowed.

        ``--agent-id`` records the recording agent's identity into the
        ``phase_visits`` audit trail. Resolution is delegated to
        ``Session.recording_identity`` so the attribution is **never
        empty** even when neither ``--agent-id`` nor ``Session.agent_id``
        is set.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        # #779: refuse to record a phase into a worktree-isolated DB the
        # shipping gate never reads. Run BEFORE any write so the attestation
        # is never split from the DB `pr create` consults — symmetric across
        # maker (testing/retro) and reviewer (reviewing) visits.
        assert_lifecycle_db_is_canonical(ticket)
        canonical = normalize_phase(phase)
        # §17.6 enforcement candidate (13): a `reviewing` visit is the
        # independent cold-review attestation — it MUST carry an explicit
        # reviewer `--agent-id`, and that identity must not be a maker /
        # coding-agent / loop role (the author rubber-stamping their own
        # work). Never idempotent-silent: an existing `reviewing` key is
        # overwritten with the new reviewer + a loud log, never a silent
        # false-success.
        if canonical == "reviewing":
            _assert_reviewer_attestation(ticket, agent_id)
            # Gate C (#1539): when a review skill is configured, the reviewing
            # attestation must be backed by durable evidence the skill ran —
            # NO-OP when ``review_skill`` is unset (opt-in default preserved).
            check_review_skill_evidence(ticket)
            # Gate D: deep-retrieval evidence on ``reviewing`` when
            # ``require_review_context`` is on; NO-OP otherwise.
            check_review_context(ticket)
        # #801 SSOT: the canonical earliest+locked policy — never the
        # old -pk-latest pick nor a raw blank-agent_id create. The
        # explicit --agent-id seeds a created session's identity.
        session = ticket.resolve_phase_session(agent_id=agent_id or "loop")
        if canonical == "reviewing" and canonical in (session.phase_visits or {}):
            logger.warning(
                "Overwriting existing 'reviewing' attestation on session %s for ticket %s "
                "(was %s, now %s) — explicit re-review, not idempotent silent success",
                session.pk,
                ticket.pk,
                (session.phase_visits or {}).get("reviewing"),
                agent_id,
            )
            visits = dict(session.phase_visits or {})
            visits.pop("reviewing", None)
            session.phase_visits = visits
            session.save(update_fields=["phase_visits"])
        session.visit_phase(canonical, agent_id=session.recording_identity(agent_id))

        transition_name = phase_transition(canonical)
        if transition_name:
            _try_advance(ticket, transition_name)

        return f"Phase '{canonical}' marked as visited on session {session.pk} (ticket state: {ticket.state})"

    @command(name="clear-ledger")
    def clear_ledger(
        self,
        ticket_id: str,
        *,
        confirm: Annotated[
            bool,
            typer.Option(help="Required: confirm the destructive phase-ledger clear."),
        ] = False,
    ) -> str:
        """Clear a reused ticket's stale phase ledger (sanctioned session-retire).

        §17.6 enforcement candidate (9): reused tickets accumulate a stale
        phase ledger from a prior workstream — the shipping gate then sees a
        passing aggregate that no longer reflects the new work (the
        anti-vacuous attestation gap). Hand-editing ``phase_visits`` /
        ``visited_phases`` was the only escape, which is exactly the
        out-of-band state mutation invariant 8 prohibits. This is the
        sanctioned ``t3`` path: it retires every session's phase ledger for
        the ticket in one transaction so the next workstream re-earns its
        attestations from scratch. Requires ``--confirm`` (destructive).
        """
        ticket = Ticket.objects.resolve(ticket_id)
        assert_lifecycle_db_is_canonical(ticket)
        if not confirm:
            return (
                f"Refusing to clear ticket {ticket.pk}'s phase ledger without --confirm "
                f"(destructive: every session's visited_phases/phase_visits is wiped)"
            )
        # #1286: delegate to the canonical ``Ticket._retire_phase_ledger``
        # helper so the CLI and the ``reopen()`` FSM workstream-boundary
        # call retire the ledger the same way. One source of truth, no
        # drift if the retire policy ever has to learn a new column.
        cleared = ticket.sessions.count()
        ticket._retire_phase_ledger()  # noqa: SLF001
        logger.warning(
            "Phase ledger cleared for ticket %s across %d session(s) — sanctioned session-retire",
            ticket.pk,
            cleared,
        )
        return f"Cleared phase ledger for ticket {ticket.pk} across {cleared} session(s)"

    @command(name="record-review-skill-run")
    def record_review_skill_run(self, ticket_id: str, skill: str) -> str:
        """Record durable evidence that the deep-review ``skill`` ran (#1539).

        Stamps ``ticket.extra['review_skill_run']`` (skill name + UTC ISO
        timestamp) so the reviewing-phase gate can attest that the configured
        ``review_skill`` actually executed before ``visit-phase ... reviewing``
        records the attestation.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        assert_lifecycle_db_is_canonical(ticket)
        ticket.record_review_skill_run(skill)
        return f"Recorded review-skill run {skill!r} for ticket {ticket.pk}"

    @command(name="record-review-context")
    def record_review_context(
        self,
        ticket_id: str,
        work_item: Annotated[
            str,
            typer.Option(help="The work item / ticket URL fetched from its source (Notion / GitLab / tracker)."),
        ] = "",
        documents: Annotated[
            str,
            typer.Option(help="Comma-separated referenced documents downloaded and read (spec, design doc, schedule)."),
        ] = "",
        analysis: Annotated[
            str,
            typer.Option(help="How the implementation was analyzed against the specified requirements + rules."),
        ] = "",
    ) -> str:
        """Record durable evidence the referenced context was retrieved + analyzed.

        Reviewing carries the same responsibility as implementing: this stamps
        ``ticket.extra['review_context']`` so the ``-> reviewing`` deep-retrieval
        gate can attest the work item was fetched from its source, its links
        followed, and each referenced document downloaded + analyzed against the
        diff before ``visit-phase ... reviewing`` records the attestation. A
        record missing the work item, any document, or the analysis does not
        satisfy the gate.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        assert_lifecycle_db_is_canonical(ticket)
        document_list = [doc.strip() for doc in documents.split(",") if doc.strip()]
        if not work_item.strip() or not document_list or not analysis.strip():
            return (
                f"record-review-context refused for ticket {ticket.pk}: --work-item, at least one "
                f"--documents entry, and --analysis are all required (a partial record never satisfies "
                f"the deep-retrieval gate)"
            )
        ticket.record_review_context(work_item, document_list, analysis)
        return f"Recorded review context for ticket {ticket.pk} ({len(document_list)} document(s))"

    @command(name="record-e2e-run")
    def record_e2e_run(
        self,
        ticket_id: str,
        *,
        spec: Annotated[str, typer.Option(help="Path to the E2E spec that ran.")] = "",
        result: Annotated[str, typer.Option(help="Run result: green or red.")] = "green",
        head_sha: Annotated[
            str,
            typer.Option("--head-sha", help="Full 40-char hex SHA of the reviewed tree the run executed against."),
        ] = "",
        posted_url: Annotated[
            str,
            typer.Option(
                "--posted-url",
                help="URL of the posted `e2e post-evidence` comment; required for a green run to satisfy the gate.",
            ),
        ] = "",
    ) -> "RecordE2ERunResult":
        """Record SHA-bound, POSTED E2E evidence for the mandatory-E2E gate (#1967).

        Writes an :class:`~teatree.core.models.e2e_mandatory_run.E2eMandatoryRun`
        for the ticket at ``--head-sha``. A green run satisfies the mandatory-E2E
        gate at ``pr create`` and the §17.4 CLEAR **only when** ``--posted-url``
        is given — recorded E2E evidence is not enough, it must be POSTED (the
        SHA-bound ``e2e post-evidence`` ticket comment). A green run at any other
        SHA does not carry. Re-recording the same spec at the same tree updates
        the row in place (idempotent). A red run, or a green run with no
        ``--posted-url``, records provenance without satisfying the gate.
        """
        from teatree.core.models.e2e_mandatory_run import E2eMandatoryRun  # noqa: PLC0415
        from teatree.core.models.merge_clear import is_commit_sha  # noqa: PLC0415

        ticket = Ticket.objects.resolve(ticket_id)
        assert_lifecycle_db_is_canonical(ticket)
        if not spec.strip():
            self.stderr.write("  record-e2e-run refused: --spec is required (the E2E spec path that ran).")
            return {"recorded": False, "error": "--spec is required"}
        if not is_commit_sha(head_sha):
            self.stderr.write(
                "  record-e2e-run refused: --head-sha must be a full 40-char hex SHA of the reviewed tree."
            )
            return {"recorded": False, "error": "--head-sha must be a full 40-char hex SHA"}
        run = E2eMandatoryRun.record(ticket=ticket, head_sha=head_sha, spec=spec, result=result, posted_url=posted_url)
        posted_note = "" if run.posted_url else " (UNPOSTED — does not satisfy the gate until --posted-url is set)"
        self.stdout.write(
            f"  recorded E2E run ({run.result}) for ticket {ticket.pk} @ {run.head_sha[:8]} ({run.spec}){posted_note}"
        )
        return {
            "recorded": True,
            "ticket_id": int(ticket.pk),
            "head_sha": run.head_sha,
            "result": run.result,
            "posted_url": run.posted_url,
        }

    @command(name="record-anti-vacuity")
    def record_anti_vacuity(
        self,
        ticket_id: str,
        *,
        head_sha: Annotated[
            str,
            typer.Option(help="Full 40-char head SHA the attestation binds to (re-attest when it moves)."),
        ] = "",
        ac_coverage: Annotated[
            str,
            typer.Option(help="How the diff was mapped against the ticket/spec acceptance criteria."),
        ] = "",
        proven_test: Annotated[
            list[str] | None,
            typer.Option(help="A new regression test proven anti-vacuous (revert fix -> RED). Repeatable."),
        ] = None,
        no_new_tests: Annotated[
            bool,
            typer.Option(help="The diff genuinely adds no new regression test (so --proven-test is empty)."),
        ] = False,
    ) -> str:
        """Record the SHA-bound anti-vacuity attestation backing review-request/merge (#1829).

        Stamps ``ticket.extra['anti_vacuity_attestation']`` so the anti-vacuity
        gate (``teatree.core.anti_vacuity_gate``) can attest, before the
        ``request review`` / merge transition, that the diff was mapped to the
        acceptance criteria AND every new regression test was proven
        anti-vacuous (revert the production fix -> the test goes RED). The
        attestation binds to ``--head-sha``; the gate drops it when the live
        head moves. A record missing the head SHA, AC-coverage, or (a proven
        test OR ``--no-new-tests``) does not satisfy the gate.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        assert_lifecycle_db_is_canonical(ticket)
        proven = [test.strip() for test in (proven_test or []) if test.strip()]
        if not head_sha.strip() or not ac_coverage.strip() or (not proven and not no_new_tests):
            return (
                f"record-anti-vacuity refused for ticket {ticket.pk}: --head-sha and --ac-coverage are "
                f"required, plus at least one --proven-test OR --no-new-tests (a partial record never "
                f"satisfies the anti-vacuity gate)"
            )
        ticket.record_anti_vacuity_attestation(head_sha, ac_coverage, proven, no_new_tests=no_new_tests)
        proven_summary = f"{len(proven)} proven test(s)" if proven else "no new tests"
        return f"Recorded anti-vacuity attestation for ticket {ticket.pk} ({proven_summary})"


def _assert_reviewer_attestation(ticket: Ticket, agent_id: str) -> None:
    """Refuse a ``reviewing`` visit without an explicit, non-maker reviewer id.

    §17.6 enforcement candidate (13): the reviewing attestation is the
    independent cold-review signal. An empty ``--agent-id`` (it would fall
    back to the session's own maker identity) or a maker/coding-agent/loop
    role recording it is the author attesting their own review — refused.
    """
    explicit = agent_id.strip()
    if not explicit:
        msg = (
            f"`lifecycle visit-phase {ticket.pk} reviewing` requires an explicit "
            f"--agent-id naming the independent reviewer (§17.6 candidate 13); "
            f"an empty id would fall back to the maker session identity"
        )
        raise ReviewerAttestationError(msg)
    if is_non_reviewer_role(explicit):
        msg = (
            f"--agent-id {explicit!r} is a maker/coding-agent/loop role — a `reviewing` "
            f"attestation must be recorded by an independent reviewer, not the author "
            f"(§17.6 candidate 13 / §17.8 clause 3)"
        )
        raise ReviewerAttestationError(msg)


def _try_advance(ticket: Ticket, transition_name: str) -> None:
    # ``phase_transition`` only ever returns the name of a real ``Ticket``
    # FSM transition, so ``getattr`` always resolves here.
    method = getattr(ticket, transition_name)
    try:
        with transaction.atomic():
            method()
            ticket.save()
    except (TransitionNotAllowed, InvalidTransitionError) as exc:
        # Loud, not swallowed (#694): an out-of-order / skipped transition
        # used to vanish at DEBUG and only resurface as a raw
        # TransitionNotAllowed at `pr create`. The phase visit is still
        # recorded; the shipping gate reconciles the FSM from it later.
        # InvalidTransitionError (dirty-worktree / missing-E2E DoD refusals)
        # is handled identically: the FSM stays put, so the gate keeps
        # blocking, but the operator sees the refusal reason instead of a
        # raw traceback.
        logger.warning(
            "Transition '%s' not valid from state '%s' for ticket %s — "
            "FSM unchanged; phase visit recorded, gate will reconcile (%s)",
            transition_name,
            ticket.state,
            ticket.pk,
            exc,
        )
