"""Canonicalize the legacy ``teatree`` overlay value to ``t3-teatree``.

souliane/teatree#1108: the bundled dogfooding overlay registers via the
``teatree.overlays`` entry point under the name ``t3-teatree`` and now
reads its ``~/.teatree.toml`` overrides from ``[overlays.t3-teatree]``.
Before the fix the overlay mislabelled itself ``teatree`` (a split-brain
between the entry-point name and ``OverlayConfig.overlay_name``), so rows
written while that bug was live carry ``overlay="teatree"``. This pure
data migration collapses every such row to the canonical ``t3-teatree``
across all overlay-carrying models so discovery, the statusline, and the
overlay-keyed selectors stop treating the two as distinct overlays.

No ``AlterField`` is emitted: the ``overlay`` field type is unchanged, so
``makemigrations --check`` stays "No changes".
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

_OVERLAY_MODELS = (
    "Ticket",
    "Worktree",
    "Session",
    "PullRequest",
    "ReviewAssignment",
    "PendingChatInjection",
)


def _canonicalize_teatree_overlay(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    for model_name in _OVERLAY_MODELS:
        model = apps.get_model("core", model_name)
        model.objects.filter(overlay="teatree").update(overlay="t3-teatree")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_pending_chat_loop_reply_fields"),
    ]

    operations = [
        # ``backwards`` is a documented one-way collapse: the legacy
        # ``teatree`` and the canonical ``t3-teatree`` are merged into a
        # single value, so the original split cannot be reconstructed (we
        # cannot tell which ``t3-teatree`` rows were legacy ``teatree``).
        # Mirrors 0002_rename_merge_request_to_pull_request's reasoning for
        # an irreversible normalization — ``RunPython.noop`` keeps the
        # migration formally reversible without fabricating data.
        migrations.RunPython(_canonicalize_teatree_overlay, migrations.RunPython.noop),
    ]
