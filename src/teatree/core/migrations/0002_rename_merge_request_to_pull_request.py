"""Rename MergeRequest model to PullRequest, table and related_name to match.

Phase 3 of issue #541 declared "PR" as the canonical term in core; this
migration completes that rename at the schema layer. It also rewrites the
``Ticket.extra["mrs"]`` JSONField key to ``Ticket.extra["prs"]`` so prompt
templates and scanners can read the new key consistently.
"""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def _rename_extra_mrs_to_prs(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Ticket = apps.get_model("core", "Ticket")
    for ticket in Ticket.objects.exclude(extra={}).iterator():
        extra = ticket.extra or {}
        if not isinstance(extra, dict) or "mrs" not in extra:
            continue
        extra["prs"] = extra.pop("mrs")
        ticket.extra = extra
        ticket.save(update_fields=["extra"])


def _rename_extra_prs_to_mrs(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Ticket = apps.get_model("core", "Ticket")
    for ticket in Ticket.objects.exclude(extra={}).iterator():
        extra = ticket.extra or {}
        if not isinstance(extra, dict) or "prs" not in extra:
            continue
        extra["mrs"] = extra.pop("prs")
        ticket.extra = extra
        ticket.save(update_fields=["extra"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RenameModel(old_name="MergeRequest", new_name="PullRequest"),
        migrations.AlterModelTable(
            name="pullrequest",
            table="teatree_pull_request",
        ),
        migrations.AlterField(
            model_name="pullrequest",
            name="ticket",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                related_name="pull_requests",
                to="core.ticket",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="pullrequest",
            name="unique_merge_request_url",
        ),
        migrations.AddConstraint(
            model_name="pullrequest",
            constraint=models.UniqueConstraint(fields=("url",), name="unique_pull_request_url"),
        ),
        migrations.RunPython(_rename_extra_mrs_to_prs, _rename_extra_prs_to_mrs),
    ]
