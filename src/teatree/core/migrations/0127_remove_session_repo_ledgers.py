"""Retire the session repo ledgers, the digest models, ``SendAudit.mode`` and ``PendingChatInjection.consumed_at``.

The reverse does not restore data; the only rollback is the pre-roll .backup with the previous image.
"""

from django.db import migrations, models
from django.utils import timezone


def delete_retired_digest_content_types(apps, schema_editor):
    try:
        content_type = apps.get_model("contenttypes", "ContentType")
        permission = apps.get_model("auth", "Permission")
    except LookupError:
        return

    alias = schema_editor.connection.alias
    content_types = content_type.objects.using(alias).filter(
        app_label="core", model__in=("dailydigestmessage", "dailydigestthread")
    )
    permission.objects.using(alias).filter(content_type__in=content_types).delete()
    content_types.delete()


def stamp_restored_chat_rows_consumed(apps, schema_editor):
    """Reverse only: the restored column is empty, which would replay every stored chat line as pending."""
    pending = apps.get_model("core", "PendingChatInjection")
    pending.objects.using(schema_editor.connection.alias).update(consumed_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0126_name_the_worker_generation_and_cursor_tables"),
    ]

    operations = [
        migrations.RemoveField(model_name="session", name="repos_modified"),
        migrations.RemoveField(model_name="session", name="repos_tested"),
        migrations.DeleteModel(name="DailyDigestMessage"),
        migrations.DeleteModel(name="DailyDigestThread"),
        migrations.AlterField(model_name="sendaudit", name="mode", field=models.CharField(max_length=16, default="")),
        migrations.RemoveField(model_name="sendaudit", name="mode"),
        migrations.AlterField(
            model_name="pendingchatinjection",
            name="consumed_at",
            field=models.DateTimeField(null=True, blank=True, default=None),
        ),
        migrations.RunPython(migrations.RunPython.noop, stamp_restored_chat_rows_consumed),
        migrations.RemoveField(model_name="pendingchatinjection", name="consumed_at"),
        migrations.RunPython(delete_retired_digest_content_types, migrations.RunPython.noop),
    ]
