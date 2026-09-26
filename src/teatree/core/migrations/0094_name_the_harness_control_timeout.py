"""Name the SDK control-request timeout, then re-read the stored rows.

A CLI child that never answers the harness's control request arrives as a raw traceback,
so ``classify_failure`` matched it to ``HARNESS_CRASH`` — environmental, which reads as
transient. 53 of 60 tasks on the deployed box died that way and the queue reported nothing
worth acting on for days. The kind is a pure function of the recorded reason, so every
stored row is re-read through the current vocabulary rather than left naming a verdict the
classifier no longer reaches.

Reads only the reason already stored; nothing is invented and no blank reason gains one.
"""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

from teatree.core.modelkit.task_failure_taxonomy import classify_failure

_BATCH = 500


def _reclassify(model: type[models.Model], *, text_field: str) -> None:
    """Re-read every row's stored *text_field* through the current vocabulary."""
    changed = []
    for row in model.objects.exclude(**{text_field: ""}).iterator(chunk_size=_BATCH):
        kind = classify_failure(getattr(row, text_field))
        # A historical model is a runtime-built `Model`, so its fields resolve statically nowhere.
        if kind == row.failure_kind:  # ty: ignore[unresolved-attribute]
            continue
        row.failure_kind = kind  # ty: ignore[unresolved-attribute]
        changed.append(row)
        if len(changed) >= _BATCH:
            model.objects.bulk_update(changed, ["failure_kind"])
            changed.clear()
    if changed:
        model.objects.bulk_update(changed, ["failure_kind"])


def reclassify(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _reclassify(apps.get_model("core", "Task"), text_field="failure_reason")
    _reclassify(apps.get_model("core", "TaskAttempt"), text_field="error")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0093_a_config_write_records_who_decided_it"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="failure_kind",
            field=models.CharField(
                blank=True,
                choices=[
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
                    ("cancelled", "Cancelled by an operator"),
                    ("superseded", "Superseded by rework"),
                    ("agent_abandoned", "Agent failed the task without a reason"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="taskattempt",
            name="failure_kind",
            field=models.CharField(
                blank=True,
                choices=[
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
                    ("cancelled", "Cancelled by an operator"),
                    ("superseded", "Superseded by rework"),
                    ("agent_abandoned", "Agent failed the task without a reason"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.RunPython(reclassify, migrations.RunPython.noop, elidable=True),
    ]
