"""The live readouts on the settings page — the values that are resolved, not edited (#3664).

The agent lane's model and reasoning-effort pins, the ``pass`` entry each credential
reads, and the self-repairs the loop applied without paging anyone (#3665). None of these
is a ``ConfigSetting`` row an operator types into, so they sit beside the editable rows
rather than among them, on their own 15s htmx poll. Like every other dash reader this
composes existing resolvers rather than introducing a second source of truth: the values
come from :func:`~teatree.config.get_effective_settings` and
:func:`~teatree.config.agent_spawn.resolve_agent_config`.

**A secret value is never rendered.** A credential row carries the ``pass`` entry NAME and
whether it resolves — never the token; an entry name that is itself private (a key in
:data:`~teatree.config.secret_settings.SECRET_SETTINGS`, whose value can carry an internal
namespace) is masked too, so the page answers "which account, and does it work" without
becoming a secret surface. Every OTHER dial the retired band classifier rendered read-only
here is now an editable row on the same page, under its declaration group.
"""

import dataclasses
import logging
from dataclasses import dataclass

from django.core.cache import cache

from teatree.config import get_effective_settings
from teatree.config.agent_spawn import resolve_agent_config
from teatree.config.secret_settings import SECRET_SETTINGS, is_credential_reference
from teatree.core.config_display import render_value
from teatree.core.config_self_repair import SELF_REPAIR_STAMP
from teatree.core.models import Task
from teatree.utils.secrets import read_pass

logger = logging.getLogger(__name__)

#: Redaction shown in place of a private ``pass`` entry NAME. A secret VALUE is redacted
#: by the shared :data:`~teatree.core.config_display.MASKED` token instead — two different
#: questions, and only the value one is a policy the editable rows also answer.
MASKED_ENTRY_NAME = "<private>"

#: How many self-repairs the readout lists, newest first.
_SELF_REPAIR_LIMIT = 20

#: Cache coordinates for the per-entry ``pass`` resolve probe. Keyed by entry NAME so
#: one absent credential never masks another as resolving.
_PROBE_CACHE_PREFIX = "dash:settings:entry_resolves:"
_PROBE_CACHE_TTL = 300


@dataclass(frozen=True, slots=True)
class ModelPin:
    """One resolved model / reasoning-effort pin — its name and its value as text."""

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
        """Build the row, masking the entry NAME when the SETTING is itself private.

        Keyed on ``SECRET_SETTINGS`` membership — NOT the shared ``is_secret`` value-masking
        taxonomy. Every setting reaching here is a credential coordinate, so ``is_secret``
        would be true for all of them and hide every account name, defeating the readout.
        This narrower rule masks only names that can carry an internal namespace.
        """
        shown = MASKED_ENTRY_NAME if setting in SECRET_SETTINGS else entry_name
        return cls(setting=setting, entry_name=shown, resolves=_pass_entry_resolves(entry_name))


@dataclass(frozen=True, slots=True)
class SelfRepairRow:
    """One correction the loop applied itself instead of paging a human (#3665)."""

    task_id: int
    phase: str
    correction: str


@dataclass(frozen=True, slots=True)
class ReadoutsView:
    models: tuple[ModelPin, ...] = ()
    credentials: tuple[CredentialEntry, ...] = ()
    self_repairs: tuple[SelfRepairRow, ...] = ()
    error: str = ""


def build_readouts_view() -> ReadoutsView:
    """Compose every readout; degrade to a visible error, never a 500."""
    try:
        credentials = _credential_rows()
        models = _model_rows()
    except Exception:
        logger.warning("dash readouts read failed — degrading to an error panel", exc_info=True)
        return ReadoutsView(error="configuration unavailable — read failed", self_repairs=_self_repair_rows())
    return ReadoutsView(models=tuple(models), credentials=tuple(credentials), self_repairs=_self_repair_rows())


def _credential_rows() -> list[CredentialEntry]:
    """Every configured credential coordinate, matched by REFERENCE suffix — no hand-kept list."""
    settings = get_effective_settings()
    rows: list[CredentialEntry] = []
    for spec in sorted(dataclasses.fields(settings), key=lambda f: f.name):
        if is_credential_reference(spec.name):
            rows.extend(_credential_entries(spec.name, getattr(settings, spec.name)))
    return rows


def _credential_entries(setting: str, value: object) -> list[CredentialEntry]:
    """One row per ``pass`` entry the setting names — a scalar path or a list of them."""
    names = value if isinstance(value, list) else [value]
    return [
        CredentialEntry.mask_if_private(setting, str(name))
        for name in names
        if isinstance(name, str | int | float) and str(name)
    ]


def _model_rows() -> list[ModelPin]:
    """The agent lane's model / reasoning-effort pins."""
    agent = resolve_agent_config()
    rows = [
        ModelPin(name="session_model", value=render_value(agent.session_model)),
        ModelPin(name="session_effort", value=render_value(agent.session_effort)),
        ModelPin(name="honesty_model", value=render_value(agent.honesty_model)),
    ]
    rows.extend(ModelPin(name=f"tier_model[{tier}]", value=model) for tier, model in sorted(agent.tier_models.items()))
    rows.extend(
        ModelPin(name=f"tier_effort[{tier}]", value=effort) for tier, effort in sorted(agent.tier_effort.items())
    )
    return rows


def _self_repair_rows() -> tuple[SelfRepairRow, ...]:
    """The corrections the loop applied itself — visible here precisely because they never paged."""
    try:
        tasks = list(
            Task.objects.filter(execution_reason__contains=SELF_REPAIR_STAMP).order_by("-pk")[:_SELF_REPAIR_LIMIT]
        )
    except Exception:
        logger.warning("dash self-repair read failed — omitting the readout", exc_info=True)
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
    """Whether the ``pass`` store yields anything for *entry_name* — cached, value discarded.

    Each probe is a GPG decrypt, and the readouts auto-poll every 15s, so an open tab
    would decrypt every configured credential ~240 times an hour to answer one boolean.
    Only the BOOLEAN is cached; the decrypted value is discarded here as it always was,
    so nothing secret enters the cache. The TTL is long enough to collapse the poll
    storm and short enough that a re-inserted entry turns the row green on its own.
    """
    if not entry_name:
        return False
    key = f"{_PROBE_CACHE_PREFIX}{entry_name}"
    cached = cache.get(key)
    if cached is not None:
        return bool(cached)
    resolves = _probe_pass_entry(entry_name)
    cache.set(key, resolves, _PROBE_CACHE_TTL)
    return resolves


def _probe_pass_entry(entry_name: str) -> bool:
    try:
        return bool(read_pass(entry_name))
    except Exception:
        logger.warning("pass probe for a configured credential entry failed", exc_info=True)
        return False


__all__ = ["CredentialEntry", "ModelPin", "ReadoutsView", "SelfRepairRow", "build_readouts_view"]
