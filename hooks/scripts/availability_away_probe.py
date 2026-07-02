"""Availability-mode read for the bare-``python3`` Stop/PreToolUse hooks (#2559, #2544, fast-hooks).

The harness invokes the hooks as a bare ``python3`` with NO ``uv`` env, so
teatree's dependencies are not importable and ``django.setup()`` cannot run in
the hook interpreter. ``_resolved_away_mode`` previously bootstrapped Django
in-process to call ``resolve_mode()``; under the real hook that bootstrap failed
and the lever returned ``False`` (never away) — silently neutering
``t3 <overlay> availability away`` as a self-pump suppressor.

Fast path (fast-hooks): the availability decision is FILE / TOML based, not a
Django concern. Its top precedence tier — an unexpired manual override written by
``t3 <overlay> availability away|autonomous-away|present`` — is a plain JSON
file, and the default (no configured schedule) is ``present``. Both are resolved
here in pure stdlib, so the common cases (a manual pause, or no schedule at all)
never shell out. Only the cron-window ``[teatree.availability].windows`` schedule
tier — which needs ``croniter``, absent from the bare hook — falls back to the
``t3`` subprocess (``availability show``) for an exact evaluation in the editable
install's child process. This removes the ~2.5s Django cold-boot the subprocess
paid on EVERY Stop/away-gate hot path (the recurring TIMEOUT) for all but the
rare configured-schedule session, while keeping the resolved mode DECISION
identical to ``teatree.core.availability.resolve_mode`` at every tier.

Three availability modes (#2544): ``present``, ``away`` (holiday — defers
questions AND pauses the self-pump), and ``autonomous_away`` (unattended run —
defers questions like ``away`` but keeps the self-pump running like ``present``).
The two behaviours ``away`` used to conflate are split into two predicates,
mirroring ``teatree.core.availability.Resolution.defers_questions`` /
``.pauses_self_pump``: :func:`resolved_defers_questions` (``away`` +
``autonomous_away``) and :func:`resolved_pauses_self_pump` (``away`` only).

DB-home resolution is reused from ``teatree.config.cold_reader``
(``canonical_config_db().parent`` is the PRIMARY data dir the installed ``t3``
reads/writes, correct even from inside a worktree), so the override file this hook
reads is the same one the CLI writes. ``src/`` is put on ``sys.path`` for that
lookup via the shared :func:`teatree_src_on_path` bootstrap (#1314).

It lives in a bare sibling module (not ``hook_router``) so the over-cap,
shrink-only router gains the stdlib behaviour without growing (``hooks/CLAUDE.md``
§ "Adding a gate").
"""

import json
import os
import shutil
import subprocess  # noqa: S404 — reads a trusted local ``t3`` binary, fixed argv, never shell
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from managed_repo import teatree_src_on_path

_AWAY_READ_TIMEOUT = 5

# The always-registered overlay's CLI alias — the fall-through token for the
# overlay-agnostic hooks. ``t3-teatree`` ships in core's pyproject, so
# ``t3 teatree …`` always resolves; ``resolve_mode()`` is overlay-independent, so
# ANY registered overlay's ``availability show`` returns the same mode.
_DEFAULT_OVERLAY_ALIAS = "teatree"

MODE_PRESENT = "present"
MODE_AWAY = "away"
# Autonomous-away (#2544): unattended run — questions defer exactly like
# holiday-``away`` but the Stop self-pump keeps firing exactly like ``present``.
MODE_AUTONOMOUS_AWAY = "autonomous_away"
_VALID_MODES = frozenset({MODE_PRESENT, MODE_AWAY, MODE_AUTONOMOUS_AWAY})

# Modes in which the user is unreachable NOW — questions defer to the durable
# backlog. Mirrors ``teatree.core.availability._DEFERRING_MODES``.
_DEFERRING_MODES = frozenset({MODE_AWAY, MODE_AUTONOMOUS_AWAY})
# Modes that pause the Stop self-pump — only holiday-``away``. Mirrors
# ``teatree.core.availability._PAUSING_MODES``. Kept in parity with the core
# resolver by ``tests/test_loop_pause_levers_stdlib.py``.
_PAUSING_MODES = frozenset({MODE_AWAY})

_OVERRIDE_FILENAME = "availability_override.json"


@dataclass(frozen=True, slots=True)
class _Override:
    """Stdlib mirror of ``teatree.core.availability.Override`` (mode + optional expiry)."""

    mode: str
    until: datetime | None

    def is_active(self, now: datetime) -> bool:
        return self.until is None or now < self.until


def resolved_mode_token() -> str:
    """The resolved availability mode string, fast stdlib path first (#2544, #2559).

    Resolves the same precedence chain as
    :func:`teatree.core.availability.resolve_mode`: an unexpired manual override
    (a JSON file) and the default-``present`` no-schedule case are decided here
    in stdlib; a configured cron ``[teatree.availability].windows`` schedule
    (which needs ``croniter``, absent from the bare hook, to evaluate the
    windows and honour the live-presence upgrade) is delegated to the ``t3``
    subprocess for an exact read.

    FAIL SAFE for the caller: an unreadable override, an unresolvable data dir,
    an absent ``t3``, a failed subprocess, or unparsable output all resolve to
    ``""`` (unknown — every predicate below treats it as non-deferring,
    non-pausing, same net effect as ``present``). This function never raises.
    """
    override_mode = _active_override_mode(datetime.now(tz=UTC))
    if override_mode is not None:
        return override_mode
    if _schedule_has_windows():
        return _subprocess_mode_token()
    return MODE_PRESENT


def resolved_away_mode() -> bool:
    """True when the resolved availability mode is exactly ``away`` (fast, no Django boot).

    Kept as the ``away``-only predicate the self-pump pause has always used —
    only holiday-``away`` pauses the loop. ``autonomous_away`` keeps the factory
    running, so it is deliberately excluded here.
    """
    return resolved_mode_token() == MODE_AWAY


def resolved_defers_questions() -> bool:
    """True when the resolved mode defers questions — ``away`` or ``autonomous_away`` (#2544)."""
    return resolved_mode_token() in _DEFERRING_MODES


def resolved_pauses_self_pump() -> bool:
    """True when the resolved mode pauses the Stop self-pump — ``away`` only (#2544)."""
    return resolved_mode_token() in _PAUSING_MODES


def _active_override_mode(now: datetime) -> str | None:
    """The active manual-override mode, or ``None`` when none is active.

    Stdlib port of ``availability.load_override`` + ``Override.is_active``: an
    absent / expired / unparsable override yields ``None`` so the caller falls
    through to the schedule / default tiers, exactly as ``load_override`` fails
    open.
    """
    override = _load_override()
    if override is None or not override.is_active(now):
        return None
    return override.mode


def _load_override() -> _Override | None:
    """Read + validate the ``availability_override.json`` file into an :class:`_Override`.

    Reads under the PRIMARY data dir. Any read/parse error, a non-object
    document, an invalid ``mode``, or a present-but-unparsable ``until`` (which
    voids the override, mirroring ``load_override`` returning ``None``) all yield
    ``None``. An absent/empty ``until`` means no expiry (active indefinitely).
    """
    data_dir = _primary_data_dir()
    if data_dir is None:
        return None
    try:
        raw = json.loads((data_dir / _OVERRIDE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode", "")).strip().lower()
    until, until_ok = _parse_until(raw.get("until"))
    if mode not in _VALID_MODES or not until_ok:
        return None
    return _Override(mode=mode, until=until)


def _parse_until(value: object) -> tuple[datetime | None, bool]:
    """Parse the override ``until`` field into ``(expiry, ok)``.

    A valid ISO-8601 string → ``(datetime, True)`` (naive parsed as UTC); an
    absent/empty ``until`` → ``(None, True)`` (no expiry); a present-but-
    unparsable value → ``(None, False)`` (voids the whole override, matching
    ``load_override`` returning ``None``).
    """
    if not isinstance(value, str) or not value.strip():
        return None, True
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        return None, False
    return (until.replace(tzinfo=UTC) if until.tzinfo is None else until), True


def _primary_data_dir() -> Path | None:
    """The PRIMARY teatree data dir (``canonical_config_db().parent``); ``None`` on failure.

    Reuses ``cold_reader``'s DB-home resolution so a worktree hook reads the same
    ``availability_override.json`` the installed ``t3`` writes, never a per-worktree
    copy. ``src/`` is bootstrapped onto ``sys.path`` for the import (#1314).
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import canonical_config_db  # noqa: PLC0415

            return canonical_config_db().parent
    except Exception:  # noqa: BLE001 — hook crash-proof: unresolvable data dir ⇒ no override read
        return None


def _schedule_has_windows() -> bool:
    """True when ``[teatree.availability].windows`` has any configured entry.

    Mirrors ``availability.load_schedule``'s config-path resolution (``TEATREE_TOML``
    env override, else ``~/.teatree.toml``). The bare hook cannot evaluate cron
    windows (no ``croniter``), so a configured window defers to the ``t3``
    subprocess for an exact read. An all-invalid-windows config still defers here
    and the subprocess returns the true ``present`` — the DECISION stays exact, at
    the cost of one avoidable subprocess in that pathological case. A missing /
    unreadable config resolves to ``False`` (no windows ⇒ default ``present``).
    """
    config_path = Path(os.environ.get("TEATREE_TOML", str(Path.home() / ".teatree.toml")))
    try:
        if not config_path.is_file():
            return False
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return False
    section = data.get("teatree", {}) if isinstance(data, dict) else {}
    availability = section.get("availability", {}) if isinstance(section, dict) else {}
    windows = availability.get("windows", []) if isinstance(availability, dict) else []
    if not isinstance(windows, list):
        return False
    return any(isinstance(entry, str) and entry.strip() for entry in windows)


def _subprocess_mode_token() -> str:
    """The resolved mode from ``t3 <overlay> availability show`` (schedule tier).

    The exact-evaluation fallback for the configured-schedule case: the editable
    install boots Django in the child ``t3`` process and evaluates the cron
    windows + live-presence upgrade via ``resolve_mode``. FAIL SAFE — an absent
    ``t3``, a failed subprocess, or unparsable output resolves to ``""``.
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return ""
    for overlay in _overlay_candidates():
        out = _availability_show(t3_bin, overlay)
        if out is not None:
            return _mode_token(out)
    return ""


def _overlay_candidates() -> list[str]:
    """Overlay CLI tokens to try for ``availability show``, in order.

    ``T3_OVERLAY_NAME`` first (when set), then the always-registered ``teatree``
    alias as the never-empty fall-through — de-duplicated, order preserved.
    """
    candidates: list[str] = []
    env_overlay = os.environ.get("T3_OVERLAY_NAME", "").strip()
    if env_overlay:
        candidates.append(env_overlay)
    if _DEFAULT_OVERLAY_ALIAS not in candidates:
        candidates.append(_DEFAULT_OVERLAY_ALIAS)
    return candidates


def _availability_show(t3_bin: str, overlay: str) -> str | None:
    """Stdout of ``t3 <overlay> availability show``; ``None`` on any failure."""
    try:
        result = subprocess.run(  # noqa: S603 — trusted local binary, fixed argv, no shell
            [t3_bin, overlay, "availability", "show"],
            capture_output=True,
            text=True,
            timeout=_AWAY_READ_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout


def _mode_token(text: str) -> str:
    """The ``mode=…`` value from an ``availability: mode=… source=…`` line, or ``""``."""
    for raw in text.splitlines():
        for token in raw.split():
            key, sep, value = token.partition("=")
            if sep and key.strip() == "mode":
                return value.strip().lower()
    return ""


__all__ = [
    "MODE_AUTONOMOUS_AWAY",
    "MODE_AWAY",
    "MODE_PRESENT",
    "resolved_away_mode",
    "resolved_defers_questions",
    "resolved_mode_token",
    "resolved_pauses_self_pump",
]
