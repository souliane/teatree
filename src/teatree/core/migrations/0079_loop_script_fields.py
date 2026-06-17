"""Add the loop script fields and make ``delay_seconds`` nullable (Phase 0).

Additive only: the script entry point, the sub-agent toggle, a human
description, and a generic backend name, plus a nullable interval so a
cadence-less loop runs every tick. The prompt-XOR-script constraint is added in
0081 *after* 0080 backfills every existing row to satisfy it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0078_seed_loops"),
    ]

    operations = [
        migrations.AddField(
            model_name="loop",
            name="script",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="loop",
            name="run_in_sub_agent",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="loop",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="loop",
            name="overlay",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="loop",
            name="delay_seconds",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
