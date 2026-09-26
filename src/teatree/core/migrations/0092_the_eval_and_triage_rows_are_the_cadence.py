"""Fold two inner cadence gates onto the ``Loop`` rows that already owned the timer.

``eval_local`` fired a daily row that then asked "has a week passed?", and
``triage_assessor`` an hourly row that asked "has a day passed?". Two clocks in series
do not make a weekly or a daily job: a fire landing slightly early skips the whole
window, so each ran LATER every time rather than on its stated cadence.

A box that never stored either key ran on the retired defaults — weekly and daily — so
those land on the rows too; leaving the rows alone would quietly make ``eval_local`` daily
and ``triage_assessor`` hourly.
"""

from django.db import migrations

from teatree.core.migrations._cadence_fold import fold_cadence_or_default

FOLDS = (
    ("eval_local", "eval_local_cadence_hours", 168),
    ("triage_assessor", "triage_assessor_cadence_hours", 24),
)


class Migration(migrations.Migration):
    dependencies = [("core", "0091_a_loop_timer_takes_no_overlay_scope")]

    operations = [migrations.RunPython(fold_cadence_or_default(FOLDS), migrations.RunPython.noop)]
