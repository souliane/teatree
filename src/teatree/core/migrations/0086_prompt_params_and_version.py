"""Add ``Prompt.params`` + the ``PromptVersion`` history table (#2513, D2).

Additive: a new nullable-defaulted ``params`` JSON column on ``teatree_prompt``
(every existing row defaults to an empty list, so no data migration) and a new
``teatree_prompt_version`` table for the superseded-content snapshots written by
``Prompt.revise``. No existing row is touched.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0085_loop_prompt_to_prompt_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="prompt",
            name="params",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="PromptVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveIntegerField()),
                ("body", models.TextField()),
                ("params", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "prompt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="core.prompt"
                    ),
                ),
            ],
            options={
                "db_table": "teatree_prompt_version",
                "ordering": ["prompt", "version"],
                "constraints": [models.UniqueConstraint(fields=("prompt", "version"), name="prompt_version_unique")],
            },
        ),
    ]
