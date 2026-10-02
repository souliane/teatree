"""Move each ``<credential>_pass_key`` nested in the ``overlays`` registry onto its own row.

A pass key is a per-venue setting with one override mechanism: a ``ConfigSetting`` row in
the overlay's scope. A value nested in the registry entry is no longer read, so it is moved
rather than dropped. A row already set for that overlay WINS and the nested copy is removed.

Every query names the connection being migrated (0099 precedent): ``ConfigSettingRouter`` pins
the model to the install-wide DB, which a worktree's isolated migrate must never rewrite.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

_REGISTRY = "overlays"
_SUFFIX = "_pass_key"
_MIGRATION_SEEDER = "migration:0104_move_overlay_registry_pass_keys_onto_settings"


def _is_pass_key(key: str) -> bool:
    return len(key) > len(_SUFFIX) and key.endswith(_SUFFIX)


def move_onto_settings(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    registry = rows.filter(scope="", key=_REGISTRY)
    overlays = registry.values_list("value", flat=True).first()
    if not isinstance(overlays, dict):
        return
    already_set = set(rows.exclude(scope="").values_list("scope", "key"))
    for overlay, entry in overlays.items():
        if not isinstance(entry, dict):
            continue
        for key in [key for key in entry if _is_pass_key(key)]:
            value = entry.pop(key)
            if (overlay, key) not in already_set:
                rows.create(
                    scope=overlay,
                    key=key,
                    value=value,
                    seeded_by=_MIGRATION_SEEDER,
                    seed_value=value,
                )
    registry.update(value=overlays)


def nest_back_into_registry(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    rows = apps.get_model("core", "ConfigSetting").objects.using(schema_editor.connection.alias)
    registry = rows.filter(scope="", key=_REGISTRY)
    overlays = registry.values_list("value", flat=True).first()
    if not isinstance(overlays, dict):
        return
    moved = rows.filter(seeded_by=_MIGRATION_SEEDER).exclude(scope="")
    for pk, scope, key, value, seed_value in moved.values_list("pk", "scope", "key", "value", "seed_value"):
        if value == seed_value and _is_pass_key(key) and isinstance(entry := overlays.get(scope), dict):
            entry[key] = value
            rows.filter(pk=pk).delete()
    registry.update(value=overlays)


class Migration(migrations.Migration):
    dependencies = [("core", "0103_refresh_issue_disposition_description")]

    operations = [migrations.RunPython(move_onto_settings, nest_back_into_registry)]
