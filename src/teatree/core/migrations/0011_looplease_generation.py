from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_worktree_compose_project"),
    ]

    operations = [
        migrations.AddField(
            model_name="looplease",
            name="generation",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
