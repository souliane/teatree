"""Name the plan-gate refusal, and re-name the rows that recorded it before the name existed.

``failure_kind`` is a stored derivation of ``failure_reason``/``error``, so adding a kind
leaves every historical row that would now carry it stale at ``unclassified`` — and the
#4578 drain sweep reads the KIND. The backfill is scoped to rows already ``unclassified``
(the vocabulary had no name for them), so no row that was successfully classified is
re-derived here.
"""

from django.db import migrations, models

_UNCLASSIFIED = "unclassified"
_PLAN_MISSING = "plan_missing"
#: The refusal's greppable prefix, kept in step with ``plan_dispatch_gate.PLAN_MISSING_PREFIX``
#: by ``tests/teatree_core/modelkit/test_task_failure_taxonomy.py``.
_PREFIX = "plan_missing: "


def _rename_unclassified_plan_refusals(apps, schema_editor) -> None:
    apps.get_model("core", "Task").objects.filter(
        failure_kind=_UNCLASSIFIED, failure_reason__startswith=_PREFIX
    ).update(failure_kind=_PLAN_MISSING)
    apps.get_model("core", "TaskAttempt").objects.filter(failure_kind=_UNCLASSIFIED, error__startswith=_PREFIX).update(
        failure_kind=_PLAN_MISSING
    )


def _restore_unclassified(apps, schema_editor) -> None:
    apps.get_model("core", "Task").objects.filter(failure_kind=_PLAN_MISSING).update(failure_kind=_UNCLASSIFIED)
    apps.get_model("core", "TaskAttempt").objects.filter(failure_kind=_PLAN_MISSING).update(failure_kind=_UNCLASSIFIED)


_CHOICES = [
    ("unrecorded", "No reason recorded"),
    ("unclassified", "Unclassified"),
    ("lease_lost", "Lease lost to another worker"),
    ("lease_expired", "Lease expired and was reaped"),
    ("runtime_ceiling", "Runtime ceiling exceeded"),
    ("usage_limit_parked", "Parked on a usage window"),
    ("credential_exhausted", "Credentials exhausted"),
    ("harness_config_invalid", "Invalid harness configuration"),
    ("harness_crash", "Harness crashed"),
    ("outage", "Network or API outage"),
    ("result_error", "Run ended without a clean result"),
    ("provision_failed", "Worktree provisioning failed"),
    ("landing_unverified", "Work never landed"),
    ("no_result_envelope", "No result envelope produced"),
    ("evidence_missing", "Required evidence missing"),
    ("recording_refused", "Recording refused by a gate"),
    ("plan_missing", "No plan recorded before an implementing dispatch"),
    ("cancelled", "Cancelled by an operator"),
    ("superseded", "Superseded by rework"),
    ("agent_abandoned", "Agent failed the task without a reason"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0080_worktree_occupancy_claim"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="failure_kind",
            field=models.CharField(blank=True, choices=_CHOICES, default="", max_length=32),
        ),
        migrations.AlterField(
            model_name="taskattempt",
            name="failure_kind",
            field=models.CharField(blank=True, choices=_CHOICES, default="", max_length=32),
        ),
        migrations.RunPython(_rename_unclassified_plan_refusals, _restore_unclassified),
    ]
