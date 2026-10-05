"""Clear the stored rows under the on-behalf dial this branch retired.

``ConfigSetting.set_value`` validates overlay-scope honourability, unattended governed
writes and cross-key consistency — it does NOT refuse a removed key — so the rows the
dial was set through outlive the field. A surviving row resolves to nothing and makes
``get_effective_settings`` ignores it. Measured on the reviewer's box: two overlay-scope rows.

``0095`` restricted itself to rows holding what was the shipped default, so deleting
them provably changed no effective value. These rows hold ``immediate`` instead — and
the reasoning still lands, for a stronger reason: the field is GONE, so the row is
already inert and its value has no reader to change.

One value still has a reader to carry it to: a global "immediate" meant "post on my
behalf whatever the hour", and ``Mode.egress`` is now the only control over that voice.
Deleting the row alone would silence the box every evening under an ``afk`` or
``maintenance`` that forbids egress, so every preset is opened to ``allow`` first — the
same behaviour, stated on the one control that remains. An overlay-scoped "immediate"
cannot be expressed on a box-global preset and only logs.

The key is a literal: a migration states
what it did to the rows that existed when it ran, and a later retirement must not
retroactively widen an applied cleanup (0027/0086/0095 precedent).
"""

import logging

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

logger = logging.getLogger(__name__)

CLEARED_KEY = "on_behalf_post_mode"
_IMMEDIATE = "immediate"


def clear_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    db = schema_editor.connection.alias
    rows = apps.get_model("core", "ConfigSetting").objects.using(db).filter(key=CLEARED_KEY)
    if rows.filter(scope="", value=_IMMEDIATE).exists():
        apps.get_model("core", "Mode").objects.using(db).update(egress="allow")
    elif scoped := sorted(rows.filter(value=_IMMEDIATE).values_list("scope", flat=True)):
        logger.warning(
            "on_behalf_post_mode=immediate was set only for %s; a preset's egress is box-global, so it is not carried",
            ", ".join(scoped),
        )
    rows.delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0108_review_unrecordable_failure_kind")]

    operations = [migrations.RunPython(clear_rows, migrations.RunPython.noop)]
