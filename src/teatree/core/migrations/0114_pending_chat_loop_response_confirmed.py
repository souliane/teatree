from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0113_self_improve_firing_digest")]

    operations = [
        migrations.AddField(
            model_name="pendingchatinjection",
            name="loop_response_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
