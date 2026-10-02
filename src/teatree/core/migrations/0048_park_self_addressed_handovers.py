"""Park the hand-offs addressed to their own author, which no session can claim (#3821).

``SessionHandover.objects.claimable_for`` admits only the session named by
``to_session`` and excludes the session named by ``from_session``. A row where
those are equal is therefore claimable by no possible session id — it is not
pending on anyone, it is unreachable, while still counting as pending in every
tally of the unclaimed queue.

Creation now refuses such a row. This forward covers the ones already persisted:
each holds a payload some session wrote, and un-stranding it is the only way that
work stops being terminal. Parking (``to_session = ""``) hands it to whichever
session starts next, which is what the author would have got by omitting the
target. Only UNCLAIMED rows are touched — a claimed one was already delivered,
and re-parking it would re-inject state a session has seen.
"""

from django.db import migrations, models


def park_self_addressed(apps, schema_editor) -> None:
    handover = apps.get_model("core", "SessionHandover")
    handover.objects.filter(claimed_at__isnull=True, to_session=models.F("from_session")).exclude(
        from_session=""
    ).update(to_session="")


class Migration(migrations.Migration):
    dependencies = [("core", "0047_alter_pullrequest_state")]

    operations = [migrations.RunPython(park_self_addressed, migrations.RunPython.noop)]
