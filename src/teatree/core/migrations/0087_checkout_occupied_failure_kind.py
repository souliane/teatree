"""Name the occupied-checkout refusal, and re-name the rows that recorded it before the name existed.

``failure_kind`` is a stored derivation of ``failure_reason``/``error``, and the #2009 stall
filters read the STORED kind off the attempt. Left at ``unclassified`` these refusals still
fingerprint-collide — the path, the holder and the lease window all normalize away — so two
in a row park a ticket that is merely waiting for a checkout. The backfill is scoped to rows
already ``unclassified`` (the vocabulary had no name for them), so no row that was
successfully classified is re-derived here.
"""

from django.db import migrations, models

_UNCLASSIFIED = "unclassified"
_CHECKOUT_OCCUPIED = "checkout_occupied"
#: The refusal's pre-prefix wording, which is all a historical row carries. Kept in step with
#: ``occupancy._occupied_error`` by ``tests/teatree_core/modelkit/test_task_failure_taxonomy.py``.
_PHRASE = "is already occupied by"

_KIND_CHOICES = [
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
    ("outage", "Network or API outage"),
    ("result_error", "Run ended without a clean result"),
    ("result_schema_invalid", "Result envelope violated the schema"),
    ("provision_failed", "Worktree provisioning failed"),
    ("landing_unverified", "Work never landed"),
    ("no_result_envelope", "No result envelope produced"),
    ("evidence_missing", "Required evidence missing"),
    ("recording_refused", "Recording refused by a gate"),
    ("plan_missing", "No plan recorded before an implementing dispatch"),
    ("cancelled", "Cancelled by an operator"),
    ("superseded", "Superseded by rework"),
    ("agent_abandoned", "Agent failed the task without a reason"),
    ("checkout_occupied", "Checkout held by another agent"),
]


def _rename_unclassified_occupancy_refusals(apps, schema_editor) -> None:
    apps.get_model("core", "Task").objects.filter(failure_kind=_UNCLASSIFIED, failure_reason__contains=_PHRASE).update(
        failure_kind=_CHECKOUT_OCCUPIED
    )
    apps.get_model("core", "TaskAttempt").objects.filter(failure_kind=_UNCLASSIFIED, error__contains=_PHRASE).update(
        failure_kind=_CHECKOUT_OCCUPIED
    )


def _restore_unclassified(apps, schema_editor) -> None:
    apps.get_model("core", "Task").objects.filter(failure_kind=_CHECKOUT_OCCUPIED).update(failure_kind=_UNCLASSIFIED)
    apps.get_model("core", "TaskAttempt").objects.filter(failure_kind=_CHECKOUT_OCCUPIED).update(
        failure_kind=_UNCLASSIFIED
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0086_anthropictokenusage_token_fingerprint"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="failure_kind",
            field=models.CharField(blank=True, choices=_KIND_CHOICES, default="", max_length=32),
        ),
        migrations.AlterField(
            model_name="taskattempt",
            name="failure_kind",
            field=models.CharField(blank=True, choices=_KIND_CHOICES, default="", max_length=32),
        ),
        migrations.RunPython(_rename_unclassified_occupancy_refusals, _restore_unclassified),
    ]
