"""Refresh the ``housekeeping`` loop description on ALREADY-migrated databases (#3841).

The loop now also reconciles the ticket board against forge truth, but the seed's
description backfill only fills a BLANK ``description`` — by design, so an operator
who rewrote one keeps it. A deployed box therefore keeps the stale text forever and
``t3 loops list`` under-reports what the loop does.

Matching on the exact superseded string is what makes that safe: an operator-edited
description does not match, so it is never clobbered. Idempotent and re-runnable —
a second pass matches nothing. ``backward`` restores the superseded text on the same
exact-match basis.
"""

from django.db import migrations

_LOOP = "housekeeping"
_SUPERSEDED = (
    "Fast-forwards the editable teatree and overlay installs (self-update) and pulls each overlay's main clone hourly."
)
_CURRENT = (
    "Fast-forwards the editable teatree and overlay installs (self-update), "
    "pulls each overlay's main clone, and reconciles the ticket board against forge truth, hourly."
)


def _swap(apps, schema_editor, *, old: str, new: str) -> None:
    loop = apps.get_model("core", "Loop")
    loop.objects.using(schema_editor.connection.alias).filter(name=_LOOP, description=old).update(description=new)


def forward(apps, schema_editor) -> None:
    _swap(apps, schema_editor, old=_SUPERSEDED, new=_CURRENT)


def backward(apps, schema_editor) -> None:
    _swap(apps, schema_editor, old=_CURRENT, new=_SUPERSEDED)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_taskattempt_park_repeats"),
    ]

    operations = [migrations.RunPython(forward, backward)]
