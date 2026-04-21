import django_fsm
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_ticket_redis_db_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="state",
            field=django_fsm.FSMField(
                choices=[
                    ("not_started", "Not started"),
                    ("scoped", "Scoped"),
                    ("started", "Started"),
                    ("coded", "Coded"),
                    ("tested", "Tested"),
                    ("reviewed", "Reviewed"),
                    ("shipped", "Shipped"),
                    ("in_review", "In review"),
                    ("merged", "Merged"),
                    ("retrospected", "Retrospected"),
                    ("delivered", "Delivered"),
                    ("ignored", "Ignored"),
                ],
                default="not_started",
                max_length=32,
            ),
        ),
    ]
