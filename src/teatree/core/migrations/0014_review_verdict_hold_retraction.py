from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0013_resume_refused")]

    operations = [
        migrations.RemoveConstraint(
            model_name="reviewverdict",
            name="uniq_review_verdict_slug_pr_sha_reviewer",
        ),
        migrations.AddConstraint(
            model_name="reviewverdict",
            constraint=models.UniqueConstraint(
                fields=("slug", "pr_id", "reviewed_sha", "reviewer_identity_normalized", "verdict"),
                name="uniq_review_verdict_slug_pr_sha_reviewer_verdict",
            ),
        ),
    ]
