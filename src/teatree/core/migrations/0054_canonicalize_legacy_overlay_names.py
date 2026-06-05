"""Canonicalize every legacy overlay value that folds onto a registered overlay.

souliane/teatree#1959: rows can carry a stale overlay value that no longer
resolves — a legacy short alias written before #1108 (``teatree`` instead of
``t3-teatree``), or any short alias of an installed entry point (``acme`` →
``t3-acme``). At dispatch time ``get_overlay_for_ticket`` raises
``ImproperlyConfigured`` for such a value and the queued task re-crashes on
every drain (the poison pill).

0027 fixed the single ``teatree`` → ``t3-teatree`` case. This migration
generalizes it: for every distinct overlay value across the overlay-carrying
models, ``resolve_overlay_name`` decides the canonical registered name; a value
that folds onto a *different* canonical is renamed (collision twins deleted
first, as in 0027). A value that resolves to itself, or to nothing (a removed
overlay, a synthetic scanner tag), is left untouched — the unresolvable rows
are surfaced and permanently failed by the runtime poison-pill guard rather
than silently rewritten to a wrong overlay.

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


def _delete_legacy_rows_with_canonical_twin(model: type[Model], *, legacy: str, canonical: str) -> None:
    """Drop ``overlay=legacy`` rows whose other-field tuple already has a ``canonical`` twin."""
    overlay_keyed_constraints = [
        c
        for c in model._meta.constraints  # noqa: SLF001
        if isinstance(c, UniqueConstraint) and "overlay" in c.fields and len(c.fields) > 1
    ]
    if not overlay_keyed_constraints:
        return
    for constraint in overlay_keyed_constraints:
        other_fields = [f for f in constraint.fields if f != "overlay"]
        canonical_tuples = set(model.objects.filter(overlay=canonical).values_list(*other_fields))
        if not canonical_tuples:
            continue
        collision_q = Q()
        for tup in canonical_tuples:
            collision_q |= Q(**dict(zip(other_fields, tup, strict=True)))
        model.objects.filter(overlay=legacy).filter(collision_q).delete()


def _canonicalize_legacy_overlay_names(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    from teatree.core.overlay_loader import resolve_overlay_name  # noqa: PLC0415

    for model_name in _OVERLAY_MODELS:
        model = apps.get_model("core", model_name)
        for legacy in model.objects.exclude(overlay="").values_list("overlay", flat=True).distinct():
            canonical = resolve_overlay_name(legacy)
            if canonical is None or canonical == legacy:
                continue
            _delete_legacy_rows_with_canonical_twin(model, legacy=legacy, canonical=canonical)
            model.objects.filter(overlay=legacy).update(overlay=canonical)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0053_planartifact"),
    ]

    operations = [
        migrations.RunPython(_canonicalize_legacy_overlay_names, migrations.RunPython.noop),
    ]
