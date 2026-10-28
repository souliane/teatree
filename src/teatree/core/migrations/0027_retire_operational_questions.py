from django.apps.registry import Apps
from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def retire_operational_cards(apps: Apps, _schema_editor: object) -> None:
    questions = apps.get_model("core", "DeferredQuestion")
    retired = (
        Q(dedupe_marker__startswith="owner-wait:held-pr:")
        | Q(dedupe_marker__startswith="review-unrecordable:")
        | Q(dedupe_marker__startswith="stuck-redispatch-halt:")
    )
    questions.objects.filter(retired, answered_at__isnull=True, dismissed_at__isnull=True).update(
        dismissed_at=timezone.now(), dismissed_reason="Automated workflow now resolves this state"
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0026_retire_checkout_questions")]
    operations = [migrations.RunPython(retire_operational_cards, migrations.RunPython.noop)]
