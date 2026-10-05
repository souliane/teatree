from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0122_worker_generation_and_claimed_generation"),
        ("core", "0120_attempt_conversation_facts_and_cli_too_old"),
    ]

    operations = []
