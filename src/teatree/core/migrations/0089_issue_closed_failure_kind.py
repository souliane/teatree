"""Name the closed-issue dispatch refusal (souliane/teatree#2663).

Choices-only, with no backfill — deliberately, unlike its ``plan_missing`` sibling
(0081), which had to re-name historical rows. The ``issue_closed: `` prefix has
never been emitted before this migration, so no stored row can be carrying it
misclassified as ``unclassified``; a backfill here would match nothing and only
obscure that the two cases differ.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0088_unflag_misattributed_compliance_recurrences"),
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
                    ("outage", "Network or API outage"),
                    ("result_error", "Run ended without a clean result"),
                    ("result_schema_invalid", "Result envelope violated the schema"),
                    ("provision_failed", "Worktree provisioning failed"),
                    ("landing_unverified", "Work never landed"),
                    ("no_result_envelope", "No result envelope produced"),
                    ("evidence_missing", "Required evidence missing"),
                    ("recording_refused", "Recording refused by a gate"),
                    ("plan_missing", "No plan recorded before an implementing dispatch"),
                    ("issue_closed", "Issue already closed on the forge"),
                    ("cancelled", "Cancelled by an operator"),
                    ("superseded", "Superseded by rework"),
                    ("agent_abandoned", "Agent failed the task without a reason"),
                    ("head_superseded", "PR head advanced past the reviewed tree"),
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
                    ("outage", "Network or API outage"),
                    ("result_error", "Run ended without a clean result"),
                    ("result_schema_invalid", "Result envelope violated the schema"),
                    ("provision_failed", "Worktree provisioning failed"),
                    ("landing_unverified", "Work never landed"),
                    ("no_result_envelope", "No result envelope produced"),
                    ("evidence_missing", "Required evidence missing"),
                    ("recording_refused", "Recording refused by a gate"),
                    ("plan_missing", "No plan recorded before an implementing dispatch"),
                    ("issue_closed", "Issue already closed on the forge"),
                    ("cancelled", "Cancelled by an operator"),
                    ("superseded", "Superseded by rework"),
                    ("agent_abandoned", "Agent failed the task without a reason"),
                    ("head_superseded", "PR head advanced past the reviewed tree"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
