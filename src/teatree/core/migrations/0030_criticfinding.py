import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_planartifact_base_sha_adequacy"),
    ]

    operations = [
        migrations.CreateModel(
            name="CriticFinding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("transition", models.CharField(max_length=64)),
                ("rubric_item", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[("fail", "Fail"), ("instrumentation_gap", "Instrumentation gap")],
                        default="fail",
                        max_length=32,
                    ),
                ),
                ("adversarial_question", models.CharField(blank=True, default="", max_length=255)),
                ("detail", models.TextField(blank=True, default="")),
                ("head_sha", models.CharField(blank=True, default="", max_length=64)),
                ("recorded_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="critic_findings",
                        to="core.ticket",
                    ),
                ),
            ],
            options={
                "db_table": "teatree_critic_finding",
                "ordering": ["-recorded_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="criticfinding",
            constraint=models.UniqueConstraint(
                fields=("ticket", "transition", "rubric_item"),
                name="uniq_critic_finding_ticket_transition_item",
            ),
        ),
    ]
