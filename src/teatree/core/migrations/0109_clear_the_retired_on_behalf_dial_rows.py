"""Clear the stored rows under the on-behalf dial this branch retired.

``ConfigSetting.set_value`` validates overlay-scope honourability, unattended governed
writes and cross-key consistency — it does NOT refuse a removed key — so the rows the
dial was set through outlive the field. A surviving row resolves to nothing and makes
``retired_settings.warn_removed_setting`` print a loud stderr line naming the key on
EVERY resolution, and ``get_effective_settings`` sits on the statusline/hook/gate hot
path. Measured on the reviewer's box: two overlay-scope rows, one warning per call.

``0095`` restricted itself to rows holding what was the shipped default, so deleting
them provably changed no effective value. These rows hold ``immediate`` instead — and
the reasoning still lands, for a stronger reason: the field is GONE, so the row is
already inert and its value has no reader to change.

The key is a literal rather than a read of ``REMOVED_SETTING_KEYS``: a migration states
what it did to the rows that existed when it ran, and a later retirement must not
retroactively widen an applied cleanup (0027/0086/0095 precedent).
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

CLEARED_KEY = "on_behalf_post_mode"


def clear_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    config_setting = apps.get_model("core", "ConfigSetting")
    config_setting.objects.filter(key=CLEARED_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0108_review_unrecordable_failure_kind")]

    operations = [migrations.RunPython(clear_rows, migrations.RunPython.noop)]
