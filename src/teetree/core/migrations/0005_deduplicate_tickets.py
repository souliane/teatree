"""Deduplicate Ticket rows sharing the same issue_url before adding a unique constraint."""

from django.db import connection, migrations


def deduplicate_tickets(apps, schema_editor):
    with connection.cursor() as cursor:
        cursor.execute("""
            DELETE FROM core_ticket
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM core_ticket
                WHERE issue_url != ''
                GROUP BY issue_url
            )
            AND issue_url != ''
        """)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_task_parent_task"),
    ]

    operations = [
        migrations.RunPython(deduplicate_tickets, migrations.RunPython.noop),
    ]
