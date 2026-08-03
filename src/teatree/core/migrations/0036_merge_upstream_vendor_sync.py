"""Rejoin the two migration leaves the 11ad82f85..17e98927b vendor sync left behind.

Upstream and the fork both branched off ``0033_retire_pane_reaper_loop``: upstream
added ``0034_taskattempt_park_repeats``, the fork added ``0034_alter_ticket_state``
and ``0035_mergeclear_host_kind``. Two leaves fail ``django_linear_migrations``
(dlm.E005), which is what turned the whole suite red after the merge.

Rejoining rather than RENUMBERING the fork's two migrations is deliberate: both are
already applied in the deployed factory database, and renaming an applied migration
makes Django re-run it (``AddField`` onto a column that already exists). A no-op
merge leaves every recorded name intact, so a deployed box has only upstream's
``0034`` and this rejoin left to apply.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_taskattempt_park_repeats"),
        ("core", "0035_mergeclear_host_kind"),
    ]

    operations: list = []
