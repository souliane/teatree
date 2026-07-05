"""``teatree.overlay_sdk`` — the surface-frozen overlay-authoring namespace (PR-27).

An overlay package needs a stable set of teatree symbols to subclass
:class:`OverlayBase`, build :class:`ProvisionStep` steps, declare readiness
probes, and resolve a :class:`Variant`. Historically each overlay reached into
many different ``teatree.*`` modules directly, so an internal rename in core
broke the overlay *asynchronously* — the overlay's own CI caught it, long after
core merged.

This module is the single documented import surface. Its ``__all__`` is the
frozen contract; :mod:`tests.test_overlay_sdk_surface` snapshots that set AND
the extension-point method signatures in **core's** CI, so a rename or a
signature change fails core *locally* — the conformance guard for extension-point
drift. An overlay imports only from here::

    from teatree.overlay_sdk import OverlayBase, ProvisionStep, Probe, Variant

Adding to the surface is a deliberate act: extend ``__all__`` here and update the
snapshot. Removing or renaming an exported symbol is a breaking change that bumps
``__overlay_api_version__``.
"""

from teatree._overlay_api import __overlay_api_version__
from teatree.config import clone_root, discover_overlays
from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.health import HealthCheck
from teatree.core.overlay import DEFAULT_TRANSITION_EMOJIS, FailedE2EWatcher, OverlayBase, OverlayConfig
from teatree.core.overlay_metadata import OverlayMetadata
from teatree.core.readiness import CommandProbeSpec, HTTPProbeSpec, Probe, ProbeResult, command_probe, http_probe
from teatree.core.variant import Variant
from teatree.core.worktree_env import compose_project, env_cache_path
from teatree.docker.reap import reap_compose_project
from teatree.types import (
    BaseImageConfig,
    DbImportStrategy,
    ProvisionStep,
    RunCommand,
    RunCommands,
    ServiceSpec,
    SkillMetadata,
    SymlinkSpec,
    ToolCommand,
    ValidationResult,
)
from teatree.utils.django_db import DjangoDbImportConfig
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail, run_checked
from teatree.visual_qa import matches_triggers

__all__ = [
    "DEFAULT_TRANSITION_EMOJIS",
    "BaseImageConfig",
    "CommandFailedError",
    "CommandProbeSpec",
    "DbImportStrategy",
    "DjangoDbImportConfig",
    "FailedE2EWatcher",
    "HTTPProbeSpec",
    "HealthCheck",
    "MergeGuard",
    "OverlayBase",
    "OverlayConfig",
    "OverlayMetadata",
    "Probe",
    "ProbeResult",
    "ProvisionStep",
    "RunCommand",
    "RunCommands",
    "ServiceSpec",
    "SkillMetadata",
    "SymlinkSpec",
    "TimeoutExpired",
    "ToolCommand",
    "ValidationResult",
    "Variant",
    "__overlay_api_version__",
    "clone_root",
    "command_probe",
    "compose_project",
    "discover_overlays",
    "env_cache_path",
    "http_probe",
    "matches_triggers",
    "reap_compose_project",
    "run_allowed_to_fail",
    "run_checked",
]
