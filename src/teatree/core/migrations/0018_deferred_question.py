# Generated for #58 — 24/7 dual question-mode (BLUEPRINT §17.1 invariant 9 / §17.3 C3).

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_botping"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeferredQuestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question", models.TextField()),
                ("options_json", models.TextField(blank=True, default="")),
                ("session_id", models.CharField(blank=True, default="", max_length=255)),
                ("tool_use_id", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("answered_at", models.DateTimeField(blank=True, null=True)),
                ("answer_text", models.TextField(blank=True, default="")),
                ("dismissed_at", models.DateTimeField(blank=True, null=True)),
                ("dismissed_reason", models.TextField(blank=True, default="")),
            ],
            options={
                "db_table": "teatree_deferred_question",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="DeferredQuestionAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=16)),
                ("answer_text", models.TextField(blank=True, default="")),
                ("dismissed_reason", models.TextField(blank=True, default="")),
                ("resolver_id", models.CharField(blank=True, default="", max_length=255)),
                ("resolved_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "question",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audits",
                        to="core.deferredquestion",
                    ),
                ),
            ],
            options={
                "db_table": "teatree_deferred_question_audit",
                "ordering": ["-resolved_at"],
            },
        ),
    ]
