import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_alter_botping_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="state",
            field=__import__("django_fsm").FSMField(
                choices=[
                    ("not_started", "Not started"),
                    ("scoped", "Scoped"),
                    ("started", "Started"),
                    ("planned", "Planned"),
                    ("coded", "Coded"),
                    ("tested", "Tested"),
                    ("reviewed", "Reviewed"),
                    ("shipped", "Shipped"),
                    ("in_review", "In review"),
                    ("merged", "Merged"),
                    ("retrospected", "Retrospected"),
                    ("delivered", "Delivered"),
                    ("ignored", "Ignored"),
                ],
                default="not_started",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="PlanArtifact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("plan_text", models.TextField()),
                ("recorded_by", models.CharField(blank=True, default="", max_length=255)),
                ("recorded_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plan_artifacts",
                        to="core.ticket",
                    ),
                ),
            ],
            options={
                "db_table": "teatree_plan_artifact",
                "ordering": ["-recorded_at"],
            },
        ),
    ]
