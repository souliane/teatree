from django.db import migrations

from teatree.config.retired_settings import RENAMED_SETTING_KEYS

_OLD_KEY = "headless_max_turns"


def _carry_configured_values(apps, schema_editor):
    """Move every configured turn ceiling onto the unqualified key.

    A rename must MIGRATE rather than silently revert an operator to the default
    (#3527) — the box this lands on has the row set to 400, four turns' worth of
    deliberate opinion that a dropped row would return to 250. Each row keeps its
    scope, so global and per-overlay rows move independently, and a row already
    present under the new key WINS, which makes a re-run a no-op.
    """
    ConfigSetting = apps.get_model("core", "ConfigSetting")
    new_key = RENAMED_SETTING_KEYS[_OLD_KEY]
    for row in ConfigSetting.objects.filter(key=_OLD_KEY):
        if not ConfigSetting.objects.filter(scope=row.scope, key=new_key).exists():
            ConfigSetting.objects.create(
                scope=row.scope,
                key=new_key,
                value=row.value,
                seeded_by=row.seeded_by,
                seed_value=row.seed_value,
            )
        row.delete()


def _restore_qualified_key(apps, schema_editor):
    ConfigSetting = apps.get_model("core", "ConfigSetting")
    ConfigSetting.objects.filter(key=RENAMED_SETTING_KEYS[_OLD_KEY]).update(key=_OLD_KEY)


class Migration(migrations.Migration):
    dependencies = [("core", "0067_drop_execution_target")]

    operations = [migrations.RunPython(_carry_configured_values, _restore_qualified_key)]
