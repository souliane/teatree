from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_task_parent_task"),
    ]

    operations = [
        migrations.AlterModelTable(
            name="session",
            table="teetree_session",
        ),
        migrations.AlterModelTable(
            name="task",
            table="teetree_task",
        ),
        migrations.AlterModelTable(
            name="taskattempt",
            table="teetree_taskattempt",
        ),
        migrations.AlterModelTable(
            name="ticket",
            table="teetree_ticket",
        ),
        migrations.AlterModelTable(
            name="worktree",
            table="teetree_worktree",
        ),
    ]
