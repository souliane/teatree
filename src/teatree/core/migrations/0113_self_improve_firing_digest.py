import hashlib

from django.db import migrations, models


def backfill_firing_digests(apps, schema_editor):
    SelfImproveFiring = apps.get_model("core", "SelfImproveFiring")
    database = schema_editor.connection.alias
    last_pk = 0
    while rows := list(SelfImproveFiring.objects.using(database).filter(pk__gt=last_pk).order_by("pk")[:500]):
        for row in rows:
            row.dedup_key_digest = hashlib.sha256(row.dedup_key.encode()).hexdigest()[:16]
        SelfImproveFiring.objects.using(database).bulk_update(rows, ["dedup_key_digest"])
        last_pk = rows[-1].pk


class Migration(migrations.Migration):
    dependencies = [("core", "0112_merge_agent_routes_and_fire_anchor")]

    operations = [
        migrations.AddField(
            model_name="selfimprovefiring",
            name="dedup_key_digest",
            field=models.CharField(db_index=True, default="", editable=False, max_length=16),
        ),
        migrations.RunPython(backfill_firing_digests, migrations.RunPython.noop),
    ]
