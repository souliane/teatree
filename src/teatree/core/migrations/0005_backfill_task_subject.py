import re

from django.db import migrations


def _ticket_number(issue_url: str, pk: int) -> str:
    match = re.search(r"(\d+)$", issue_url or "")
    if match and match.group(1) != "0":
        return match.group(1)
    return str(pk)


def _derive_subject(task) -> str:
    ticket = task.ticket
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    issue_title = extra.get("issue_title", "")
    issue_title = issue_title if isinstance(issue_title, str) else ""
    title = (ticket.short_description or issue_title).strip()
    number = _ticket_number(ticket.issue_url, ticket.pk)
    if title:
        return f"#{number} {title}"[:120]
    if task.phase:
        return f"#{number} {task.phase}"[:120]
    return f"#{number}"


def backfill_subject(apps, schema_editor):
    Task = apps.get_model("core", "Task")
    for task in Task.objects.filter(subject="").select_related("ticket").iterator():
        task.subject = _derive_subject(task)
        task.save(update_fields=["subject"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_task_created_at_task_subject"),
    ]

    operations = [
        migrations.RunPython(backfill_subject, noop_reverse),
    ]
