from django.db import migrations, models

# ``derive_issue_number`` is imported (rather than inlined as frozen history) on
# purpose: it is the stable pure trailing-digits helper ``Ticket.save`` also
# calls, so reusing it keeps the backfill and the live column-write byte-for-byte
# in agreement — an inlined copy could silently drift. It performs no DB access
# and reads no evolving schema, so the migration stays self-contained.
from teatree.core.models.ticket_number import derive_issue_number


def _backfill_issue_number(apps, schema_editor):
    """Denormalize each existing ticket's forge issue number into the new column.

    Mirrors ``Ticket.save`` exactly (``derive_issue_number(issue_url)``) so the
    backfilled value equals what a subsequent save would write — the indexed
    ``_ticket_by_number`` lookup then agrees with the ``ticket_number`` property
    for every pre-existing row. Rows with a blank ``issue_url`` already hold the
    correct ``""`` default and are skipped.
    """
    ticket_model = apps.get_model("core", "Ticket")
    # Scope the read and the bulk_update to the alias the migration is running
    # against (matches 0005) so a multi-DB / non-default-alias apply backfills the
    # DB being migrated, not the default connection.
    db_alias = schema_editor.connection.alias
    pending = []
    for ticket in ticket_model.objects.using(db_alias).exclude(issue_url="").only("pk", "issue_url", "issue_number"):
        derived = derive_issue_number(ticket.issue_url)
        if ticket.issue_number != derived:
            ticket.issue_number = derived
            pending.append(ticket)
    ticket_model.objects.using(db_alias).bulk_update(pending, ["issue_number"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_implementedissuemarker_claim_ref_sha"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="issue_number",
            field=models.CharField(blank=True, db_index=True, default="", max_length=32),
        ),
        migrations.RunPython(_backfill_issue_number, migrations.RunPython.noop),
    ]
