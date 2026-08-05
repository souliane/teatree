"""One unclaimed hand-off per author, addressed to somebody who can claim it (#4194).

Three forwards then three constraints, in that order — a constraint added before
its data forward would fail to apply on any live DB holding the measured rows.

The forwards rescue what is already persisted. Rows 25-28 on the reported box were
addressed to ``"loop-runner"``, the ``t3 worker``'s durable principal:
``claimable_for`` admits ``to_session == session_id`` or ``to_session == ""`` and a
receiving session's id is a UUID, so those rows were claimable by nobody while
counting as pending forever — the same unreachable shape ``0048`` was written to
park, reached by a route its guard did not cover.

The duplicate collapse CONCATENATES; it never drops a payload. Absorbing a later
hand-off into the earlier row is a NEW contract, and the authors of these rows never
opted into it — one session's three rows open by stating that all three must be read.
So the surviving row carries every sibling's payload behind its own fence and the
siblings' rows (not their content) are what gets removed.
"""

from django.db import migrations, models

#: Frozen against :data:`teatree.core.session_identity.LOOP_RUNNER_SESSION_ID`. A
#: migration must not import runtime code, so the literal is repeated here and the
#: two are pinned in sync by ``tests/teatree_core/migrations/``.
_LOOP_RUNNER_SESSION_ID = "loop-runner"


def park_loop_runner_targets(apps, schema_editor) -> None:
    handover = apps.get_model("core", "SessionHandover")
    handover.objects.filter(claimed_at__isnull=True, to_session=_LOOP_RUNNER_SESSION_ID).update(to_session="")


def park_self_addressed(apps, schema_editor) -> None:
    """``0048``'s forward, repeated so the check constraint applies on any DB a raw create slipped one into."""
    handover = apps.get_model("core", "SessionHandover")
    handover.objects.filter(claimed_at__isnull=True, to_session=models.F("from_session")).exclude(
        from_session=""
    ).update(to_session="")


def collapse_duplicate_unclaimed(apps, schema_editor) -> None:
    handover = apps.get_model("core", "SessionHandover")
    unclaimed = handover.objects.filter(claimed_at__isnull=True)
    authors = [
        row["from_session"]
        for row in unclaimed.values("from_session").annotate(rows=models.Count("pk")).filter(rows__gt=1)
    ]
    for author in authors:
        siblings = list(unclaimed.filter(from_session=author).order_by("created_at", "id"))
        survivor = min(siblings, key=lambda row: row.pk)
        newest = siblings[-1]
        survivor.payload = "\n\n".join(
            f"## Hand-off {index} of {len(siblings)} — from `{row.from_session}` "
            f"at {row.created_at.isoformat()}\n\n{row.payload}"
            for index, row in enumerate(siblings, start=1)
        )
        survivor.created_at = newest.created_at
        survivor.to_session = newest.to_session
        survivor.save(update_fields=["payload", "created_at", "to_session"])
        handover.objects.filter(pk__in=[row.pk for row in siblings if row.pk != survivor.pk]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0058_merge_vendor_sync_and_availability_removal")]

    operations = [
        migrations.RunPython(park_loop_runner_targets, migrations.RunPython.noop),
        migrations.RunPython(park_self_addressed, migrations.RunPython.noop),
        migrations.RunPython(collapse_duplicate_unclaimed, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="sessionhandover",
            constraint=models.UniqueConstraint(
                condition=models.Q(("claimed_at__isnull", True)),
                fields=("from_session",),
                name="uniq_unclaimed_handover_per_from_session",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionhandover",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("claimed_at__isnull", False))
                    | models.Q(("to_session", ""))
                    | ~models.Q(("to_session", models.F("from_session")))
                ),
                name="ck_handover_target_not_self",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionhandover",
            constraint=models.CheckConstraint(
                condition=models.Q(("claimed_at__isnull", False)) | ~models.Q(("to_session", _LOOP_RUNNER_SESSION_ID)),
                name="ck_handover_target_not_loop_runner",
            ),
        ),
    ]
