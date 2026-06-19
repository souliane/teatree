"""Create the first-class reusable :class:`Prompt` model (#2513).

Additive, schema-only: a new ``teatree_prompt`` table. The ``Loop.prompt``
TextField → FK conversion is the separate, data-preserving migration that
follows (``0085``), so this one never touches existing rows.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0083_loop_script_requires_delay"),
    ]

    operations = [
        migrations.CreateModel(
            name="Prompt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=64, unique=True)),
                ("body", models.TextField()),
                ("description", models.TextField(blank=True, default="")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "teatree_prompt",
                "ordering": ["name"],
            },
        ),
    ]
