"""Add ``LoopLease.session_id`` for the session-scoped loop-owner claim (#1073).

Trivial ``AddField`` — no backfill. Existing rows (the ``loop-tick``
concurrency mutex) keep ``session_id=""`` and are unaffected; the new
persistent ``loop-owner`` row is created on first contact by
``LoopLeaseQuerySet.claim_ownership``'s ``get_or_create``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_outbound_claim"),
    ]

    operations = [
        migrations.AddField(
            model_name="looplease",
            name="session_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
