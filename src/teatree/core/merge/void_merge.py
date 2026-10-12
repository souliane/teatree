"""Void a forge merge whose content never reached the default branch: a read-only plan, then one approved write.

Human-only by construction: the operator checks refuse the loop runner and any process a
dispatched agent spawned, the write needs a real terminal answering yes, and the model
mutators refuse without the :class:`MergeVoid` row this module alone creates.
"""

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, TextIO

from django.db import transaction

from teatree.core.backend_protocols import BackendResolutionError
from teatree.core.forge_content import ContentVerdict, ContentVerdictKind, PrContentSnapshot, classify_content
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.pr_slug_resolution import resolved_repo_slug
from teatree.core.models import MergeAudit, MergeClear, MergeVoid, PullRequest, Ticket
from teatree.core.models.merge_void import (
    MergeVoidFields,
    PullRequestClose,
    TicketKept,
    TicketRestore,
    VoidedRefs,
    VoidEvidence,
)
from teatree.core.models.reviewer_identity import REVIEWER_ROLE_COMPONENTS, is_non_reviewer_role
from teatree.core.session_identity import is_loop_runner_session, loop_principal
from teatree.url_classify import pr_ref
from teatree.utils.agent_spawn_marker import is_dispatched_agent_process
from teatree.utils.approval import ApprovalRefusedError, require_interactive_approval
from teatree.utils.pr_ref import PrRef
from teatree.utils.run import TimeoutExpired

if TYPE_CHECKING:
    from django.db.models import QuerySet

_FORGE_READ_FAILURES = (
    BackendResolutionError,
    LookupError,
    OSError,
    RuntimeError,
    ValueError,
    TimeoutExpired,
)
_AGENT_WORDS = REVIEWER_ROLE_COMPONENTS | {
    "agent",
    "bot",
    "claude",
    "debugger",
    "factory",
    "orchestrator",
    "planner",
    "shipper",
    "t3",
    "teatree",
    "tester",
    "worker",
}


class VoidRefusal(StrEnum):
    NOT_A_PR_URL = "not_a_pr_url"
    REASON_MISSING = "reason_missing"
    ACTOR_NOT_HUMAN = "actor_not_human"
    UNATTENDED = "unattended"
    AGENT_PROCESS = "agent_process"
    ALREADY_VOIDED = "already_voided"
    FORGE_UNREADABLE = "forge_unreadable"
    FORGE_NOT_MERGED = "forge_not_merged"
    CONTENT_ON_DEFAULT = "content_on_default"
    CONTENT_CARRIED_BY_MERGE = "content_carried_by_merge"
    NO_MERGE_EDGE = "no_merge_edge"
    NOTHING_TO_VOID = "nothing_to_void"
    NO_TERMINAL = "no_terminal"
    DECLINED = "declined"
    PLAN_CHANGED = "plan_changed"


class VoidRefusedError(Exception):
    def __init__(self, code: VoidRefusal, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_VERDICT_REFUSALS = {
    ContentVerdictKind.UNKNOWN: VoidRefusal.FORGE_UNREADABLE,
    ContentVerdictKind.NOT_MERGED: VoidRefusal.FORGE_NOT_MERGED,
    ContentVerdictKind.PRESENT_AT_TIP: VoidRefusal.CONTENT_ON_DEFAULT,
    ContentVerdictKind.CARRIED_BY_MERGE: VoidRefusal.CONTENT_CARRIED_BY_MERGE,
}


@dataclass(frozen=True, slots=True)
class VoidPlan:
    ref: PrRef
    pr_url: str
    snapshot: PrContentSnapshot = field(default_factory=PrContentSnapshot)
    verdict: ContentVerdict = field(default_factory=lambda: ContentVerdict(ContentVerdictKind.UNKNOWN))
    restores: tuple[TicketRestore, ...] = ()
    kept: tuple[TicketKept, ...] = ()
    closes: tuple[PullRequestClose, ...] = ()
    open_clears: tuple[int, ...] = ()
    refusal: VoidRefusedError | None = None

    def fingerprint(self) -> tuple[object, ...]:
        facts = (self.snapshot.merge_commit, self.snapshot.content_head, self.snapshot.default_tip, self.verdict.kind)
        return (*facts, self.restores, self.kept, self.closes)

    def evidence(self) -> VoidEvidence:
        return VoidEvidence(
            tickets=list(self.restores),
            kept=list(self.kept),
            pull_requests=list(self.closes),
            open_clears=list(self.open_clears),
        )

    def fields(self, *, reason: str, actor: str) -> MergeVoidFields:
        snapshot = self.snapshot
        return MergeVoidFields(
            host_kind=self.ref.host_kind,
            slug=self.ref.slug,
            pr_id=self.ref.pr_id,
            pr_url=self.pr_url,
            merged_sha=snapshot.merge_commit,
            content_head=snapshot.content_head,
            merge_base=snapshot.merge_base,
            default_branch=snapshot.default_branch,
            default_tip=snapshot.default_tip,
            changed_paths=list(self.verdict.changed),
            evidence=self.evidence(),
            reason=reason,
            actor=actor,
        )

    def describe(self) -> list[str]:
        snapshot, verdict = self.snapshot, self.verdict
        absent = len(verdict.changed) - len(verdict.present_at_tip)
        lines = [
            f"Void the merge of {self.pr_url}",
            (
                f"  merge commit {snapshot.merge_commit[:8] or '?'}, content head {snapshot.content_head[:8] or '?'}, "
                f"default {snapshot.default_branch or '?'}@{snapshot.default_tip[:8] or '?'}"
            ),
            f"  verdict {verdict.kind}: {absent} of {len(verdict.changed)} paths absent ({verdict.reason})",
        ]
        lines += [
            f"  pull request row {c['pull_request_id']}: {c['from_state']} -> {c['to_state']}" for c in self.closes
        ]
        lines += [f"  ticket {r['ticket_id']}: {r['from_state']} -> {r['to_state']}" for r in self.restores]
        lines += [f"  ticket {k['ticket_id']} kept merged: {k['reason']}" for k in self.kept]
        if self.open_clears:
            lines.append(f"  unconsumed clears left as they are: {', '.join(map(str, self.open_clears))}")
        if self.refusal is not None:
            lines.append(f"  REFUSED {self.refusal}")
        return lines


def check_operator(*, reason: str, actor: str) -> None:
    if not reason.strip():
        raise VoidRefusedError(VoidRefusal.REASON_MISSING, "--reason must say why the recorded merge is void")
    words = {word for word in re.split(r"[^a-z0-9]+", actor.casefold()) if word}
    if not words or is_non_reviewer_role(actor) or words & _AGENT_WORDS:
        raise VoidRefusedError(VoidRefusal.ACTOR_NOT_HUMAN, f"--by {actor!r} must name the human operator")
    if is_loop_runner_session(loop_principal()[0]):
        raise VoidRefusedError(VoidRefusal.UNATTENDED, "the loop runner may not void a merge")
    if is_dispatched_agent_process():
        raise VoidRefusedError(VoidRefusal.AGENT_PROCESS, "a process a dispatched agent spawned may not void a merge")


def plan_void(pr_url: str) -> VoidPlan:
    ref = pr_ref(pr_url.strip())
    if ref is None:
        raise VoidRefusedError(VoidRefusal.NOT_A_PR_URL, f"{pr_url!r} names no pull or merge request")
    if MergeVoid.objects.for_pr(slug=ref.slug, pr_id=ref.pr_id, host_kind=ref.host_kind).exists():
        raise VoidRefusedError(VoidRefusal.ALREADY_VOIDED, f"the merge of {pr_url} is already voided")
    try:
        snapshot = CodeHostQuery.for_ref(ref).pr_content_snapshot()
    except _FORGE_READ_FAILURES as exc:
        snapshot = PrContentSnapshot(unreadable=f"forge unreachable: {exc}")
    verdict = classify_content(snapshot)
    voided = MergeVoid.objects.voided_refs()
    restores: list[TicketRestore] = []
    kept: list[TicketKept] = []
    edgeless: list[int] = []
    for ticket in _candidate_tickets(ref):
        if other := _other_merge_evidence(ticket, ref, voided):
            kept.append(TicketKept(ticket_id=ticket.pk, reason=f"still evidenced by {other}"))
        elif (entry := ticket.transitions.merge_entry()) is None:
            edgeless.append(ticket.pk)
        else:
            restores.append(TicketRestore(ticket_id=ticket.pk, from_state=ticket.state, to_state=entry.from_state))
    closes = tuple(
        PullRequestClose(pull_request_id=row.pk, from_state=row.state, to_state=PullRequest.State.CLOSED)
        for row in _rows(ref).filter(state=PullRequest.State.MERGED).order_by("pk")
    )
    plan = VoidPlan(
        ref=ref,
        pr_url=pr_url.strip(),
        snapshot=snapshot,
        verdict=verdict,
        restores=tuple(restores),
        kept=tuple(kept),
        closes=closes,
        open_clears=tuple(
            MergeClear.objects.filter(pr_id=ref.pr_id, slug__iexact=ref.slug, consumed_at__isnull=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        ),
    )
    return replace(plan, refusal=_refusal(plan, edgeless))


def apply_void(plan: VoidPlan, *, reason: str, actor: str, stdin: TextIO, stdout: TextIO) -> MergeVoid:
    check_operator(reason=reason, actor=actor)
    if plan.refusal is not None:
        raise plan.refusal
    try:
        require_interactive_approval("\n".join(plan.describe()), stdin=stdin, stdout=stdout)
    except ApprovalRefusedError as exc:
        code = VoidRefusal.DECLINED if stdin.isatty() and stdout.isatty() else VoidRefusal.NO_TERMINAL
        raise VoidRefusedError(code, str(exc)) from exc
    fresh = plan_void(plan.pr_url)
    if fresh.refusal is not None:
        raise fresh.refusal
    if fresh.fingerprint() != plan.fingerprint():
        raise VoidRefusedError(VoidRefusal.PLAN_CHANGED, "the forge or the ledger moved since the plan; plan again")
    return _write(fresh, reason=reason.strip(), actor=actor.strip())


@transaction.atomic
def _write(plan: VoidPlan, *, reason: str, actor: str) -> MergeVoid:
    tickets = list(Ticket.objects.filter(pk__in=[restore["ticket_id"] for restore in plan.restores]).order_by("pk"))
    void = MergeVoid.objects.record(plan.fields(reason=reason, actor=actor), tickets=tickets)
    for row in PullRequest.objects.filter(pk__in=[close["pull_request_id"] for close in plan.closes]):
        row.void_merge()
        row.save(update_fields=["state"])
    for ticket in tickets:
        ticket.void_merge(void)
        ticket.save(update_fields=["state"])
    return void


def _refusal(plan: VoidPlan, edgeless: list[int]) -> VoidRefusedError | None:
    if code := _VERDICT_REFUSALS.get(plan.verdict.kind):
        return VoidRefusedError(code, plan.verdict.reason)
    if edgeless:
        ids = ", ".join(map(str, edgeless))
        return VoidRefusedError(
            VoidRefusal.NO_MERGE_EDGE, f"no recorded edge into merged to restore for ticket(s) {ids}"
        )
    if not plan.restores and not plan.closes:
        return VoidRefusedError(VoidRefusal.NOTHING_TO_VOID, "no merged pull-request row or restorable ticket names it")
    return None


def _names(ref: PrRef, *, slug: str, pr_id: object, host_kind: str = "") -> bool:
    return (
        slug.strip().casefold() == ref.slug.casefold()
        and pr_id == ref.pr_id
        and (not host_kind or host_kind == ref.host_kind)
    )


def _rows(ref: PrRef) -> "QuerySet[PullRequest]":
    return PullRequest.objects.for_pr(slug=ref.slug, pr_id=ref.pr_id)


def _audit_ref(audit: MergeAudit) -> tuple[str, int, str]:
    clear = audit.clear
    return audit.repo_slug or resolved_repo_slug(clear) or clear.slug, clear.pr_id, clear.host_kind


def _candidate_tickets(ref: PrRef) -> list[Ticket]:
    """Merged-or-past tickets naming the PR by row, issue url, recorded forge evidence, or a merge audit."""
    ids = set(_rows(ref).values_list("ticket_id", flat=True))
    for audit in MergeAudit.objects.filter(clear__pr_id=ref.pr_id, clear__ticket__isnull=False).select_related(
        "clear__ticket"
    ):
        slug, pr_id, host_kind = _audit_ref(audit)
        if _names(ref, slug=slug, pr_id=pr_id, host_kind=host_kind):
            ids.add(audit.clear.ticket_id)
    candidates = []
    for ticket in Ticket.objects.filter(state__in=Ticket.merged_states()).order_by("pk"):
        own = pr_ref(ticket.issue_url or "")
        recorded = (ticket.extra or {}).get("forge_merge_evidence")
        if (
            ticket.pk in ids
            or (own is not None and _names(ref, slug=own.slug, pr_id=own.pr_id, host_kind=own.host_kind))
            or (
                isinstance(recorded, dict)
                and _names(ref, slug=str(recorded.get("slug", "")), pr_id=recorded.get("pr_id"))
            )
        ):
            candidates.append(ticket)
    return candidates


def _other_merge_evidence(ticket: Ticket, ref: PrRef, voided: VoidedRefs) -> str:
    """The first evidence that *ticket* merged through a PR other than *ref* and not voided, else ``""``."""
    evidence = [_audit_ref(audit) for audit in MergeAudit.objects.filter(clear__ticket=ticket).select_related("clear")]
    recorded = (ticket.extra or {}).get("forge_merge_evidence")
    if isinstance(recorded, dict):
        evidence.append((str(recorded.get("slug", "")), recorded.get("pr_id"), ""))
    for row in PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED):
        row_ref = pr_ref(row.url)
        evidence.append((row.repo, row_ref.pr_id if row_ref else row.iid, row_ref.host_kind if row_ref else ""))
    for slug, pr_id, host_kind in evidence:
        if _names(ref, slug=slug, pr_id=pr_id, host_kind=host_kind):
            continue
        if isinstance(pr_id, int) and voided.covers(slug=slug, pr_id=pr_id, host_kind=host_kind):
            continue
        return f"{slug}#{pr_id}"
    return ""
