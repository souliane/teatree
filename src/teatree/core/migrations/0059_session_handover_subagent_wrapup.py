"""The sub-agent wrap-up union, stored rather than re-parsed out of markdown (#4194).

Every ``handover create`` appended another ``## Sub-agent wrap-up`` section, so ten
hand-offs left the receiver ten sections — each a snapshot of a different moment,
which is the N-partially-contradictory-narratives problem the single row was built
to end. Rendering ONE section from a stored union needs the previous barrier's
per-agent records, and those existed only as rendered markdown inside ``payload``.

No data forward: an existing row's payload already carries whatever sections it
accumulated, and the default is the empty union, so its next hand-off starts the
union from that barrier rather than inventing records nothing can recover.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0058_one_unclaimed_handover_per_session"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionhandover",
            name="subagent_wrapup",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
