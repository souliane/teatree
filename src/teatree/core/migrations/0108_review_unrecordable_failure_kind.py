"""Name the unrecordable-review refusal, and re-name the rows that recorded it before the name existed.

``failure_kind`` is a stored derivation of ``failure_reason``/``error``, so adding a kind
leaves every historical row that would now carry it stale at ``unclassified`` — and the
re-dispatch budget reads the KIND. The backfill is scoped to rows already ``unclassified``
(the vocabulary had no name for them), so no row that was successfully classified is
re-derived here.

The 53 historical rows predate the greppable prefix, so they are matched on the phrase the
recorder has always carried rather than on ``startswith``. The reverse restores every row
this kind now holds, prefix or not — the kind did not exist before this migration, so no
row can be carrying it for another reason.
"""

from django.db import migrations, models

_UNCLASSIFIED = "unclassified"
_REVIEW_UNRECORDABLE = "review_unrecordable"
#: The phrase both refusals carry, in the recorder since long before the prefix existed.
_HISTORICAL_PHRASE = "no pull request head is recorded"


def _rename_unclassified_unrecordable_refusals(apps, schema_editor) -> None:
    apps.get_model("core", "Task").objects.filter(
        failure_kind=_UNCLASSIFIED, failure_reason__contains=_HISTORICAL_PHRASE
    ).update(failure_kind=_REVIEW_UNRECORDABLE)
    apps.get_model("core", "TaskAttempt").objects.filter(
        failure_kind=_UNCLASSIFIED, error__contains=_HISTORICAL_PHRASE
    ).update(failure_kind=_REVIEW_UNRECORDABLE)


def _restore_unclassified(apps, schema_editor) -> None:
    apps.get_model("core", "Task").objects.filter(failure_kind=_REVIEW_UNRECORDABLE).update(failure_kind=_UNCLASSIFIED)
    apps.get_model("core", "TaskAttempt").objects.filter(failure_kind=_REVIEW_UNRECORDABLE).update(
        failure_kind=_UNCLASSIFIED
    )


_CHOICES = [
    ("unrecorded", "No reason recorded"),
    ("unclassified", "Unclassified"),
    ("lease_lost", "Lease lost to another worker"),
    ("lease_expired", "Lease expired and was reaped"),
    ("runtime_ceiling", "Runtime ceiling exceeded"),
    ("usage_limit_parked", "Halted on a usage window"),
    ("credential_exhausted", "Credentials exhausted"),
    ("credential_missing", "No credential configured"),
    ("harness_config_invalid", "Invalid harness configuration"),
    ("overlay_unknown", "Overlay not installed or misnamed"),
    ("harness_crash", "Harness crashed"),
    ("harness_control_timeout", "Harness control request timed out"),
    ("outage", "Network or API outage"),
    ("result_error", "Run ended without a clean result"),
    ("result_schema_invalid", "Result envelope violated the schema"),
    ("provision_failed", "Worktree provisioning failed"),
    ("landing_unverified", "Work never landed"),
    ("no_result_envelope", "No result envelope produced"),
    ("evidence_missing", "Required evidence missing"),
    ("recording_refused", "Recording refused by a gate"),
    ("plan_missing", "No plan recorded before an implementing dispatch"),
    ("review_unrecordable", "Review verdict could not be recorded"),
    ("cancelled", "Cancelled by an operator"),
    ("superseded", "Superseded by rework"),
    ("agent_abandoned", "Agent failed the task without a reason"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0107_e2emandatoryrun_target"),
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
        migrations.RunPython(_rename_unclassified_unrecordable_refusals, _restore_unclassified),
    ]
