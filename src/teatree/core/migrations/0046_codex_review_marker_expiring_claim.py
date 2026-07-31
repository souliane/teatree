from django.db import migrations, models


def _spend_legacy_codex_claims(apps, schema_editor):
    """Mark every pre-#3921 marker RESOLVED, preserving its "never re-dispatch" outcome.

    A row that already exists was claimed under the unbounded ``get_or_create``, so
    today it blocks its head forever. RESOLVED is terminal under the shared
    acquirability predicate, which reproduces that outcome exactly — the migration
    is behaviour-preserving for every existing row, and only heads claimed from here
    on can ever be re-armed.

    Leaving them ``dispatched`` would preserve the outcome too (a NULL deadline is
    never stolen by expiry), but it would make ``state=dispatched`` mean "in flight"
    for live claims and "abandoned years ago" for legacy ones. ``resolved_at`` stays
    NULL because the real resolution time is genuinely unknown; fabricating one from
    ``dispatched_at`` would assert a fact this migration does not have.
    """
    apps.get_model("core", "CodexReviewMarker").objects.update(state="resolved")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0045_unshipped_work_first_captured_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="codexreviewmarker",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="codexreviewmarker",
            name="deadline",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="codexreviewmarker",
            name="resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="codexreviewmarker",
            name="state",
            field=models.CharField(
                choices=[("dispatched", "Dispatched"), ("resolved", "Resolved")],
                default="dispatched",
                max_length=32,
            ),
        ),
        migrations.RunPython(_spend_legacy_codex_claims, migrations.RunPython.noop),
    ]
