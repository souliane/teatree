# Generated for #1295 — autonomous review team pipeline.
#
# Adds three idempotency ledgers used by the new fat-loop sweeps:
#   * RedMrFixAttempt — capability D (my_pr.failed → t3:debug dispatch dedup)
#   * ScannedFailedE2E — capability E (failed-E2E Slack post → t3:e2e dedup)
#   * AssessFinding + AssessSweepRun — capability H (nightly assess sweep dedup
#     + cadence).

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_add_github_note_kind"),
    ]

    operations = [
        migrations.CreateModel(
            name="RedMrFixAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pr_url", models.URLField(max_length=512)),
                ("head_sha", models.CharField(max_length=64)),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("dispatched_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("worktree_hint", models.CharField(blank=True, default="", max_length=512)),
            ],
            options={
                "db_table": "teatree_red_mr_fix_attempt",
                "ordering": ["-dispatched_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="redmrfixattempt",
            constraint=models.UniqueConstraint(
                fields=("pr_url", "head_sha"),
                name="uniq_redmrfix_url_sha",
            ),
        ),
        migrations.CreateModel(
            name="ScannedFailedE2E",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("channel", models.CharField(max_length=64)),
                ("slack_ts", models.CharField(max_length=64)),
                ("spec_path", models.CharField(max_length=512)),
                ("test_title", models.CharField(blank=True, default="", max_length=512)),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "teatree_scanned_failed_e2e",
                "ordering": ["-observed_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="scannedfailede2e",
            constraint=models.UniqueConstraint(
                fields=("channel", "slack_ts", "spec_path"),
                name="uniq_failede2e_channel_ts_spec",
            ),
        ),
        migrations.CreateModel(
            name="AssessFinding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64)),
                ("repo", models.CharField(max_length=512)),
                ("file_path", models.CharField(max_length=512)),
                ("finding_fingerprint", models.CharField(max_length=128)),
                ("severity", models.CharField(blank=True, default="", max_length=32)),
                ("finding_text", models.TextField(blank=True, default="")),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("dispatched_task_id", models.CharField(blank=True, default="", max_length=64)),
            ],
            options={
                "db_table": "teatree_assess_finding",
                "ordering": ["-observed_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="assessfinding",
            constraint=models.UniqueConstraint(
                fields=("repo", "file_path", "finding_fingerprint"),
                name="uniq_assessfinding_repo_file_fpr",
            ),
        ),
        migrations.CreateModel(
            name="AssessSweepRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overlay", models.CharField(blank=True, default="", max_length=64, unique=True)),
                ("last_run_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "teatree_assess_sweep_run",
            },
        ),
    ]
