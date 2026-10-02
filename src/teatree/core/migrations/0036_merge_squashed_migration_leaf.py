"""Reconcile the two AST-visible ``core`` migration leaves left by the squash (#3862).

``0001_squashed_0030`` replaces 0001-0030 and Django elides it once the
replaced originals are present — ``MigrationLoader.detect_conflicts()``
sees a single lineage, and ``manage.py migrate`` never raises. But
``0031_taskattempt_reasoning_effort_and_more`` still names the pre-squash
``0030_review_verdict_reviewer_identity_normalized`` as its dependency, so
a naive AST-only reader (no ``replaces`` awareness — teatree's own
CLEAR-time fork probe) sees the squash as an orphan node with no
dependent, alongside the real chain's tip at 0035: two leaves, not one.
This depends on both, giving every such reader a single leaf again — a
no-op for Django, which already treated the graph as linear.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_squashed_0030"),
        ("core", "0035_housekeeping_description_board_reconcile"),
    ]

    operations = []
