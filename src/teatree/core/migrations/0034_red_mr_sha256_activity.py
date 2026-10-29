from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_config_setting_change"),
    ]

    operations = [
        migrations.AlterField(
            model_name="redmrfixattempt",
            name="head_sha",
            field=models.CharField(max_length=82),
        ),
    ]
