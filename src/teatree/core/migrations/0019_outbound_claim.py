"""Create ``OutboundClaim`` for the outbound-audit drift verifier (#1019)."""

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_deferred_question"),
    ]

    operations = [
        migrations.CreateModel(
            name="OutboundClaim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent_session_id", models.CharField(blank=True, max_length=255)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("slack_dm", "Slack DM"),
                            ("slack_reaction", "Slack reaction"),
                            ("gitlab_note", "GitLab note"),
                            ("gitlab_approve", "GitLab approve"),
                            ("notion_comment", "Notion comment"),
                            ("notion_edit", "Notion edit"),
                        ],
                        max_length=32,
                    ),
                ),
                ("target_url", models.URLField(blank=True, max_length=1024)),
                ("idempotency_key", models.CharField(max_length=255, unique=True)),
                ("claim_ts", models.DateTimeField(default=django.utils.timezone.now)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("drift_detected", models.BooleanField(default=False)),
                ("drift_reason", models.TextField(blank=True)),
                ("drift_alerted_at", models.DateTimeField(blank=True, null=True)),
                ("extra", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "db_table": "teatree_outbound_claim",
                "ordering": ["-claim_ts"],
                "indexes": [
                    models.Index(fields=["kind", "claim_ts"], name="teatree_out_kind_cl_idx"),
                    models.Index(fields=["verified_at", "drift_detected"], name="teatree_out_verify_idx"),
                    models.Index(
                        fields=["drift_detected", "drift_alerted_at"],
                        name="teatree_out_drift_idx",
                    ),
                ],
            },
        ),
    ]
