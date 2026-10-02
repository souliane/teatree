from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_loopstate_forced_loopstate_forced_reason_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="dreamqaprobe",
            old_name="overlay",
            new_name="scope",
        ),
        migrations.AlterField(
            model_name="dreamqaprobe",
            name="scope",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
    ]
