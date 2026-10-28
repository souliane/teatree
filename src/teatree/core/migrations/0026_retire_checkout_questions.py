from django.apps.registry import Apps
from django.db import migrations
from django.utils import timezone


def retire_checkout_cards(apps: Apps, _schema_editor: object) -> None:
    questions = apps.get_model("core", "DeferredQuestion")
    questions.objects.filter(
        evidence__action__kind="attest", answered_at__isnull=True, dismissed_at__isnull=True
    ).update(dismissed_at=timezone.now(), dismissed_reason="Factory checkout safety now decides this state")


class Migration(migrations.Migration):
    dependencies = [("core", "0025_ticketdmthread_ticketdmpost")]
    operations = [migrations.RunPython(retire_checkout_cards, migrations.RunPython.noop)]
