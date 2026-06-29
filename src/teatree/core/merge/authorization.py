"""§17.4.3 identity/substrate authorization guards + the anti-vacuity wrapper.

The result type :class:`MergePrecheck` and the guard functions
:func:`_assert_clear_authorized` / :func:`_assert_anti_vacuity` that
:mod:`execution` runs before it binds the irreversible merge.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.author_trust import classify_author
from teatree.core.merge.ci_rollup import fetch_pr_author
from teatree.core.merge.errors import MergePreconditionError

if TYPE_CHECKING:
    from teatree.core.models import MergeClear


@dataclass(frozen=True, slots=True)
class MergePrecheck:
    """Outcome of :func:`assert_merge_preconditions`.

    ``verified_sha`` is the SHA the merge binds to (``expected_head_oid``).
    ``already_merged_sha`` is non-empty only when the §928 reconciliation
    fired: GitHub reports the PR already MERGED at the exact reviewed tree
    (a lost post-hook), so the irreversible merge must be SKIPPED and the
    post hook run idempotently against the existing merge commit.
    """

    verified_sha: str
    already_merged_sha: str = ""

    @property
    def needs_reconcile(self) -> bool:
        return bool(self.already_merged_sha)


def _assert_clear_authorized(
    *,
    clear: object,
    executing_loop_identity: str,
    slug: str,
    pr_id: int,
    human_authorized: str,
) -> "MergeClear":
    """The §17.4.3 identity/substrate authorization guards (steps 1 + 5).

    Split out of :func:`assert_merge_preconditions` so the orchestration
    there reads as the ordered §17.4.3 sequence (authorize → SHA →
    reconcile → draft → checks) rather than one deeply-branching block.
    Raises :class:`MergePreconditionError` on the first failed guard;
    returns the narrowed :class:`MergeClear` on success.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415
    from teatree.core.models.merge_clear import is_non_reviewer_role  # noqa: PLC0415

    if not isinstance(clear, MergeClear):
        msg = f"no MergeClear row for {slug}#{pr_id} — refusing to merge (§17.4.3 step 1)"
        raise MergePreconditionError(msg)

    # 1. CLEAR exists, all fields populated, unconsumed.
    if not clear.is_actionable():
        msg = (
            f"MergeClear for {slug}#{pr_id} is not actionable (missing fields or already "
            f"consumed) — treated as absent (§17.4.2/§17.4.3 step 1)"
        )
        raise MergePreconditionError(msg)

    # The recorded reviewer verdict must be merge-safe. ``MergeClear.issue()``
    # rejects a non-green verdict at issue time, but a row written directly via
    # ``.objects.create()`` (fixture / migration / non-factory ORM path) could
    # smuggle a HOLD (pending/failed) verdict past it. Re-check here so the
    # live-CI re-check below can never stamp green over the reviewer's recorded
    # HOLD when CI self-flips green — the green-over-HOLD class (§17.8 clause 3:
    # the checker's recorded verdict is authoritative, mirroring the
    # ``is_non_reviewer_role`` issue/merge double-guard above).
    if clear.gh_verify_result != clear.VerifyResult.GREEN:
        msg = (
            f"MergeClear for {slug}#{pr_id} records gh_verify_result "
            f"({clear.gh_verify_result!r}), not green — the reviewer recorded a HOLD at the "
            f"reviewed tree; a non-green verdict can never authorize a merge regardless of the "
            f"live CI rollup (§17.4.2 / §17.8 clause 3)"
        )
        raise MergePreconditionError(msg)

    # Independent cold-review CLEAR: the reviewer identity must be distinct
    # from the executing loop (§17.8 clause 3 — the loop cannot rubber-stamp
    # its own CLEAR).
    if clear.reviewer_identity.strip() == executing_loop_identity.strip():
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) equals the "
            f"executing loop identity — a CLEAR must be issued by an independent "
            f"cold reviewer, not self-issued (§17.8 clause 3)"
        )
        raise MergePreconditionError(msg)

    # The factory ``MergeClear.issue()`` rejects a maker/coding-agent/loop
    # reviewer_identity at issue time (§17.8 clause 3 — the same shared
    # ``is_non_reviewer_role`` helper), but a row written directly via
    # ``.objects.create()`` (fixture, migration, or any non-factory ORM
    # path — e.g. ``ticket.py`` loads the row by pk without re-validation)
    # would otherwise smuggle a self-attesting maker through the equality
    # check above. Re-check the same role classification here so the
    # issue-time and merge-time gates cannot drift apart (codex #1282
    # finding 1 / #1283).
    if is_non_reviewer_role(clear.reviewer_identity):
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) is a "
            f"maker/coding-agent/loop non-reviewer role — a CLEAR must be issued "
            f"by an independent cold reviewer, not self-attested (§17.8 clause 3)"
        )
        raise MergePreconditionError(msg)

    # The human-substrate escape is substrate-only. Presenting it against a
    # non-substrate CLEAR is refused outright so the path can never be used to
    # short-circuit independent loop review of a logic/docs PR (the loop is
    # the reviewer-of-record for those — invariant 8 / §17.4.1).
    presented = human_authorized.strip()
    if presented and not clear.is_substrate():
        msg = (
            f"--human-authorized presented for non-substrate MergeClear "
            f"({slug}#{pr_id}, blast_class={clear.blast_class}); the recorded-human-"
            f"approval path is substrate-only — a logic/docs CLEAR merges through "
            f"the loop, not via a human-approval escape hatch (invariant 8 / §17.4.1)"
        )
        raise MergePreconditionError(msg)

    # 5. blast_class respected — substrate-class PRs are draft-locked and require
    #    a recorded PER-PR human sign-off (invariant 4 / §17.4.3 step 5). Substrate
    #    is NEVER covered by the overlay's standing grant — not even at
    #    ``autonomy = full``: the owner's directive is that substrate (merge
    #    keystone, architecture spec, governance doc) PINGS-and-HOLDS so they
    #    authorize every such merge. ``_overlay_grants_standing_substrate_signoff``
    #    therefore returns ``False`` for any substrate clear (the explicit gate
    #    that the standing grant excludes substrate), so the ONLY thing that unlocks
    #    a substrate merge here is a per-CLEAR ``human_authorizer`` matching the
    #    value re-presented at merge time. When unsatisfied the held clear raises
    #    below, which the loop edge routes to the substrate-hold Slack ping. The
    #    AGENT still executes the authorized merge through this same SHA-bound,
    #    audited transition (invariant 8). The quality/safety floor (independent
    #    cold-review, reviewed-SHA bind, CI-green, not-draft, never-lockout, privacy
    #    scan) is untouched. NON-substrate changes self-merge unchanged.
    if (
        clear.is_substrate()
        and not clear.human_merge_authorized_by(presented)
        and not _overlay_grants_standing_substrate_signoff(clear, resolved_slug=slug)
    ):
        detail = (
            "no human authoriser recorded on the CLEAR — substrate is held for the owner, never auto-merged"
            if not clear.human_authorizer
            else f"presented authoriser != recorded ({clear.human_authorizer!r})"
            if presented
            else "no --human-authorized presented at merge time"
        )
        msg = (
            f"MergeClear for {slug}#{pr_id} is blast_class=substrate — substrate "
            f"changes are held for the owner and are draft-locked (invariant 4); the "
            f"loop never auto-merges them, not even at autonomy=full (§17.4.3 step 5). "
            f"{detail.capitalize()}. The sanctioned path: issue `t3 <overlay> ticket "
            f"clear … --blast-class substrate --human-authorize <id>` (a per-PR "
            f"recorded approval), then the agent executes `t3 <overlay> ticket "
            f"merge <clear_id> --human-authorized <id>`"
        )
        raise MergePreconditionError(msg)

    return clear


def _resolve_clear_overlay_name(clear: "MergeClear", *, resolved_slug: str = "") -> str:
    """The overlay name to resolve autonomy against for *clear* — by REPO IDENTITY.

    The merge-approval gate is a property of the **repo**, not of whatever
    overlay token a ticket happens to carry. A repo's OWNING overlay
    (:func:`infer_overlay_for_url` over every overlay's ``get_workspace_repos``)
    is authoritative — a repo resolves to its OWNING overlay even when the
    linked ticket was mis-stamped with a different overlay at creation.
    Resolving the stored ``ticket.overlay`` first inverted this: a ticket
    created while the agent was typed as a *different* overlay (the
    ``T3_OVERLAY_NAME`` the CLI bridge stamps, or a loop scanner setting
    ``ticket.overlay = self.overlay_name``) carried the WRONG overlay, so a PR
    on a repo governed by a ``full`` overlay was evaluated under a below-full
    overlay and refused. This is the name-collision trap: two overlays can carry
    similar names while owning disjoint repo sets — the repo's owning overlay,
    not the typed token, decides the gate.

    Resolution order, first non-empty wins:

    1.  :func:`infer_overlay_for_url` on the CLEAR's stored ``slug`` — the repo's
        OWNING overlay, authoritative. Resolves only when the stored slug is an
        ``owner/repo`` (the merge-authorization path stores ``owner/repo``).
    2.  :func:`infer_overlay_for_url` on *resolved_slug* — the real
        ``owner/repo`` the merge keystone recovered for this CLEAR
        (:func:`resolve_pr_repo_slug` →
        :func:`_reconcile_slug_against_reviewed_sha`). The loop routinely
        issues a ticket-less substrate CLEAR whose stored ``slug`` is a *branch
        name* (``merge-candidate-working-repos``), not ``owner/repo`` — step 1
        returns ``""`` for it, so this step resolves the SAME repo the bound
        merge targets.
    3.  ``clear.ticket.overlay`` — the stored token, the LAST resort. Used only
        when repo identity is inconclusive (no overlay claims either slug, e.g.
        a repo not yet declared in any overlay's ``workspace_repos``), so an
        existing attribution is never discarded for a blank inference.

    Returns ``""`` when no source resolves an overlay (the fail-closed default).
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    from_stored = infer_overlay_for_url(str(getattr(clear, "slug", "") or "")).strip()
    if from_stored:
        return from_stored
    from_recovered = infer_overlay_for_url(resolved_slug.strip()).strip()
    if from_recovered:
        return from_recovered
    return str(getattr(getattr(clear, "ticket", None), "overlay", "") or "").strip()


def _overlay_grants_standing_substrate_signoff(clear: "MergeClear", *, resolved_slug: str = "") -> bool:
    """Whether the overlay's standing grant covers this per-PR sign-off (invariant 4 carve-out).

    A SUBSTRATE clear is NEVER covered — it returns ``False`` immediately. The
    owner's directive is that substrate must PING-and-HOLD, never auto-merge: a
    substrate change (merge keystone, architecture spec, governance doc) is the
    one class the owner sees and authorizes every time, so even at
    ``autonomy = full`` the standing grant does not remove its per-PR human
    sign-off. The held substrate CLEAR raises the same MergePreconditionError,
    which the loop edge routes to the substrate-hold Slack ping.

    The remaining resolution (the standing grant for a NON-substrate clear —
    ``autonomy = full`` OR an explicit ``require_human_approval_to_merge = false``
    on a non-collaborative tier, the ``notify`` tier excluded, #2666) is retained
    so the gate stays a single named contract; non-substrate clears do not reach
    this function from the substrate-only call site, so for them it is moot.

    *resolved_slug* is the real ``owner/repo`` the merge keystone recovered for
    this CLEAR (threaded from :func:`assert_merge_preconditions`'s ``slug``
    kwarg) — see :func:`_resolve_clear_overlay_name`.
    """
    from teatree.config import Autonomy, get_effective_settings  # noqa: PLC0415

    # Substrate is excluded from the standing grant entirely (the §3.2 gate):
    # substrate PINGS-and-HOLDS for the owner, so the standing grant never removes
    # its per-PR human sign-off, not even at ``autonomy = full``.
    if clear.is_substrate():
        return False
    overlay_name = _resolve_clear_overlay_name(clear, resolved_slug=resolved_slug)
    if not overlay_name:
        return False
    settings = get_effective_settings(overlay_name=overlay_name)
    if settings.autonomy is Autonomy.FULL:
        return True
    # The collaborative ``notify`` tier collapses the merge-approval gate to
    # ``false`` too, but merges only after a colleague approval — its ``false``
    # is a tier side effect, never the owner's self-merge grant.
    if settings.autonomy is Autonomy.NOTIFY:
        return False
    return settings.require_human_approval_to_merge is False


def _assert_anti_vacuity(clear: "MergeClear", head_sha: str) -> None:
    """Refuse a merge whose CLEAR ticket lacks a SHA-bound anti-vacuity proof (#1829).

    NO-OP when ``require_anti_vacuity_attestation`` is off (opt-in default) or
    the CLEAR carries no ticket (the attestation lives on the ticket's durable
    ``extra``). The :class:`AntiVacuityAttestationError` raised on a block is
    re-wrapped as a :class:`MergePreconditionError` so the merge command's
    single re-escalation path surfaces it (the loop never self-issues a
    replacement CLEAR).
    """
    from teatree.core.gates.anti_vacuity_gate import (  # noqa: PLC0415
        AntiVacuityAttestationError,
        check_anti_vacuity_attestation,
    )

    ticket = clear.ticket
    if ticket is None:
        return
    try:
        check_anti_vacuity_attestation(ticket, head_sha, transition="merge")
    except AntiVacuityAttestationError as exc:
        raise MergePreconditionError(str(exc)) from exc


def _assert_rubric_satisfied(clear: "MergeClear", head_sha: str) -> None:
    """Refuse a merge whose CLEAR ticket's rubric is not fully PASS at ``head_sha`` (#2241).

    NO-OP when ``require_rubric_verification`` is off (opt-in default) or the CLEAR
    carries no ticket (the rubric is FK'd to the ticket). The
    :class:`RubricNotSatisfiedError` raised on a block is re-wrapped as a
    :class:`MergePreconditionError` so the merge command's single re-escalation
    path surfaces it (the loop never self-issues a replacement CLEAR). Sibling of
    :func:`_assert_anti_vacuity`; called immediately after it, bound to the same
    just-verified live head SHA so a force-push invalidates the CLEAR, the
    attestation, and the rubric grade together.
    """
    from teatree.core.gates.rubric_gate import RubricNotSatisfiedError, check_rubric_satisfied  # noqa: PLC0415

    ticket = clear.ticket
    if ticket is None:
        return
    try:
        check_rubric_satisfied(ticket, head_sha, transition="merge")
    except RubricNotSatisfiedError as exc:
        raise MergePreconditionError(str(exc)) from exc


def assert_public_repo_author_trusted(*, slug: str, pr_id: int, host_kind: str = "github") -> None:
    """Refuse the merge when *slug* is PUBLIC and the PR author is not trusted (#1773).

    The authoritative, load-bearing author gate (BLUEPRINT §17.4.3 step 6 /
    invariant 8): every sanctioned merge funnels through ``merge_ticket_pr``, so
    even a future scanner that forgets the author still cannot auto-merge an
    untrusted public-repo PR. The overlay merge-guard sits in FRONT of this
    keystone, so relaxing an overlay over-block can never relax this gate.

    PRIVATE / internal repo -> no author check (the user owns access control).
    PUBLIC repo -> the author must be a trusted identity; an untrusted, unknown,
    empty, or unfetchable author is refused (fail-closed).
    """
    author = fetch_pr_author(slug, pr_id, host_kind=host_kind)
    classification = classify_author(slug, author, host_kind=host_kind)
    if classification.internal_repo or classification.trusted:
        return
    msg = (
        f"{slug}#{pr_id} is on a PUBLIC repo and its author is not a trusted identity — refusing to "
        f"auto-merge (§17.4.3 author gate / #1773). On a public repo anyone who is not the user is a "
        f"potential malicious actor; add the handle via `t3 identities add <platform> <handle>` if it is "
        f"genuinely the user, or merge it by hand after an adversarial review."
    )
    raise MergePreconditionError(msg)
