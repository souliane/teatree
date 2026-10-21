from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_review_verdict_identity_and_verdict_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="first_admitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunSQL(
            "UPDATE teatree_task SET first_admitted_at = admitted_at WHERE admitted_at IS NOT NULL",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
