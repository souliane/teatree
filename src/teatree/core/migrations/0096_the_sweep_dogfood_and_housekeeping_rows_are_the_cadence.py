"""Fold three more inner cadence gates onto the ``Loop`` rows that already owned the timer.

``backlog_sweep`` and ``dogfood_smoke`` fired a daily row that then asked "has a day
passed?", and ``self_update`` an hourly row that asked "has an hour passed?" — the same
two-clocks-in-series shape 0092 folded for ``eval_local`` and ``triage_assessor``, whose
keys were retired alongside these three and whose stored hours were moved, not dropped.
"""

from django.db import migrations

from teatree.core.migrations._cadence_fold import fold_cadence

FOLDS = (
    ("backlog_sweep", "backlog_sweep_cadence_hours"),
    ("dogfood", "dogfood_smoke_cadence_hours"),
    ("housekeeping", "self_update_cadence_hours"),
)


class Migration(migrations.Migration):
    dependencies = [("core", "0095_clear_retired_setting_rows")]

    operations = [migrations.RunPython(fold_cadence(FOLDS), migrations.RunPython.noop)]
