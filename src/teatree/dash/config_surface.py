"""What this box is actually configured to do, readable without an SSH session (#3664).

Every dial the operator tunes — the agent lane's model and reasoning effort, the
``pass`` entries each credential reads, the kill switches, the concurrency dials,
the memory caps — plus the self-repairs the loop applied without paging anyone
(#3665). Like every other dash reader this composes existing resolvers rather
than introducing a second source of truth: the effective values come from
:func:`~teatree.config.get_effective_settings` and
:func:`~teatree.config.agent_spawn.resolve_agent_config`.

**A secret value is never rendered.** A credential row carries the ``pass`` entry
NAME and whether it resolves — never the token. An entry name that is itself
private (a key in :data:`~teatree.config.secret_settings.SECRET_SETTINGS`, whose
value can carry an internal namespace) is masked too, so the page answers "which
account, and does it work" without becoming a secret surface.
"""

import dataclasses
import logging
import re
from dataclasses import dataclass, field

from teatree.config import get_effective_settings
from teatree.config.agent_spawn import resolve_agent_config
from teatree.config.secret_settings import SECRET_SETTINGS, is_credential_reference
from teatree.core.config_self_repair import SELF_REPAIR_STAMP
from teatree.core.models import Task
from teatree.utils.secrets import read_pass

logger = logging.getLogger(__name__)

#: Redaction shown in place of a private entry name.
MASKED = "<private>"

#: How many self-repairs the band lists, newest first.
_SELF_REPAIR_LIMIT = 20

_AGENT_SETTINGS = frozenset({"agent_runtime", "agent_harness", "agent_harness_provider", "mode", "wip", "autonomy"})
_KILL_SWITCH_RE = re.compile(r"(_enabled|_disabled)$")
_EXTRA_KILL_SWITCHES = frozenset({"danger_gate_fail_open", "worker_quiescing"})
_CONCURRENCY_RE = re.compile(r"(concurrency|max_concurrent|_workers)")
_MEMORY_RE = re.compile(r"(ram_|_ram|memory|_mem_|mem_ceiling)")


def classify_setting_band(name: str) -> str:
    """The band *name* belongs to, or ``""`` when it is not an operator dial.

    Name-driven rather than a hand-kept list, so a newly-added kill switch or
    concurrency dial appears on the page with no second registration to forget.
    Memory is checked before concurrency so a RAM ceiling is not read as a
    worker count.
    """
    if name in _AGENT_SETTINGS:
        return "agent"
    if is_credential_reference(name):
        return "credentials"
    if _KILL_SWITCH_RE.search(name) or name in _EXTRA_KILL_SWITCHES:
        return "kill_switches"
    if _MEMORY_RE.search(name):
        return "memory"
    if _CONCURRENCY_RE.search(name):
        return "concurrency"
    return ""


@dataclass(frozen=True, slots=True)
class SettingRow:
    """One rendered dial — its setting name and its effective value as text."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class CredentialEntry:
    """A credential's ``pass`` entry NAME and whether it resolves — never its value."""

    setting: str
    entry_name: str
    resolves: bool

    @classmethod
    def mask_if_private(cls, setting: str, entry_name: str) -> "CredentialEntry":
        """Build the row, masking the entry name when the SETTING is itself private."""
        shown = MASKED if setting in SECRET_SETTINGS else entry_name
        return cls(setting=setting, entry_name=shown, resolves=_pass_entry_resolves(entry_name))


@dataclass(frozen=True, slots=True)
class SelfRepairRow:
    """One correction the loop applied itself instead of paging a human (#3665)."""

    task_id: int
    phase: str
    correction: str


@dataclass(frozen=True, slots=True)
class ConfigView:
    models: tuple[SettingRow, ...] = ()
    agent: tuple[SettingRow, ...] = ()
    credentials: tuple[CredentialEntry, ...] = ()
    kill_switches: tuple[SettingRow, ...] = ()
    concurrency: tuple[SettingRow, ...] = ()
    memory: tuple[SettingRow, ...] = ()
    self_repairs: tuple[SelfRepairRow, ...] = ()
    error: str = ""


@dataclass(slots=True)
class _Bands:
    """Mutable accumulator the settings sweep fills, one list per band."""

    agent: list[SettingRow] = field(default_factory=list)
    credentials: list[CredentialEntry] = field(default_factory=list)
    kill_switches: list[SettingRow] = field(default_factory=list)
    concurrency: list[SettingRow] = field(default_factory=list)
    memory: list[SettingRow] = field(default_factory=list)


def build_config_view() -> ConfigView:
    """Compose every band; degrade the whole page to a visible error, never a 500."""
    try:
        bands = _settings_bands()
        models = _model_rows()
    except Exception:
        logger.warning("dash config view read failed — degrading to an error page", exc_info=True)
        return ConfigView(error="configuration unavailable — read failed", self_repairs=_self_repair_rows())
    return ConfigView(
        models=tuple(models),
        agent=tuple(bands.agent),
        credentials=tuple(bands.credentials),
        kill_switches=tuple(bands.kill_switches),
        concurrency=tuple(bands.concurrency),
        memory=tuple(bands.memory),
        self_repairs=_self_repair_rows(),
    )


def _settings_bands() -> _Bands:
    settings = get_effective_settings()
    bands = _Bands()
    for spec in sorted(dataclasses.fields(settings), key=lambda f: f.name):
        band = classify_setting_band(spec.name)
        if not band:
            continue
        value = getattr(settings, spec.name)
        if band == "credentials":
            bands.credentials.extend(_credential_entries(spec.name, value))
            continue
        getattr(bands, band).append(SettingRow(name=spec.name, value=_render(value)))
    return bands


def _credential_entries(setting: str, value: object) -> list[CredentialEntry]:
    """One row per ``pass`` entry the setting names — a scalar path or a list of them."""
    names = value if isinstance(value, list) else [value]
    return [
        CredentialEntry.mask_if_private(setting, str(name))
        for name in names
        if isinstance(name, str | int | float) and str(name)
    ]


def _model_rows() -> list[SettingRow]:
    """The agent lane's model / reasoning-effort pins."""
    agent = resolve_agent_config()
    rows = [
        SettingRow(name="session_model", value=_render(agent.session_model)),
        SettingRow(name="session_effort", value=_render(agent.session_effort)),
        SettingRow(name="honesty_model", value=_render(agent.honesty_model)),
    ]
    rows.extend(
        SettingRow(name=f"tier_model[{tier}]", value=model) for tier, model in sorted(agent.tier_models.items())
    )
    rows.extend(
        SettingRow(name=f"tier_effort[{tier}]", value=effort) for tier, effort in sorted(agent.tier_effort.items())
    )
    return rows


def _self_repair_rows() -> tuple[SelfRepairRow, ...]:
    """The corrections the loop applied itself — visible here precisely because they never paged."""
    try:
        tasks = list(
            Task.objects.filter(execution_reason__contains=SELF_REPAIR_STAMP).order_by("-pk")[:_SELF_REPAIR_LIMIT]
        )
    except Exception:
        logger.warning("dash self-repair read failed — omitting the band", exc_info=True)
        return ()
    return tuple(
        SelfRepairRow(task_id=task.pk, phase=task.phase, correction=correction)
        for task in tasks
        if (correction := _correction_from(task.execution_reason))
    )


def _correction_from(execution_reason: str) -> str:
    """The ``<setting>=<value>`` a task's self-repair stamp records, or ``""``."""
    _, _, tail = execution_reason.partition(SELF_REPAIR_STAMP)
    return tail.strip().splitlines()[0].strip() if tail.strip() else ""


def _pass_entry_resolves(entry_name: str) -> bool:
    """Whether the ``pass`` store yields anything for *entry_name* — the value is discarded."""
    if not entry_name:
        return False
    try:
        return bool(read_pass(entry_name))
    except Exception:
        logger.warning("pass probe for a configured credential entry failed", exc_info=True)
        return False


def _render(value: object) -> str:
    """A setting's effective value as display text — booleans as on/off, absence as a dash."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None or (isinstance(value, str) and not value):
        return "—"
    return str(value)


__all__ = ["ConfigView", "CredentialEntry", "SelfRepairRow", "SettingRow", "build_config_view", "classify_setting_band"]
