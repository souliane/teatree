from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_config_setting_change"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourcepressuremarker",
            name="last_dead_worktree_scan_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
