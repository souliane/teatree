from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0106_an_unmeasured_window_is_null_not_zero")]

    operations = [
        migrations.AddField(
            model_name="e2emandatoryrun",
            name="target",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("dev", "Dev"),
                    ("qa", "QA"),
                    ("local", "Local"),
                    ("stack", "Stack"),
                ],
                default="unknown",
                max_length=16,
            ),
        ),
    ]
