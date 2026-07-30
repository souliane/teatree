"""Surface — and, on request, reconcile — a ``Loop`` row that disagrees with ``defaults.toml``.

A ``Loop`` row is seeded once and never re-read from the shipped ``[loops.<name>]``
table, so a value that was true when the row was created outlives the shipped
change. The consequence is silent and one-directional: a row stuck at
``colleague_facing=1`` is skipped by the away-class admission gate, so the loop
stops firing while every surface still reports it enabled — the shape that starved
cold review, and with it the ``merge_safe`` verdict the PR sweep merges on.

``colleague_facing`` IS operator-editable (the Django admin lists it as editable),
so the seed must not clobber it and the reconcile is explicit
(``seed_loops --reconcile-classification``). Detection is the standing half: the
disagreement is reported by ``t3 doctor check`` with both values, rather than being
inferable only from a loop that mysteriously never runs.
"""

from teatree.loops.seed import DEFAULT_LOOPS

#: Fields whose shipped value is the classification the away-gate reads. Operator-
#: editable via the admin, so drift is REPORTED always and written back only on an
#: explicit reconcile.
CLASSIFICATION_FIELDS: tuple[str, ...] = ("colleague_facing",)


def classification_drift() -> list[str]:
    """One finding per ``Loop`` row disagreeing with its shipped classification."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    stored = {row.name: row for row in Loop.objects.all()}
    findings: list[str] = []
    for spec in DEFAULT_LOOPS:
        row = stored.get(spec.name)
        if row is None:
            continue
        findings.extend(
            f"loop '{spec.name}': DB {field}={getattr(row, field)!r} but shipped defaults.toml "
            f"declares {getattr(spec, field)!r} — the stale row wins at read time"
            for field in CLASSIFICATION_FIELDS
            if getattr(row, field) != getattr(spec, field)
        )
    return findings


def reconcile_classification() -> list[str]:
    """Write the shipped classification back onto every drifting row; return what changed."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    reconciled: list[str] = []
    for spec in DEFAULT_LOOPS:
        for field in CLASSIFICATION_FIELDS:
            shipped = getattr(spec, field)
            updated = Loop.objects.filter(name=spec.name).exclude(**{field: shipped}).update(**{field: shipped})
            if updated:
                reconciled.append(f"loop '{spec.name}': {field} → {shipped!r}")
    return reconciled
