from datetime import timedelta

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone

REDELIVERY_AGE_CUTOFF = timedelta(hours=72)
SENDING_STALE_AFTER = timedelta(seconds=300)
RECOVERABLE = ("noop", "failed")


def settle_stranded_info_backlog(apps, schema_editor):
    """Terminally EXPIRE the historic recoverable INFO backlog (#2064).

    Every recoverable INFO row older than the age cutoff is stale operator
    noise that must never re-deliver. Marking them EXPIRED in the same change
    drains ``recoverable_info`` to a quiet steady state — the scanner stops
    grinding the historic NOOP rows every tick. Fresh rows under the cutoff
    are left recoverable so the genuinely-stranded case keeps re-delivering.
    """
    bot_ping = apps.get_model("core", "BotPing")
    moment = timezone.now()
    age_cutoff = moment - REDELIVERY_AGE_CUTOFF
    stale_before = moment - SENDING_STALE_AFTER
    terminal = Q(status__in=RECOVERABLE)
    stale_claim = Q(status="sending", posted_at__lte=stale_before)
    bot_ping.objects.filter(kind="info").filter(terminal | stale_claim).filter(posted_at__lte=age_cutoff).update(
        status="expired",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0057_deferredquestion_mirror_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="botping",
            name="attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="botping",
            name="status",
            field=models.CharField(
                choices=[
                    ("sending", "Sending (delivery claimed, in flight)"),
                    ("sent", "Sent"),
                    ("noop", "Noop (no backend)"),
                    ("failed", "Failed"),
                    ("expired", "Expired (re-delivery abandoned)"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(settle_stranded_info_backlog, migrations.RunPython.noop),
    ]
