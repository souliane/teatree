from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from teatree.core.modelkit.gate_registry import get_gate
from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.types import validated_ticket_extra

if TYPE_CHECKING:
    from teatree.core.models.types import (
        AntiVacuityAttestation,
        ReviewContext,
        ReviewSkillRun,
        TicketExtra,
        TicketSiblingFields,
    )


class TicketEvidenceModel(TicketFacet):
    """The durable ``extra``/``context`` evidence store — one locked read-modify-write primitive (#800 N3)."""

    class Meta:
        abstract = True

    def _extra(self) -> "TicketExtra":
        return validated_ticket_extra(self.extra)

    def merge_extra(
        self,
        *,
        set_keys: "TicketExtra | None" = None,
        pop_keys: "list[str] | None" = None,
        also_set: "TicketSiblingFields | None" = None,
    ) -> None:
        """Canonical locked read-modify-write of ``extra`` (#800 N3).

        Several writers mutate shared ``extra`` JSON — ``pr_urls`` (ship
        worker), ``visual_qa`` (the pre-push gate), ``reviewed_sha`` /
        ``last_review_state`` (reviewer path). Done as an unlocked
        ``self.extra = …; self.save(update_fields=["extra"])`` they
        last-writer-clobber each other's key (the Haki-Benita
        lost-update). This is the single primitive every ``extra``
        mutation routes through, with the same shape as
        ``Session.visit_phase``: the RMW runs in ``transaction.atomic()``
        with the row ``select_for_update``-locked and **re-read from the
        locked row** (not the possibly-stale in-memory instance), so a
        concurrent writer's key survives the merge instead of being
        overwritten. The locked re-read is what makes it correct on the
        production SQLite backend (where ``select_for_update`` is a no-op
        but the #804 ``BEGIN IMMEDIATE`` serialises the writers, so the
        re-read sees the other writer's committed key).

        ``also_set`` writes sibling **model fields** (``state``,
        ``repos``, ``variant``, …) in the SAME locked ``UPDATE`` as
        ``extra``. The tracker-sync paths legitimately co-write
        ``extra`` with ``state``/``repos`` in one ``save`` — routing
        them through here keeps that write atomic (no split into two
        non-atomic writes) while still going through the single locked
        primitive, so the SSOT holds with zero unlocked ``extra`` RMW
        anywhere.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            merged = dict(locked.extra or {})
            if set_keys:
                merged.update(set_keys)
            for key in pop_keys or []:
                merged.pop(key, None)
            self.extra = merged
            for field, value in (also_set or {}).items():
                setattr(self, field, value)
            type(self).objects.filter(pk=self.pk).update(extra=merged, **(also_set or {}))

    def record_review_skill_run(self, skill: str) -> None:
        """Stamp durable evidence that the deep-review ``skill`` ran (#1539).

        Written through the canonical locked ``merge_extra`` primitive so a
        concurrent ``extra`` writer's key survives. The timestamp is UTC ISO
        so the reviewing-phase gate's audit trail is timezone-unambiguous.
        """
        run: ReviewSkillRun = {"skill": skill, "at": timezone.now().isoformat()}
        self.merge_extra(set_keys={"review_skill_run": run})

    def record_review_context(self, work_item: str, documents: list[str], analysis: str) -> None:
        """Stamp durable evidence the referenced context was retrieved + analyzed.

        Reviewing carries the same responsibility as implementing: the
        ``-> reviewing`` deep-retrieval gate (``teatree.core.gates.review_context_gate``)
        reads this to refuse a verdict formed from the diff alone. ``work_item``
        is the fetched ticket / work-item source, ``documents`` the downloaded
        references, ``analysis`` how the implementation was checked against the
        specified requirements. Written through the canonical locked
        ``merge_extra`` primitive so a concurrent ``extra`` writer's key
        survives; the timestamp is UTC ISO.
        """
        context: ReviewContext = {
            "work_item": work_item,
            "documents": list(documents),
            "analysis": analysis,
            "at": timezone.now().isoformat(),
        }
        self.merge_extra(set_keys={"review_context": context})

    def record_anti_vacuity_attestation(
        self,
        head_sha: str,
        ac_coverage: str,
        proven_tests: list[str],
        *,
        no_new_tests: bool = False,
    ) -> None:
        """Stamp the SHA-bound anti-vacuity attestation backing review-request/merge (#1829).

        ``head_sha`` binds the attestation to the exact tree the maker
        self-reviewed; the anti-vacuity gate (``teatree.core.gates.anti_vacuity_gate``)
        drops it when the live head moves. ``ac_coverage`` records how the diff
        was mapped to the acceptance criteria. ``proven_tests`` lists every new
        regression test proven anti-vacuous (revert fix -> RED); ``no_new_tests``
        is the explicit "this diff adds no new regression test" claim so an
        empty ``proven_tests`` can never silently pass. Written through the
        canonical locked ``merge_extra`` primitive so a concurrent ``extra``
        writer's key survives; the timestamp is UTC ISO.
        """
        attestation: AntiVacuityAttestation = {
            "head_sha": head_sha.strip().lower(),
            "ac_coverage": ac_coverage,
            "proven_tests": list(proven_tests),
            "no_new_tests": no_new_tests,
            "at": timezone.now().isoformat(),
        }
        self.merge_extra(set_keys={"anti_vacuity_attestation": attestation})

    def review_context_satisfied(self) -> bool:
        """Whether the ``-> reviewing`` deep-retrieval precondition is met.

        An FSM ``condition`` on ``review()``: the ``TESTED -> REVIEWED``
        transition is mechanically refused (``TransitionNotAllowed``) when
        ``require_review_context`` is on and no complete ``review_context``
        artifact is recorded — so a verdict from the diff alone cannot advance
        the FSM regardless of entry path. NO-OP (returns ``True``) when the knob
        is off (opt-in default preserved).
        """
        return bool(get_gate("review_context_satisfied")(self))

    def append_context(self, entry: str) -> str:
        r"""Append a timestamped block to the durable per-ticket knowledge store (#627).

        ``context`` is append-only: parallel sessions on the same ticket each
        add their own ``\n\n[YYYY-MM-DD HH:MM] …`` block rather than
        overwriting, so a later session never loses an earlier one's note
        (open question 2 — append-only with timestamp prefixes). Returns the
        full updated context. Refuses a blank entry — an empty note carries no
        durable knowledge and would just add noise.
        """
        text = entry.strip()
        if not text:
            msg = "context entry is empty"
            raise ValueError(msg)
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            updated = f"{locked.context}\n\n[{stamp}] {text}"
            self.context = updated
            type(self).objects.filter(pk=self.pk).update(context=updated)
        return updated
