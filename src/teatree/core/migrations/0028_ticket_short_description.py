"""Add ``Ticket.short_description`` — terminal-friendly AI summary (#1156).

A short (=40 char rendered) line per ticket that the statusline anchors
zone surfaces alongside ``#N``. Generated lazily via the existing
scanner → task queue chain: ``ActiveTicketsScanner`` enqueues a
``Task(phase="short_describe", execution_target=HEADLESS)`` for rows
where ``short_description=""`` and ``extra["issue_title"]`` is non-blank,
and the headless worker writes the result back.

Pure additive ``AddField`` — no data migration, no field type change.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_canonicalize_teatree_overlay"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="short_description",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
    ]
