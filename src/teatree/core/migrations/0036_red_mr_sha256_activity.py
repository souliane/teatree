from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_merge_operator_grant_and_config_setting_change"),
    ]

    operations = [
        migrations.AlterField(
            model_name="redmrfixattempt",
            name="head_sha",
            field=models.CharField(max_length=82),
        ),
    ]
