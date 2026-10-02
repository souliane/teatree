"""Move a stored ``venv_idle_days`` row onto ``artifact_idle_days``.

The #3527 contract: a RENAMED setting migrates its stored value, it does not silently
revert an operator to the default. The alias in ``retired_settings`` is the safety net;
this is the mechanism (0027 precedent).

The move is a RENAME in place, so each row keeps its scope — a global and a per-overlay row
migrate independently — along with its value and its seed provenance, with no field left to
forget. A scope that already holds the new key WINS (the canonical key is authoritative), so
its old row is dropped rather than clobbering a deliberate newer opinion; re-running is a
no-op either way.

Every query names the connection being migrated and works through the queryset rather than
``row.save()``/``row.delete()`` (which re-ask the router): ``ConfigSettingRouter`` pins the
model to the install-wide DB it also refuses to migrate, so a router-selected manager here
would rewrite the operator's real config store from a worktree's isolated one.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

_OLD_KEY = "venv_idle_days"
_NEW_KEY = "artifact_idle_days"


def carry_configured_value(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    rows = config_setting.objects.using(schema_editor.connection.alias)
    already_answered = set(rows.filter(key=_NEW_KEY).values_list("scope", flat=True))
    rows.filter(key=_OLD_KEY).exclude(scope__in=already_answered).update(key=_NEW_KEY)
    rows.filter(key=_OLD_KEY).delete()


def restore_the_venv_only_key(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    rows = config_setting.objects.using(schema_editor.connection.alias)
    already_answered = set(rows.filter(key=_OLD_KEY).values_list("scope", flat=True))
    rows.filter(key=_NEW_KEY).exclude(scope__in=already_answered).update(key=_OLD_KEY)
    rows.filter(key=_NEW_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0098_a_loss_free_sweep_keeps_its_own_plan")]

    operations = [migrations.RunPython(carry_configured_value, restore_the_venv_only_key)]
