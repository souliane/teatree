"""§17.4.3 identity/substrate authorization guards + the anti-vacuity wrapper.

The result type :class:`MergePrecheck` and the guard functions
:func:`_assert_clear_authorized` / :func:`_assert_anti_vacuity` that
:mod:`execution` runs before it binds the irreversible merge.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

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

    # 5. blast_class respected — substrate-class PRs are draft-locked and
    #    require a recorded human sign-off (invariant 4 / §17.4.3 step 5). Two
    #    things satisfy the per-PR sign-off, in this order:
    #      a. a per-CLEAR ``human_authorizer`` matching the value re-presented
    #         at merge time (the owner approved this exact diff), OR
    #      b. the overlay's STANDING autonomy grant resolving to ``full`` — the
    #         owner recorded once, in config, that this overlay merges
    #         end-to-end without a per-PR sign-off (invariant 4 carve-out).
    #    Either way the AGENT executes through this same SHA-bound, audited
    #    transition (invariant 8) — never raw ``gh``, never a human-performed
    #    merge. The quality/safety floor (independent cold-review, reviewed-SHA
    #    bind, CI-green, not-draft, never-lockout, privacy scan) is untouched by
    #    the carve-out; autonomy=full removes ONLY the per-PR human sign-off.
    if (
        clear.is_substrate()
        and not clear.human_merge_authorized_by(presented)
        and not _overlay_grants_full_substrate_autonomy(clear)
    ):
        detail = (
            "no human authoriser recorded on the CLEAR and the overlay autonomy is not full"
            if not clear.human_authorizer
            else f"presented authoriser != recorded ({clear.human_authorizer!r})"
            if presented
            else "no --human-authorized presented at merge time and the overlay autonomy is not full"
        )
        msg = (
            f"MergeClear for {slug}#{pr_id} is blast_class=substrate — substrate "
            f"changes require a recorded human approval and are draft-locked "
            f"(invariant 4); the loop never auto-merges them (§17.4.3 step 5). "
            f"{detail.capitalize()}. The sanctioned paths: `t3 <overlay> autonomy "
            f"set full` (the standing owner grant), or issue `t3 <overlay> ticket "
            f"clear … --blast-class substrate --human-authorize <id>` (a per-PR "
            f"recorded approval), then the agent executes `t3 <overlay> ticket "
            f"merge <clear_id> [--human-authorized <id>]`"
        )
        raise MergePreconditionError(msg)

    return clear


def _resolve_clear_overlay_name(clear: "MergeClear") -> str:
    """The overlay name to resolve autonomy against for *clear*.

    The CLEAR's ``ticket.overlay`` is authoritative when present, but the loop
    routinely issues a CLEAR with no linked ticket (every substrate CLEAR in
    the live ledger). The CLEAR always carries the ``owner/repo`` ``slug``, so
    the overlay is recovered from it via :func:`infer_overlay_for_url` — the
    same workspace-repos inference ``ticket.overlay`` itself is populated from.
    Returns ``""`` when neither source resolves an overlay.
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    overlay_name = str(getattr(getattr(clear, "ticket", None), "overlay", "") or "").strip()
    if overlay_name:
        return overlay_name
    return infer_overlay_for_url(str(getattr(clear, "slug", "") or "")).strip()


def _overlay_grants_full_substrate_autonomy(clear: "MergeClear") -> bool:
    """Whether the CLEAR's overlay stands at ``autonomy = full`` (invariant 4 carve-out).

    Resolves the effective autonomy for the CLEAR's overlay
    (:func:`_resolve_clear_overlay_name`) via :func:`get_effective_settings`.
    ``full`` is the owner's standing, recorded grant that this overlay merges
    end-to-end without a per-PR human sign-off; it satisfies the substrate
    sign-off in place of a per-CLEAR ``human_authorizer``. Any other tier
    (``notify`` / ``babysit``), or an unresolvable overlay, is fail-closed:
    the per-CLEAR human authoriser stays mandatory. The carve-out touches ONLY
    the per-PR sign-off — every other substrate-merge floor guard runs unchanged.
    """
    from teatree.config import Autonomy, get_effective_settings  # noqa: PLC0415

    overlay_name = _resolve_clear_overlay_name(clear)
    if not overlay_name:
        return False
    return get_effective_settings(overlay_name=overlay_name).autonomy is Autonomy.FULL


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
