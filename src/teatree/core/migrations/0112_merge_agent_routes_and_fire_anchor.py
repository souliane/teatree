"""Rejoin the agent-route and fire-anchor migration leaves."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0111_agent_skill_routes"),
        ("core", "0111_merge_the_fire_anchor_branch"),
    ]

    operations: list = []
