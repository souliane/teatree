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

souliane/teatree#1154: a naive bulk ``UPDATE`` raises
``IntegrityError`` (UNIQUE constraint failed) on any row whose
overlay-keyed unique constraint key already has a canonical
``t3-teatree`` twin (observed: 43 of 44 legacy rows in
``PendingChatInjection`` on a real database). Before the update, we
delete legacy rows whose tuple already matches a canonical row on any
``(overlay, *others)`` unique constraint — the canonical row already
carries the data, so the duplicate collapses into it. The non-colliding
legacy rows are then canonicalized by the unchanged ``UPDATE``.

No ``AlterField`` is emitted: the ``overlay`` field type is unchanged, so
``makemigrations --check`` stays "No changes".
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.db.models import Model, Q, UniqueConstraint

_OVERLAY_MODELS = (
    "Ticket",
    "Worktree",
    "Session",
    "PullRequest",
    "ReviewAssignment",
    "PendingChatInjection",
)


def _delete_legacy_rows_with_canonical_twin(model: type[Model]) -> None:
    """Drop legacy ``overlay='teatree'`` rows whose other-field tuple already has a canonical twin.

    Iterates over every unique constraint that names ``overlay`` plus at
    least one other field. For each, the set of other-field tuples on
    canonical rows is the merge key — any legacy row matching one of
    those tuples would raise ``IntegrityError`` on the upcoming
    ``UPDATE``, so we delete it (the canonical row survives as the
    merged record).
    """
    # ``Model._meta`` is Django's documented public API for model introspection
    # despite the leading underscore — see Django docs § "Model _meta API".
    overlay_keyed_constraints = [
        c
        for c in model._meta.constraints  # noqa: SLF001
        if isinstance(c, UniqueConstraint) and "overlay" in c.fields and len(c.fields) > 1
    ]
    if not overlay_keyed_constraints:
        return
    for constraint in overlay_keyed_constraints:
        other_fields = [f for f in constraint.fields if f != "overlay"]
        canonical_tuples = set(model.objects.filter(overlay="t3-teatree").values_list(*other_fields))
        if not canonical_tuples:
            continue
        # Multi-column ``IN (tuple, tuple, …)`` is not portable across the
        # backends teatree supports, so build an explicit ``OR`` chain of
        # equality clauses — one ``Q`` per canonical tuple.
        collision_q = Q()
        for tup in canonical_tuples:
            collision_q |= Q(**dict(zip(other_fields, tup, strict=True)))
        model.objects.filter(overlay="teatree").filter(collision_q).delete()


def _canonicalize_teatree_overlay(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    for model_name in _OVERLAY_MODELS:
        model = apps.get_model("core", model_name)
        _delete_legacy_rows_with_canonical_twin(model)
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
