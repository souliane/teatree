from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.state import StateApps
from django.db.models import Count


def refuse_lossy_rollback(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    ReviewVerdict = apps.get_model("core", "ReviewVerdict")
    duplicates = (
        ReviewVerdict.objects.using(schema_editor.connection.alias)
        .values("slug", "pr_id", "reviewed_sha", "reviewer_identity_normalized")
        .annotate(count=Count("pk"))
        .filter(count__gt=1)
    )
    if duplicates.exists():
        msg = "Review verdict rollback would discard distinct verdicts at the same head"
        raise IrreversibleError(msg)


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
        migrations.RunPython(migrations.RunPython.noop, refuse_lossy_rollback),
    ]
