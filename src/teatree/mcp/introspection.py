"""Read-only introspection over teatree's own configuration and command surface.

The operator-config half of the MCP read tools, split from
:mod:`teatree.mcp.search` by concern: where ``search`` answers questions about
the WORK (tickets, worktrees, PRs, tasks, loop/factory signals), this module
answers questions about TEATREE ITSELF — the effective value of a config
setting, the merge-governing gate states, and which ``t3`` command to run.
Same contract as ``search``: synchronous, JSON-safe, never mutates.
"""

from typing import Any

from teatree.config import COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS, cold_reader, get_effective_settings
from teatree.config.registries import REGISTRY_SETTINGS
from teatree.core.models import ConfigSetting
from teatree.mcp import command_catalogue
from teatree.mcp.search import _capped

_DEFAULT_COMMAND_LIMIT = 20

# The review/merge-governing settings agents most need to read before deciding
# whether a merge is theirs to make — the review gate proper plus the review-phase
# evidence gates. All are ``UserSettings`` bool fields resolved via the effective
# settings, so a per-overlay override is reflected.
_REVIEW_GATE_KEYS = (
    "require_human_approval_to_merge",
    "require_reviewed_state_for_review_request",
    "require_review_context",
    "require_anti_vacuity_attestation",
    "require_merge_evidence",
    "e2e_mandatory_gate_enabled",
)
# The raw/out-of-band merge gate is a cold-hook key (no ``UserSettings`` field),
# resolved from the canonical config DB with its registered fail-open default.
_RAW_MERGE_GATE_KEY = "out_of_band_merge_gate_enabled"


def _scope_label(scope: str) -> str:
    """``global`` for the empty scope, else ``overlay:<name>`` — mirrors the CLI label."""
    return "global" if not scope else f"overlay:{scope}"


def _jsonable(value: object) -> object:
    """Coerce a resolved config value to a JSON-safe primitive for the boundary.

    A ``ConfigSetting`` row is already JSON; a ``UserSettings`` fallback may be a
    ``StrEnum``, ``Path`` or other rich type, so anything not a plain primitive
    is stringified so the read-only tool never fails to serialize.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def config_setting_get(*, key: str, overlay: str | None = None) -> dict[str, Any]:
    """The effective value of a config setting and where it resolves from.

    The read side of the DB override store, mirroring ``t3 <overlay>
    config_setting get``: a ``ConfigSetting`` row in the requested scope is
    reported as ``source == "db"``; otherwise the value falls through to the
    file/env layer (``source == "file/env"``). ``overlay`` reads that overlay's
    scope. A key in neither the overridable nor the registry partition is
    reported ``known == False`` (value ``None``) rather than raising — the read
    surface stays crash-proof on a typo.
    """
    scope = overlay or ""
    label = _scope_label(scope)
    if key not in OVERLAY_OVERRIDABLE_SETTINGS and key not in REGISTRY_SETTINGS:
        return {"key": key, "known": False, "value": None, "source": None, "scope": label, "overlay": scope}
    stored = ConfigSetting.objects.get_effective(key, scope=scope)
    if stored is not None:
        return {"key": key, "known": True, "value": _jsonable(stored), "source": "db", "scope": label, "overlay": scope}
    fallback = getattr(get_effective_settings(overlay or None), key, None)
    return {
        "key": key,
        "known": True,
        "value": _jsonable(fallback),
        "source": "file/env",
        "scope": label,
        "overlay": scope,
    }


def gate_status(*, overlay: str | None = None) -> dict[str, Any]:
    """The review-gate and raw-merge gate state — the merge-governing gates at a glance.

    ``review_gate`` reports whether a human must approve a merge
    (``require_human_approval_to_merge``) plus the review-phase evidence gates,
    all resolved through the effective settings so a per-``overlay`` override is
    reflected. ``raw_merge_gate`` reports whether raw ``gh``/``glab`` merges are
    blocked (``out_of_band_merge_gate_enabled``), resolved from the canonical
    config DB with its registered fail-open default. Read-only: flip a gate with
    ``t3 <overlay> config_setting set`` / ``t3 <overlay> gate``.
    """
    settings = get_effective_settings(overlay or None)
    review_gate = {key: bool(getattr(settings, key)) for key in _REVIEW_GATE_KEYS}
    raw_merge_gate = {
        _RAW_MERGE_GATE_KEY: cold_reader.bool_setting(
            _RAW_MERGE_GATE_KEY,
            default=bool(COLD_HOOK_SETTINGS[_RAW_MERGE_GATE_KEY].default),
        ),
    }
    return {"overlay": overlay or "", "review_gate": review_gate, "raw_merge_gate": raw_merge_gate}


def command_search(*, query: str, limit: int = _DEFAULT_COMMAND_LIMIT) -> list[dict[str, Any]]:
    """The `t3` leaf commands matching *query* — the CLI-discoverability read.

    Answers "which `t3` command do I run for X" so an agent stops guessing
    subcommands that do not exist. Each match carries the full invocation
    ``path``, its one-line help ``summary``, and ``emits_json`` (whether it
    exposes a ``--json`` / ``--format`` output the agent can parse). Sourced from
    the live Typer command tree via the registered catalogue provider, best match
    first.
    """
    return command_catalogue.search_commands(
        query,
        catalogue=command_catalogue.build_command_catalogue(),
        limit=_capped(limit, _DEFAULT_COMMAND_LIMIT),
    )
