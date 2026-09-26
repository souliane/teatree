"""Join the dream-clear and incident-receipt migration branches."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0115_clear_the_retired_dream_promotion_cap_rows"),
        ("core", "0114_pending_chat_loop_response_confirmed"),
    ]

    operations = []
