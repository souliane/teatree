"""Why a standing merge authorisation was spent without a merge (#4739).

``reconcile-clears`` spends a CLEAR only on forge evidence, and refuses without it —
correct, and it left one class of row standing forever: a CLEAR whose PR never existed.
The evidence that refusal waits for can never arrive, so two rows sat ``live`` for
weeks, misreporting the answer to "what is currently authorised to merge?".

Disposal is the third outcome, and it is RECORDED rather than silent: deleting the row
would erase that something once authorised a merge, and stamping ``consumed_at`` alone
would make a phantom indistinguishable from an authorisation a real merge spent. The
reason lives here rather than on ``MergeClear`` because that module is over the
module-health LOC cap and may only shrink.
"""

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.merge_clear import MergeClear


class MergeClearDisposalManager(models.Manager["MergeClearDisposal"]):
    def dispose_phantom_family(
        self,
        clear: MergeClear,
        *,
        pr_url: str,
        now: datetime | None = None,
    ) -> list[MergeClear]:
        """Consume every unconsumed CLEAR sharing *clear*'s ``(slug, pr_id)``; return them.

        The 404 is evidence about the PR NUMBER, not about one row, and the ledger holds
        hundreds of superseded siblings per phantom number — disposing only the live one
        would leave ``list-clears`` reporting the rest forever.

        An already-consumed row is left exactly as it is: its ``consumed_at`` records a
        real merge, and re-stamping it would rewrite that history. Idempotent, so the
        loop's reconcile lane and a hand-run pass cannot double-dispose.
        """
        stamp = now or timezone.now()
        with transaction.atomic():
            family = list(
                MergeClear.objects.filter(
                    slug=clear.slug,
                    pr_id=clear.pr_id,
                    consumed_at__isnull=True,
                ).order_by("pk")
            )
            if not family:
                return []
            self.bulk_create(
                [
                    MergeClearDisposal(
                        clear=row,
                        reason=MergeClearDisposal.Reason.PHANTOM_PR,
                        evidence=f"forge reports no such PR: {pr_url}"[:255],
                        disposed_at=stamp,
                    )
                    for row in family
                ],
                ignore_conflicts=True,
            )
            MergeClear.objects.filter(pk__in=[row.pk for row in family]).update(consumed_at=stamp)
        return family


class MergeClearDisposal(models.Model):
    """The recorded reason a ``MergeClear`` was spent by something other than a merge."""

    class Reason(models.TextChoices):
        PHANTOM_PR = "phantom_pr", "PR absent from the forge"

    clear = models.OneToOneField(MergeClear, on_delete=models.CASCADE, related_name="disposal")
    reason = models.CharField(max_length=32, choices=Reason.choices)
    evidence = models.CharField(max_length=255)
    disposed_at = models.DateTimeField(default=timezone.now)

    objects = MergeClearDisposalManager()

    class Meta:
        ordering: ClassVar[list[str]] = ["-disposed_at"]

    if TYPE_CHECKING:
        # Django synthesises the ``<fk>_id`` shadow attribute at class-prep time —
        # invisible to a static checker. Declared here (annotation-only, never
        # evaluated at runtime) so ``__str__`` reads the id without a relation query.
        clear_id: int

    def __str__(self) -> str:
        return f"disposal of clear {self.clear_id} ({self.reason})"
