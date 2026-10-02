import django.db.models.deletion
from django.db import migrations, models

_ADD_FALLBACK_FROM_ATTEMPT = (
    'ALTER TABLE "teatree_taskattempt" ADD COLUMN "fallback_from_attempt_id" bigint NULL '
    'REFERENCES "teatree_taskattempt" ("id") DEFERRABLE INITIALLY DEFERRED'
)
_DROP_FALLBACK_FROM_ATTEMPT = 'ALTER TABLE "teatree_taskattempt" DROP COLUMN "fallback_from_attempt_id"'
_CREATE_FALLBACK_FROM_ATTEMPT_INDEX = (
    'CREATE INDEX "teatree_taskattempt_fallback_from_attempt_id_0bf32d43" '
    'ON "teatree_taskattempt" ("fallback_from_attempt_id")'
)
_DROP_FALLBACK_FROM_ATTEMPT_INDEX = 'DROP INDEX "teatree_taskattempt_fallback_from_attempt_id_0bf32d43"'


class Migration(migrations.Migration):
    dependencies = [("core", "0110_merge_clear_merged_without_squash")]

    operations = [
        migrations.CreateModel(
            name="AgentRouteAvailability",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=128)),
                ("harness", models.CharField(max_length=128)),
                ("provider", models.CharField(blank=True, default="", max_length=128)),
                ("model", models.CharField(max_length=255)),
                ("phase", models.CharField(blank=True, default="", max_length=128)),
                ("unavailable_reason", models.TextField(blank=True, default="")),
                ("observed_at", models.DateTimeField()),
                ("retry_at", models.DateTimeField()),
            ],
            options={
                "db_table": "teatree_agentrouteavailability",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("overlay", "harness", "provider", "model", "phase"),
                        name="uniq_agent_route_availability",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="taskattempt",
            name="selected_harness",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="taskattempt",
            name="selected_provider",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="taskattempt",
            name="selected_model",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="taskattempt",
            name="route_candidate_index",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="taskattempt",
            name="route_source_skill",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="taskattempt",
            name="fallback_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="taskattempt",
                    name="fallback_from_attempt",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fallback_attempts",
                        to="core.taskattempt",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=_ADD_FALLBACK_FROM_ATTEMPT,
                    reverse_sql=_DROP_FALLBACK_FROM_ATTEMPT,
                ),
                migrations.RunSQL(
                    sql=_CREATE_FALLBACK_FROM_ATTEMPT_INDEX,
                    reverse_sql=_DROP_FALLBACK_FROM_ATTEMPT_INDEX,
                ),
            ],
        ),
        migrations.AlterField(
            model_name="taskattempt",
            name="lane",
            field=models.CharField(
                blank=True,
                choices=[("subscription", "Subscription"), ("metered", "Metered"), ("managed", "Managed")],
                default="",
                max_length=16,
            ),
        ),
    ]
