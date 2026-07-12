"""Open harness backend registry — overlay-registrable transports + capability flags (#3157 E1).

The seam #2565 shipped a :class:`~teatree.agents.harness.Harness` protocol pair but a
CLOSED backend set: a two-member ``AgentHarness`` enum resolved by a hard-coded
``resolve_harness``. An overlay that needs a third transport — a direct Anthropic
Messages-API backend, an enterprise cloud endpoint, a self-hosted model — had to edit
core, contradicting the overlay philosophy (overlays register via ``teatree.overlays``
entry points; harnesses should too).

This module opens the set. :func:`register_harness` records a factory keyed by string
name; the ``teatree.harnesses`` entry-point group lets an installed overlay package add
one with ZERO core edits. :func:`resolve_harness_spec` looks a name up. Every built-in
enum value (``claude_sdk`` / ``pydantic_ai``) is just a registry key now, registered by
:mod:`teatree.agents.harness` at import.

The registry ALSO carries the per-backend :class:`HarnessCapabilities` — a typed flag
set (``hooks`` / ``mcp`` / ``cache_control`` / ``server_resume`` / ``structured_output``)
so dispatch code asks a harness what it supports instead of ``isinstance``-branching on
the concrete class, and an overlay can introspect a backend before selecting it.
"""

import importlib.metadata
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.agents.harness import Harness
    from teatree.config.settings import UserSettings
    from teatree.core.models import Task

logger = logging.getLogger(__name__)

#: The entry-point group an installed overlay package adds a harness under. The
#: value of each entry point is a zero-arg callable returning a :class:`HarnessSpec`.
HARNESS_ENTRY_POINT_GROUP = "teatree.harnesses"


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """The typed flag set the driver + doctors read instead of ``isinstance``-branching.

    Dispatch code and doctors ask a harness these questions instead of branching on the
    concrete class; an overlay introspects them before selecting a backend. Two categories:

    Capability flags — WHAT the backend supports:

    *   ``hooks`` — fires pre/post-tool hook events (the ``claude-agent-sdk`` lane does).
    *   ``mcp`` — drives MCP servers/toolsets.
    *   ``cache_control`` — can place explicit prompt-cache breakpoints with a TTL (the
        direct Anthropic Messages-API binding does; the OpenAI-compatible router does not).
    *   ``server_resume`` — resumes a prior session server-side (the SDK's ``--resume``).
    *   ``structured_output`` — enforces a schema-validated result envelope natively,
        rather than scraping the last JSON line of agent text.

    Dispatch-lane hints — HOW the driver routes it (#3157 AH-5, previously read off the
    concrete class by untyped ``getattr``; now typed fields the driver reads through the
    protocol's ``capabilities`` attribute):

    *   ``spawns_cli_child`` — spawns the bundled ``claude`` CLI child, so dispatch resolves
        the Layer-2 provider child env for it (only the ``claude_sdk`` backend does).
    *   ``metered_lane`` — the transport FIXES the run to the metered Layer-2 lane (OrcaRouter
        BYOK or the native Anthropic key), unlike the ``claude_sdk`` subscription lane whose
        attribution is resolved from the explicit provider pin.
    """

    hooks: bool = False
    mcp: bool = False
    cache_control: bool = False
    server_resume: bool = False
    structured_output: bool = False
    spawns_cli_child: bool = False
    metered_lane: bool = False


@dataclass(frozen=True, slots=True)
class HarnessBuildContext:
    """The resolution context a harness factory builds an instance from.

    *task* is the dispatch about to run (a resumable backend rehydrates its parked
    thread from it); *phase* opts a dispatch into a phase-scoped tool layer; *settings*
    is the resolved effective settings a factory reads its per-backend knobs from
    (router lane, credentials, request cap). A factory is free to ignore any of them —
    the ``claude_sdk`` factory needs none.
    """

    task: "Task | None" = None
    phase: str | None = None
    settings: "UserSettings | None" = None


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """A registered harness backend — its factory, declared capabilities, and constraints.

    *factory* builds a :class:`~teatree.agents.harness.Harness` from a
    :class:`HarnessBuildContext`. *capabilities* is the backend's declared flag set.
    *valid_providers* is the set of ``AgentHarnessProvider`` values valid under this
    backend (the registry-declared constraint that mirrors, for the built-ins,
    ``AgentHarnessProvider.valid_for``, and lets an overlay backend declare its own).
    """

    name: str
    factory: "Callable[[HarnessBuildContext], Harness]"
    capabilities: HarnessCapabilities = field(default_factory=HarnessCapabilities)
    valid_providers: frozenset[str] = frozenset()


class UnknownHarnessError(LookupError):
    """The configured ``agent_harness`` names no registered backend.

    Raised at resolve time (not config-parse time — the config layer cannot see the
    agents-layer registry), so a typo or an overlay whose entry point failed to load
    surfaces as a recorded dispatch failure rather than a silent wrong transport.
    """


class InvalidHarnessProviderError(ValueError):
    """The pinned ``agent_harness_provider`` is not valid under the resolved harness backend.

    The live harness↔provider constraint (#3157 AH-6). Raised at dispatch resolution — the
    agents layer can consult the registry, the config layer cannot — from the registry's
    declared :attr:`HarnessSpec.valid_providers`, so an overlay-registered THIRD harness's
    provider constraint is enforced too. The closed-enum
    :meth:`~teatree.config.AgentHarnessProvider.valid_for` only knows the two built-ins;
    this backs it with the open registry so a Vertex/enterprise backend can declare and
    enforce its own valid providers with zero core edits.
    """


_REGISTRY: dict[str, HarnessSpec] = {}
_ENTRY_POINTS_LOADED = False


def register_harness(
    name: str,
    factory: "Callable[[HarnessBuildContext], Harness]",
    *,
    capabilities: HarnessCapabilities | None = None,
    valid_providers: frozenset[str] = frozenset(),
) -> None:
    """Register a harness backend under *name* (last registration wins).

    The built-ins register at :mod:`teatree.agents.harness` import; an overlay adds one
    through the :data:`HARNESS_ENTRY_POINT_GROUP` entry point (see :func:`_load_entry_points`)
    or by calling this directly from its own setup.
    """
    _REGISTRY[name] = HarnessSpec(
        name=name,
        factory=factory,
        capabilities=capabilities if capabilities is not None else HarnessCapabilities(),
        valid_providers=frozenset(valid_providers),
    )


def resolve_harness_spec(name: str) -> HarnessSpec:
    """Return the :class:`HarnessSpec` registered under *name*, loading entry points first.

    Raises :class:`UnknownHarnessError` when no backend — built-in, overlay entry point,
    or programmatic registration — carries the name.
    """
    _load_entry_points()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        msg = f"No harness registered under agent_harness={name!r}; registered: {known}"
        raise UnknownHarnessError(msg) from exc


def registered_harness_names() -> frozenset[str]:
    """Every registered harness name (built-ins + loaded overlay entry points)."""
    _load_entry_points()
    return frozenset(_REGISTRY)


def valid_providers_for(name: str) -> frozenset[str]:
    """The ``agent_harness_provider`` values valid under the harness registered as *name*.

    Reads the registry-declared :attr:`HarnessSpec.valid_providers` (#3157 AH-6) — the OPEN
    parallel of the closed-enum :meth:`~teatree.config.AgentHarnessProvider.valid_for`, so an
    overlay backend's own constraint is consulted. An unregistered *name* returns an empty
    set (unconstrained) rather than raising: the unknown-harness condition surfaces at real
    dispatch resolution (:class:`UnknownHarnessError`), not here.
    """
    try:
        return resolve_harness_spec(name).valid_providers
    except UnknownHarnessError:
        return frozenset()


def assert_provider_valid_for_harness(name: str, provider: str | None) -> None:
    """Raise :class:`InvalidHarnessProviderError` when *provider* is pinned but invalid under *name*.

    The live consumer of :attr:`HarnessSpec.valid_providers` (#3157 AH-6). A ``None``/absent
    *provider* (the ambient-default, no explicit pin) always passes. An empty declared valid
    set (a harness that did not declare its providers) is treated as UNCONSTRAINED — declaring
    ``valid_providers`` is opt-in, so an under-declared overlay backend is never blocked.
    """
    if provider is None:
        return
    valid = valid_providers_for(name)
    if valid and provider not in valid:
        allowed = ", ".join(sorted(valid))
        msg = f"agent_harness_provider={provider!r} is not valid under agent_harness={name!r}; valid: {allowed}"
        raise InvalidHarnessProviderError(msg)


def _load_entry_points() -> None:
    """Load and register every ``teatree.harnesses`` entry point exactly once.

    Each entry point resolves to a zero-arg callable returning a :class:`HarnessSpec`;
    an already-registered name (a built-in) is not overridden by an entry point. Loading
    is memoised so repeated resolution never re-imports.
    """
    global _ENTRY_POINTS_LOADED  # noqa: PLW0603 — one-time memoised entry-point scan
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for entry_point in importlib.metadata.entry_points(group=HARNESS_ENTRY_POINT_GROUP):
        try:
            spec = entry_point.load()()
        except Exception:
            logger.warning(
                "Harness entry point %r failed to load; skipping it — other backends still resolve.",
                getattr(entry_point, "name", entry_point),
                exc_info=True,
            )
            continue
        _REGISTRY.setdefault(spec.name, spec)


def _reset_registry_for_test() -> None:
    """Drop entry-point-loaded state so a test can re-run the discovery path."""
    global _ENTRY_POINTS_LOADED  # noqa: PLW0603 — test-only reset of the memoised scan
    _ENTRY_POINTS_LOADED = False
