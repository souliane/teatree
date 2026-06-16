"""Read-only ``t3 config show`` view: text-file intent vs DB-cached state.

Encodes the #628 cache-vs-intent invariant in the output itself. The
**intent** section is the resolved user-authored config — under the #1775
partition that spans BOTH homes: the ``~/.teatree.toml`` carve-out and the
DB-home ``ConfigSetting`` store (rows are user intent, not regenerable cache).
Deleting either loses user intent. The **derived** section is DB / data-dir
state that can be deleted and deterministically rebuilt from the config plus
repo state (tickets, sessions, update/skill caches); every entry is flagged
``regenerable`` so the invariant is visible, not just documented.

Building the view never imports the ORM eagerly and never writes anything
— it must work with the DB absent (offline-readable, mirroring the
bootstrap-config constraint #628 calls out).
"""

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import teatree.config as config_mod
from teatree.config import get_effective_settings
from teatree.config_mr_reminder import MrReminderConfig
from teatree.paths import CANONICAL_DB, DATA_DIR, DATA_DIR_AUTO_ISOLATED
from teatree.types import SpeakConfig
from teatree.utils.django_bootstrap import ensure_django

_REGENERABLE_CACHE_FILES: tuple[str, ...] = (
    "update-check.json",
    "skill-metadata.json",
    "bad_artifacts.json",
)


@dataclass
class ConfigView:
    config_path: str
    config_exists: bool
    intent: dict[str, Any]
    derived: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "config_exists": self.config_exists,
            "intent": self.intent,
            "derived": self.derived,
        }


def _json_safe(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, SpeakConfig):
        return value.to_dict()
    if isinstance(value, MrReminderConfig):
        return value.to_dict()
    return value


def _intent() -> dict[str, Any]:
    settings = get_effective_settings()
    out = {f.name: _json_safe(getattr(settings, f.name)) for f in dataclasses.fields(settings)}
    out["mode"] = str(out["mode"])
    return out


def _ticket_session_counts() -> dict[str, int] | None:
    try:
        ensure_django()
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        return {"tickets": Ticket.objects.count(), "sessions": Session.objects.count()}
    except Exception:  # noqa: BLE001 — DB absent/unmigrated is a valid offline state, not a crash.
        return None


def _derived() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {
            "name": "control DB",
            "path": str(CANONICAL_DB),
            "exists": CANONICAL_DB.is_file(),
            "worktree_isolated": DATA_DIR_AUTO_ISOLATED,
            "regenerable": True,
        }
    ]
    counts = _ticket_session_counts()
    if counts is not None:
        entries.append({"name": "DB row counts", "value": counts, "regenerable": True})
    for filename in _REGENERABLE_CACHE_FILES:
        cache_file = DATA_DIR / filename
        entries.append(
            {
                "name": filename,
                "path": str(cache_file),
                "exists": cache_file.is_file(),
                "regenerable": True,
            }
        )
    return entries


def build_config_view() -> ConfigView:
    config_path = config_mod.CONFIG_PATH
    return ConfigView(
        config_path=str(config_path),
        config_exists=config_path.is_file(),
        intent=_intent(),
        derived=_derived(),
    )


def _render_derived_entry(entry: dict[str, Any]) -> str:
    name = entry["name"]
    if "value" in entry:
        return f"  {name}: {entry['value']}  [regenerable cache]"
    extra = " (worktree-isolated)" if entry.get("worktree_isolated") else ""
    present = "exists" if entry.get("exists") else "not yet created"
    return f"  {name}: {entry['path']} — {present}{extra}  [regenerable cache]"


def render_config_view(view: ConfigView) -> str:
    status = "present" if view.config_exists else "absent (defaults shown)"
    lines = [
        f"Intent — user-authored source of truth (TOML {view.config_path}, {status}; + DB ConfigSetting store):",
        *(f"  {key} = {view.intent[key]}" for key in sorted(view.intent)),
        "",
        "Derived — DB / data-dir regenerable cache (deletable, rebuilt from intent + repo):",
        *(_render_derived_entry(entry) for entry in view.derived),
    ]
    return "\n".join(lines)
