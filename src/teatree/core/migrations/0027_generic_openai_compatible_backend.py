from django.db import migrations

from teatree.config.retired_settings import RENAMED_SETTING_KEYS

# The #3666 retirements this migration carries, and the provider VALUE that moved
# with them. Read off the one retired-settings registry rather than re-listed, so a
# renamed key cannot be recorded there and forgotten here.
_BACKEND_KEYS = ("orca_router_pass_path", "orca_router_name", "orca_router_lane")
_PROVIDER_KEY = "agent_harness_provider"
_OLD_PROVIDER = "orca_router_byok"
_NEW_PROVIDER = "openai_compatible"


def _carry_configured_values(apps, schema_editor):
    """Move every configured provider-specific row onto its generic setting.

    The whole point of #3527: a renamed setting must MIGRATE, never silently revert
    an operator to a default. Each row keeps its scope (global and per-overlay rows
    move independently) and its seed provenance. A row already present under the new
    key WINS — the canonical key is authoritative — so the old row is dropped rather
    than clobbering a deliberate newer opinion, and re-running the migration is a
    no-op.

    Every query names the connection being migrated, the removal through the queryset
    rather than ``row.delete()`` (which re-asks the router). ``ConfigSettingRouter``
    pins the model to the canonical install-wide DB it also refuses to migrate, so a
    router-selected manager here rewrote the operator's real config store from a
    worktree's isolated one.
    """
    ConfigSetting = apps.get_model("core", "ConfigSetting")
    rows = ConfigSetting.objects.using(schema_editor.connection.alias)
    for old_key in _BACKEND_KEYS:
        new_key = RENAMED_SETTING_KEYS[old_key]
        for row in rows.filter(key=old_key):
            if not rows.filter(scope=row.scope, key=new_key).exists():
                rows.create(
                    scope=row.scope,
                    key=new_key,
                    value=row.value,
                    seeded_by=row.seeded_by,
                    seed_value=row.seed_value,
                )
            rows.filter(pk=row.pk).delete()
    rows.filter(key=_PROVIDER_KEY, value=_OLD_PROVIDER).update(value=_NEW_PROVIDER)


def _restore_provider_specific_values(apps, schema_editor):
    ConfigSetting = apps.get_model("core", "ConfigSetting")
    rows = ConfigSetting.objects.using(schema_editor.connection.alias)
    for old_key in _BACKEND_KEYS:
        rows.filter(key=RENAMED_SETTING_KEYS[old_key]).update(key=old_key)
    rows.filter(key=_PROVIDER_KEY, value=_NEW_PROVIDER).update(value=_OLD_PROVIDER)


class Migration(migrations.Migration):
    dependencies = [("core", "0026_rehome_reviewer_delivered_tickets")]

    operations = [migrations.RunPython(_carry_configured_values, _restore_provider_specific_values)]
