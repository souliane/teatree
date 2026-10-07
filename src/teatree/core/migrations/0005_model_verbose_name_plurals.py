from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_deferredquestion_resolved_via_agent"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="agentrouteavailability",
            options={"verbose_name_plural": "agent route availabilities"},
        ),
        migrations.AlterModelOptions(
            name="autoreviewdispatch",
            options={"ordering": ["-dispatched_at"], "verbose_name_plural": "auto review dispatches"},
        ),
        migrations.AlterModelOptions(
            name="consolidatedmemory",
            options={"ordering": ["-created_at"], "verbose_name_plural": "consolidated memories"},
        ),
        migrations.AlterModelOptions(
            name="criticdispatch",
            options={"ordering": ["-dispatched_at"], "verbose_name_plural": "critic dispatches"},
        ),
        migrations.AlterModelOptions(
            name="directivedispatch",
            options={"ordering": ["-dispatched_at"], "verbose_name_plural": "directive dispatches"},
        ),
        migrations.AlterModelOptions(
            name="interactivedispatch",
            options={"ordering": ["admitted_at"], "verbose_name_plural": "interactive dispatches"},
        ),
        migrations.AlterModelOptions(
            name="replydispatch",
            options={"ordering": ["-dispatched_at"], "verbose_name_plural": "reply dispatches"},
        ),
        migrations.AlterModelOptions(
            name="trustedidentity",
            options={"ordering": ["platform", "handle"], "verbose_name_plural": "trusted identities"},
        ),
    ]
