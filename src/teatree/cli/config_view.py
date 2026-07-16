"""Read-only ``t3 config show`` view: user-intent config vs DB-cached state.

Encodes the #628 cache-vs-intent invariant in the output itself. The
**intent** section is the resolved user-authored config — the DB-home
``ConfigSetting`` store (rows are user intent, not regenerable cache), listable
with ``t3 <overlay> config_setting list``. The **derived** section is DB /
data-dir state that can be deleted and deterministically rebuilt from the config
plus repo state (tickets, sessions, update/skill caches); every entry is flagged
``regenerable`` so the invariant is visible, not just documented.

Building the view never imports the ORM eagerly and never writes anything
— it must work with the DB absent (offline-readable, mirroring the
bootstrap-config constraint #628 calls out).
"""

import dataclasses
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teatree.config import FEATURE_FLAGS, get_effective_settings
from teatree.config.mr_reminder import MrReminderConfig
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
    intent: dict[str, Any]
    derived: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "derived": self.derived,
            "flags": self.flags,
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
    # Feature flags are governed, lifecycle-staged toggles — not durable settings.
    # They are partitioned OUT of the user-facing intent dump into their own
    # stage-labelled ``flags`` section, so a temporary switch never reads as a knob
    # the operator is meant to keep.
    settings = get_effective_settings()
    out = {
        f.name: _json_safe(getattr(settings, f.name))
        for f in dataclasses.fields(settings)
        if f.name not in FEATURE_FLAGS
    }
    out["mode"] = str(out["mode"])
    return out


def _flags() -> list[dict[str, Any]]:
    settings = get_effective_settings()
    return [
        {
            "name": key,
            "value": getattr(settings, key),
            "stage": flag.stage.value,
            "tracking_issue": flag.tracking_issue,
            "summary": flag.summary,
        }
        for key, flag in FEATURE_FLAGS.items()
    ]


def _ticket_session_counts() -> dict[str, int] | None:
    try:
        ensure_django()
        from teatree.core.models.session import Session  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

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
            # The canonical store holds tickets, sessions, merge approvals, and the
            # ConfigSetting rows themselves — user intent, NOT regenerable cache.
            # Deleting it destroys durable state, so it must never read "deletable".
            "regenerable": False,
        }
    ]
    counts = _ticket_session_counts()
    if counts is not None:
        entries.append({"name": "DB row counts", "value": counts, "regenerable": False})
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
    return ConfigView(
        intent=_intent(),
        derived=_derived(),
        flags=_flags(),
    )


def _render_derived_entry(entry: dict[str, Any]) -> str:
    name = entry["name"]
    tag = "[regenerable cache]" if entry.get("regenerable", True) else "[canonical store — NOT regenerable]"
    if "value" in entry:
        return f"  {name}: {entry['value']}  {tag}"
    extra = " (worktree-isolated)" if entry.get("worktree_isolated") else ""
    present = "exists" if entry.get("exists") else "not yet created"
    return f"  {name}: {entry['path']} — {present}{extra}  {tag}"


def _render_flag_entry(entry: dict[str, Any]) -> str:
    return (
        f"  {entry['name']} = {entry['value']}  "
        f"[feature flag, stage={entry['stage']}, tracking {entry['tracking_issue']}]"
    )


def render_config_view(view: ConfigView) -> str:
    lines = [
        "Intent — user-authored source of truth (DB ConfigSetting store; `t3 <overlay> config_setting list`):",
        *(f"  {key} = {view.intent[key]}" for key in sorted(view.intent)),
        "",
        "Flags — governed feature toggles (stage-labelled lifecycle; born and removed with the code they gate):",
        *(_render_flag_entry(entry) for entry in sorted(view.flags, key=operator.itemgetter("name"))),
        "",
        (
            "Derived — DB / data-dir artifacts (regenerable cache rebuilt from intent + repo; "
            "the canonical store is NOT regenerable — deleting it destroys durable state):"
        ),
        *(_render_derived_entry(entry) for entry in view.derived),
    ]
    return "\n".join(lines)
