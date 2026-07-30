from django.db import migrations


def _rehome_reviewer_delivered(apps, schema_editor):
    """Move reviewer-role DELIVERED tickets with no merge audit to REVIEW_POSTED.

    Reviewer tickets used to short-circuit to DELIVERED, so the board showed
    them as author-merged "Landed" work. DELIVERED now means only author work
    merged to main; re-home the mislabeled reviewer ghosts. A reviewer ticket
    that genuinely produced a merge (a MergeAudit via a linked MergeClear) is
    left alone — the "no merge audit" predicate is what distinguishes a review
    ghost from real merged work. Idempotent: a second run matches nothing.
    """
    Ticket = apps.get_model("core", "Ticket")
    MergeAudit = apps.get_model("core", "MergeAudit")

    merged_ticket_ids = set(
        MergeAudit.objects.filter(clear__ticket__isnull=False).values_list("clear__ticket_id", flat=True),
    )
    Ticket.objects.filter(role="reviewer", state="delivered").exclude(pk__in=merged_ticket_ids).update(
        state="review_posted",
    )


def _restore_reviewer_delivered(apps, schema_editor):
    Ticket = apps.get_model("core", "Ticket")
    Ticket.objects.filter(role="reviewer", state="review_posted").update(state="delivered")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0025_alter_ticket_state"),
    ]

    operations = [
        migrations.RunPython(_rehome_reviewer_delivered, _restore_reviewer_delivered),
    ]
