# Generated for the pull-main-clone scanner — per-repo cadence ledger for work-repo main clones.

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_pendingarticlesuggestion"),
    ]

    operations = [
        migrations.CreateModel(
            name="PullMainCloneMarker",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("repo_label", models.CharField(max_length=128, unique=True)),
                ("repo_path", models.CharField(blank=True, default="", max_length=512)),
                ("last_outcome", models.CharField(blank=True, default="", max_length=16)),
                ("last_reason", models.CharField(blank=True, default="", max_length=200)),
                ("last_pulled_sha", models.CharField(blank=True, default="", max_length=64)),
                ("last_pull_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "teatree_pull_main_clone_marker",
                "ordering": ["-last_pull_at"],
            },
        ),
    ]
