import re

from django.db import migrations, models


def _ticket_number(issue_url: str, pk: int) -> str:
    match = re.search(r"(\d+)$", issue_url or "")
    if match and match.group(1) != "0":
        return match.group(1)
    return str(pk)


def backfill_compose_project(apps, schema_editor):
    """Freeze each existing worktree's compose project at its RUNNING stack name.

    Before this field, ``compose_project`` was derived live as
    ``<repo_path>-wt<ticket_number>`` — the name every already-running docker
    stack carries. Storing it verbatim means the pk-scheme cutover applies only
    to NEW worktrees: an existing stack is never renamed and so never orphaned
    (the live-stack-compat concern #2774 deferred). An explicit value is left
    untouched.
    """
    worktree_model = apps.get_model("core", "Worktree")
    for wt in worktree_model.objects.filter(compose_project="").select_related("ticket").iterator():
        ticket = wt.ticket
        number = _ticket_number(ticket.issue_url, ticket.pk)
        wt.compose_project = f"{wt.repo_path}-wt{number}"
        wt.save(update_fields=["compose_project"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_seed_loop_descriptions"),
    ]

    operations = [
        migrations.AddField(
            model_name="worktree",
            name="compose_project",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(backfill_compose_project, noop_reverse),
    ]
