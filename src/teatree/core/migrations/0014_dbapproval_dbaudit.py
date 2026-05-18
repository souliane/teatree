import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_ticket_context"),
    ]

    operations = [
        migrations.CreateModel(
            name="DbApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("op", models.CharField(max_length=64)),
                ("tenant", models.CharField(max_length=255)),
                ("approver_id", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "teatree_db_approval",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="DbAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("op", models.CharField(max_length=64)),
                ("tenant", models.CharField(max_length=255)),
                ("approver_id", models.CharField(max_length=255)),
                ("executed_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "approval",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audits",
                        to="core.dbapproval",
                    ),
                ),
            ],
            options={
                "db_table": "teatree_db_audit",
                "ordering": ["-executed_at"],
            },
        ),
    ]
