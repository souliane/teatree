"""Record the forge each CLEAR's merge transport binds to.

A bare ``owner/repo`` slug carries no host, so a CLEAR with no ticket had
nothing to resolve the forge from and the keystone silently defaulted to
``github`` — binding the GitHub transport against a GitLab MR. Existing rows are
backfilled with exactly that legacy rule (the ticket's ``issue_url`` host, else
``github``) so no in-flight CLEAR is invalidated; only newly-issued CLEARs must
name their forge.
"""

from django.db import migrations, models

from teatree.utils.forge import forge_from_remote


def backfill_host_kind(apps, schema_editor):
    merge_clear = apps.get_model("core", "MergeClear")
    clears = merge_clear.objects.using(schema_editor.connection.alias)
    for clear in clears.select_related("ticket").iterator():
        issue_url = clear.ticket.issue_url if clear.ticket_id else ""
        clears.filter(pk=clear.pk).update(host_kind=forge_from_remote(issue_url or "") or "github")


def clear_host_kind(apps, schema_editor):
    apps.get_model("core", "MergeClear").objects.using(schema_editor.connection.alias).update(host_kind="")


class Migration(migrations.Migration):
    dependencies = [("core", "0034_alter_ticket_state")]

    operations = [
        migrations.AddField(
            model_name="mergeclear",
            name="host_kind",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.RunPython(backfill_host_kind, clear_host_kind),
    ]
