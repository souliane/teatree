"""Collapse the deleted ``agent_review_request_disabled`` flag into the autonomy tier.

souliane/teatree#2579 item 1: the parallel side flag
``agent_review_request_disabled`` is deleted; review-request blocking is now
driven off the ``Autonomy`` TIER (the ``notify`` tier BLOCKs review-request, the
``full`` tier PROCEEDs). This data migration maps every stale ``ConfigSetting``
row keyed ``agent_review_request_disabled`` forward so a pre-existing customer
overlay keeps its intent:

Forward, per scope holding a truthy flag:

- guard-set that scope's ``autonomy = notify`` ONLY when no higher tier is
    already pinned for the scope (never downgrades a ``full`` overlay; upgrades
    a ``babysit`` one; leaves a ``notify`` one as-is), then
- DELETE the stale ``agent_review_request_disabled`` row.

A falsy flag carries no intent (the default already PROCEEDs) — its row is simply
deleted, never written as an autonomy tier. Irreversible (a no-op reverse): the
deleted flag has no field to restore to once the source drops it.
"""

from django.db import migrations

_FLAG_KEY = "agent_review_request_disabled"
_AUTONOMY_KEY = "autonomy"
_NOTIFY = "notify"
# Tiers that are >= notify and must NOT be downgraded by the truthy flag.
_AT_OR_ABOVE_NOTIFY = frozenset({"notify", "full"})


def _collapse_flag(apps, schema_editor) -> None:
    config_model = apps.get_model("core", "ConfigSetting")
    stale = list(config_model.objects.filter(key=_FLAG_KEY))
    for row in stale:
        if row.value:
            existing = config_model.objects.filter(scope=row.scope, key=_AUTONOMY_KEY).first()
            current_tier = existing.value if existing is not None else None
            # Guard: never downgrade a higher-or-equal tier (full / notify already
            # block); only set notify when the scope has no tier at or above it.
            if current_tier not in _AT_OR_ABOVE_NOTIFY:
                config_model.objects.update_or_create(
                    scope=row.scope,
                    key=_AUTONOMY_KEY,
                    defaults={"value": _NOTIFY},
                )
    # Delete every stale flag row regardless of truthiness — the field is gone.
    config_model.objects.filter(key=_FLAG_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0092_remove_ticket_redis_db_index"),
    ]

    operations = [
        migrations.RunPython(_collapse_flag, migrations.RunPython.noop),
    ]
