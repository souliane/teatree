"""Normalize reviewer identity + make (slug, pr, sha, reviewer) unique (F8).

Free-text ``reviewer_identity`` made "has this sha been reviewed by this
reviewer?" unanswerable, so one sha was reviewed 17 times under 14 spellings
(66% of all verdicts were duplicates). This adds the normalized idempotency
column, backfills it for existing rows, dedups the historical duplicates
keeping the NEWEST verdict per key (the only one the newest-wins effective
state ever reads — non-lossy), then adds the unique constraint the new
``record`` writes idempotently against.

Idempotent: re-running normalizes to the same value and finds no remaining
duplicates. The dedup runs BEFORE ``AddConstraint`` so the constraint can land
on a database that already holds the duplicate rows.
"""

from django.db import migrations, models


def _normalize(identity: str) -> str:
    # Frozen snapshot of teatree.core.models.review_verdict.normalize_reviewer_identity:
    # strip + collapse internal whitespace runs + casefold. Duplicated here on
    # purpose — a migration is a point-in-time transform and must not drift with
    # the live function.
    return " ".join((identity or "").split()).casefold()


def _backfill_and_dedup(apps, schema_editor):
    review_verdict = apps.get_model("core", "reviewverdict")

    for row in review_verdict.objects.all().only("pk", "reviewer_identity", "reviewer_identity_normalized"):
        normalized = _normalize(row.reviewer_identity)
        if row.reviewer_identity_normalized != normalized:
            review_verdict.objects.filter(pk=row.pk).update(reviewer_identity_normalized=normalized)

    seen: set[tuple[str, int, str, str]] = set()
    to_delete: list[int] = []
    ordered = review_verdict.objects.order_by("-recorded_at", "-pk").only(
        "pk", "slug", "pr_id", "reviewed_sha", "reviewer_identity_normalized"
    )
    for row in ordered:
        key = (row.slug, row.pr_id, row.reviewed_sha, row.reviewer_identity_normalized)
        if key in seen:
            to_delete.append(row.pk)
        else:
            seen.add(key)
    if to_delete:
        review_verdict.objects.filter(pk__in=to_delete).delete()


def _noop_reverse(apps, schema_editor):
    """The normalized column + dedup are not restored on reverse — data-only, safe to drop."""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_dm_sweep_loop_and_directive_cadence"),
    ]

    operations = [
        migrations.AddField(
            model_name="reviewverdict",
            name="reviewer_identity_normalized",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.RunPython(_backfill_and_dedup, _noop_reverse),
        migrations.AddConstraint(
            model_name="reviewverdict",
            constraint=models.UniqueConstraint(
                fields=("slug", "pr_id", "reviewed_sha", "reviewer_identity_normalized"),
                name="uniq_review_verdict_slug_pr_sha_reviewer",
            ),
        ),
    ]
