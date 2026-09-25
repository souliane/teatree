"""Grandfather the escalation counts accrued before the ladder was bounded (#4748).

#4706 turned an escalation into a step toward dismissal. Every count already on a row
was stamped under the opposite rule — an escalation explicitly resolved nothing — so
reading those counts as rungs dismisses a legacy row on the FIRST sweep after deploy,
with no ask under the new semantics. Measured on the real backlog: a row at ``count=9``
produced ``expired=1`` immediately, against ~105 rows in that shape.

Stamping the base at migration time IS the epoch: every row that exists now accrued all
of its escalations before the bound, and every escalation after this runs is a rung on
the bounded ladder. ``escalated_at`` is left alone so the re-ask window still paces the
fresh asks instead of flooding the owner with the whole backlog at once.

Rows already dismissed under the reinterpretation are not resurrected here — a blanket
reopen re-floods the queue with rows the sweep may well have been right about. They are
listed by ``t3 <overlay> questions list --all`` and reopened one at a time with
``t3 <overlay> questions reopen``.
"""

from django.db import migrations, models


def grandfather_pre_bound_escalations(apps, schema_editor) -> None:
    question = apps.get_model("core", "DeferredQuestion")
    question.objects.filter(escalation_count__gt=0).update(escalation_base=models.F("escalation_count"))


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0092_taskattempt_taskattempt_recent_ended"),
    ]

    operations = [
        migrations.AddField(
            model_name="deferredquestion",
            name="escalation_base",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(grandfather_pre_bound_escalations, migrations.RunPython.noop),
    ]
