from django.apps.registry import Apps
from django.db import migrations

OLD = (
    "Sweeps the owner's DM threads hourly and resolves the ones that no longer need them (owner "
    "already replied, subject merged/closed, duplicate of an open thread); leaves anything "
    "older than a day for the resurfacing side, and says nothing when it resolved nothing."
)
NEW = (
    "Sweeps the owner's DM threads hourly and resolves the ones that no longer need them "
    "(subject merged/closed, duplicate of an open thread); leaves anything older than a day "
    "for the resurfacing side, and says nothing when it resolved nothing."
)


def update_description(apps: Apps, _schema_editor: object) -> None:
    apps.get_model("core", "Loop").objects.filter(name="dm_sweep", description=OLD).update(description=NEW)


def restore_description(apps: Apps, _schema_editor: object) -> None:
    apps.get_model("core", "Loop").objects.filter(name="dm_sweep", description=NEW).update(description=OLD)


class Migration(migrations.Migration):
    dependencies = [("core", "0031_review_hold_disposition_and_merge_attribution")]
    operations = [migrations.RunPython(update_description, restore_description)]
