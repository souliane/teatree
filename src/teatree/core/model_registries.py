"""App-ready population of the model-callable registries (#2385 PR-2a).

The models are the lowest stratum of ``teatree.core``; gates, the overlay
resolvers, and the cost layer all depend ON the models. A model that imported
them directly would form an intra-``core`` up-edge (invisible to tach inside the
single ``core`` node). The edge is inverted through
``teatree.core.modelkit.gate_registry``: the higher modules register their
callables here, and the models fetch them by name at call time.

``CoreConfig.ready`` calls :func:`populate_model_registries` exactly where the
ordering matters — register gates → register resolvers → register cost — before
``register_signals`` runs. The registry dicts are idempotent (a re-registration
overwrites the same key), so a second ``ready()`` (test re-entry, in-process
``call_command``) is a no-op, not a duplicate-key error.
"""


def _infer_overlay_for_url(url: str) -> str:
    # Re-read the module attribute at call time so a test patching
    # ``teatree.core.overlay_loader.infer_overlay_for_url`` is still seen by the
    # model that fetches this resolver from the registry (#2385).
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    return infer_overlay_for_url(url)


def _resolve_overlay_name(name: str) -> str | None:
    from teatree.core.overlay_loader import resolve_overlay_name  # noqa: PLC0415

    return resolve_overlay_name(name)


def populate_model_registries() -> None:
    """Import every gate (self-registering) and register the resolvers + cost factories."""
    # Importing a gate module runs its module-level ``register_gate(...)`` call.
    from teatree.core.cost import register_cost_factories  # noqa: PLC0415
    from teatree.core.gates import (  # noqa: F401, PLC0415
        dod_gate,
        fix_dod_gate,
        integration_review_gate,
        merge_evidence_gate,
        plan_gate,
        review_context_gate,
        spec_coverage_gate,
    )
    from teatree.core.modelkit.gate_registry import register_resolver  # noqa: PLC0415

    register_resolver("infer_overlay_for_url", _infer_overlay_for_url)
    register_resolver("resolve_overlay_name", _resolve_overlay_name)
    register_cost_factories()
