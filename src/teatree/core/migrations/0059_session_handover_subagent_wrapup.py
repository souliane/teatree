"""The sub-agent barrier's returns as ROW STATE rather than payload text (#4194).

Every ``handover create`` appended another ``## Sub-agent wrap-up`` section, so ten
hand-offs left the receiver ten sections — each a snapshot of a different moment,
which is the N-partially-contradictory-narratives problem the single row was built
to end. Rendering ONE section needs the previous barrier's per-agent records, and
those existed only as rendered markdown inside ``payload``.

Splicing the section INTO the payload then had to decide which bytes were the
harness's own by matching marker text, so an authored body quoting a marker had that
region — or, with an unterminated quote, its whole tail — deleted. The union is
therefore joined by the barrier FACT itself: ``last_barrier_at`` records when a
barrier last completed on this row and ``barrier_ran_at_latest_handoff`` whether the
latest hand-off ran one, so the section can be rendered onto the delivery surface and
the payload never has to be read or edited to learn something about the harness.
``[]`` means "no agents"; NULL means "nobody looked" — conflating them is what made a
hand-off assert a barrier it never ran.

No data forward for any of the three: an existing row's payload already carries
whatever sections it accumulated, the defaults (empty union, NULL, false) are exactly
"nothing is known yet", and on ``main`` the barrier's returns were PRINTED only, so
no persisted payload has ever carried a block to migrate out.
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
        migrations.AddField(
            model_name="sessionhandover",
            name="last_barrier_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sessionhandover",
            name="barrier_ran_at_latest_handoff",
            field=models.BooleanField(default=False),
        ),
    ]
