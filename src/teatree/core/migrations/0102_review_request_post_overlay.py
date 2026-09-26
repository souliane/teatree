"""Attribute review-request posts to one overlay when the MR ledger proves it."""

from django.db import migrations, models


def _backfill_overlay(apps, schema_editor) -> None:
    review_request_post = apps.get_model("core", "ReviewRequestPost")
    pull_request = apps.get_model("core", "PullRequest")
    review_assignment = apps.get_model("core", "ReviewAssignment")
    using = schema_editor.connection.alias

    for post in review_request_post.objects.using(using).filter(overlay__isnull=True).iterator():
        candidates: set[str] = set()
        rows = pull_request.objects.using(using).filter(url=post.mr_url).values_list("overlay", "ticket__overlay")
        for pr_overlay, ticket_overlay in rows:
            candidates.update(value for value in (pr_overlay, ticket_overlay) if value)
        candidates.update(
            value
            for value in review_assignment.objects.using(using)
            .filter(mr_url=post.mr_url)
            .values_list("overlay", flat=True)
            if value
        )
        if len(candidates) == 1:
            review_request_post.objects.using(using).filter(pk=post.pk).update(overlay=candidates.pop())


class Migration(migrations.Migration):
    dependencies = [("core", "0101_reviewrequestpost_last_nag_reply_ts")]

    operations = [
        migrations.AddField(
            model_name="reviewrequestpost",
            name="overlay",
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
        migrations.RunPython(_backfill_overlay, migrations.RunPython.noop),
    ]
