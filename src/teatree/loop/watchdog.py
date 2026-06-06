"""Laptop always-on session watchdog (#1139).

Two cooperating pieces:

A) **OS-level supervisor.** On macOS, a ``launchd`` LaunchAgent
    (``~/Library/LaunchAgents/com.<user>.teatree-loop.plist``) invokes
    ``t3 loop spawn-headless`` with ``KeepAlive=true``+``RunAtLoad=true``
    so the loop's Claude Code session is restarted whenever it exits.
    Linux is documented as a TODO — a bare ``cron`` line is suggested by
    the CLI.

B) **Account-switch session-discovery.** When the user runs ``/login``
    to switch the active Claude Code account, sessions previously
    spawned under the old account become unreachable from the new
    account's mobile dispatch surface. The watchdog detects this by
    reading the currently-active ``oauthAccount.accountUuid`` from
    ``~/.claude.json`` and comparing it to the account UUID recorded in
    ``~/.claude/teatree-loop-session.json`` at the time the watchdog
    spawned the loop session (the "pin" file). When the two differ — or
    the pinned PID is dead — the watchdog respawns a fresh session under
    the active account.

The module is split between pure-data helpers (parsers, plist body,
dataclasses) that are unit-tested with synthetic JSON under
``tmp_path`` and an outer ``install_watchdog`` / ``uninstall_watchdog``
shell that drives ``launchctl`` via :mod:`subprocess`. Only the
``subprocess`` boundary is mocked in tests; everything else runs against
a fake ``home`` directory.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from teatree.core.account_fingerprint import current_account_fingerprint
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

CLAUDE_HOME_DIR = ".claude"
ACTIVE_ACCOUNT_FILE = ".claude.json"
SESSIONS_SUBDIR = "sessions"
LOOP_PIN_FILE = "teatree-loop-session.json"
DEFAULT_LABEL = "com.teatree.loop"


class WatchdogError(RuntimeError):
    """Raised when the watchdog cannot complete a required action."""


@dataclass(frozen=True, slots=True)
class AccountState:
    """The currently-logged-in Claude Code account, as recorded in ``~/.claude.json``."""

    account_uuid: str
    email: str


@dataclass(frozen=True, slots=True)
class LoopSessionInfo:
    """A teatree-loop Claude Code session candidate, as discovered on disk."""

    session_id: str
    pid: int
    account_uuid: str | None
    is_alive: bool
    belongs_to_active_account: bool


# ── account discovery ────────────────────────────────────────────────


def current_active_account(*, home: Path | None = None) -> AccountState | None:
    """Return the currently-logged-in Claude Code account, or ``None`` if unknown.

    The account fingerprint (``oauthAccount.accountUuid``) comes from the
    canonical single reader :func:`teatree.core.account_switch.current_account_fingerprint`
    so the watchdog and the in-session recovery never diverge on which value is
    "the account". The display ``email`` is read alongside; a missing or
    malformed file is "no signal" (``None``) so the watchdog degrades to
    "any session is good enough".
    """
    home = home if home is not None else Path.home()
    uuid = current_account_fingerprint(home=home)
    if not uuid:
        return None
    email = _read_account_email(home)
    return AccountState(account_uuid=uuid, email=email)


def _read_account_email(home: Path) -> str:
    cfg = home / ACTIVE_ACCOUNT_FILE
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    email = oauth.get("emailAddress", "") if isinstance(oauth, dict) else ""
    return email if isinstance(email, str) else ""


# ── session discovery ────────────────────────────────────────────────


def _read_loop_pin(home: Path) -> dict[str, str]:
    """Read ``~/.claude/teatree-loop-session.json`` (the watchdog pin)."""
    pin = home / CLAUDE_HOME_DIR / LOOP_PIN_FILE
    if not pin.is_file():
        return {}
    try:
        data = json.loads(pin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def discover_loop_sessions(
    *,
    active_account_uuid: str | None,
    home: Path | None = None,
) -> list[LoopSessionInfo]:
    """Return every running Claude Code session pinned as a teatree-loop session.

    Pinning is the structural signal: only sessions recorded in
    ``~/.claude/teatree-loop-session.json`` are considered. Each entry is
    cross-referenced against the per-PID metadata in
    ``~/.claude/sessions/<pid>.json`` and tagged with whether it
    ``belongs_to_active_account``.
    """
    home = home if home is not None else Path.home()
    sessions_dir = home / CLAUDE_HOME_DIR / SESSIONS_SUBDIR
    if not sessions_dir.is_dir():
        return []

    pin = _read_loop_pin(home)
    pinned_session_id = pin.get("sessionId")
    pinned_account_uuid = pin.get("accountUuid")
    if not pinned_session_id:
        return []

    results: list[LoopSessionInfo] = []
    for state_file in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        pid = data.get("pid")
        if sid != pinned_session_id or not isinstance(pid, int):
            continue
        results.append(
            LoopSessionInfo(
                session_id=sid,
                pid=pid,
                account_uuid=pinned_account_uuid if isinstance(pinned_account_uuid, str) else None,
                is_alive=_is_pid_alive(pid),
                belongs_to_active_account=(
                    isinstance(pinned_account_uuid, str)
                    and isinstance(active_account_uuid, str)
                    and pinned_account_uuid == active_account_uuid
                ),
            ),
        )
    return results


def needs_respawn(*, home: Path | None = None) -> bool:
    """Return ``True`` when the watchdog should boot a new headless session.

    Decision rules: respawn when there is no pinned session, when the
    pinned session's PID is dead, or when the pinned session was spawned
    under an account different from the currently-active one.
    """
    home = home if home is not None else Path.home()
    active = current_active_account(home=home)
    active_uuid = active.account_uuid if active is not None else None
    sessions = discover_loop_sessions(active_account_uuid=active_uuid, home=home)
    if not sessions:
        return True
    return not any(s.is_alive and s.belongs_to_active_account for s in sessions)


# ── pin file ─────────────────────────────────────────────────────────


def pin_session(*, session_id: str, home: Path | None = None) -> Path:
    """Record ``session_id`` as the watchdog-owned loop session.

    Stamps the session with the currently-active account UUID so
    :func:`needs_respawn` can detect ``/login`` switches.
    """
    home = home if home is not None else Path.home()
    active = current_active_account(home=home)
    if active is None:
        msg = "Cannot pin loop session: no active Claude Code account in ~/.claude.json."
        raise WatchdogError(msg)
    pin = home / CLAUDE_HOME_DIR / LOOP_PIN_FILE
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(
        json.dumps({"sessionId": session_id, "accountUuid": active.account_uuid}),
        encoding="utf-8",
    )
    return pin


# ── launchd plist ────────────────────────────────────────────────────


def launch_agent_plist_path(*, label: str = DEFAULT_LABEL, home: Path | None = None) -> Path:
    """Return the path where the LaunchAgent plist should live."""
    home = home if home is not None else Path.home()
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def launch_agent_plist(*, label: str = DEFAULT_LABEL, t3_bin: str = "t3") -> str:
    """Return the LaunchAgent plist body that respawns ``t3 loop spawn-headless``.

    The agent runs under ``zsh -lc`` so the user's login PATH (including
    pyenv shims and ``uv`` tool installs) resolves; the absolute
    ``t3_bin`` is passed through the shell verbatim.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "    <key>Label</key>",
        f"    <string>{label}</string>",
        "    <key>ProgramArguments</key>",
        "    <array>",
        "        <string>/bin/zsh</string>",
        "        <string>-lc</string>",
        f"        <string>{t3_bin} loop spawn-headless</string>",
        "    </array>",
        "    <key>RunAtLoad</key>",
        "    <true/>",
        "    <key>KeepAlive</key>",
        "    <true/>",
        "    <key>ThrottleInterval</key>",
        "    <integer>30</integer>",
        "    <key>StandardOutPath</key>",
        "    <string>/tmp/teatree-loop.out.log</string>",
        "    <key>StandardErrorPath</key>",
        "    <string>/tmp/teatree-loop.err.log</string>",
        "</dict>",
        "</plist>",
        "",
    ]
    return "\n".join(lines)


# ── install / uninstall ──────────────────────────────────────────────


def install_watchdog(
    *,
    home: Path | None = None,
    label: str = DEFAULT_LABEL,
    t3_bin: str = "t3",
) -> Path:
    """Write the LaunchAgent plist and ``launchctl load`` it.

    Idempotent: an existing plist with identical content is left in
    place; otherwise it is overwritten and reloaded. ``launchctl``
    failures are surfaced as :class:`WatchdogError`.
    """
    home = home if home is not None else Path.home()
    path = launch_agent_plist_path(label=label, home=home)
    body = launch_agent_plist(label=label, t3_bin=t3_bin)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.is_file() and path.read_text(encoding="utf-8") == body
    path.write_text(body, encoding="utf-8")
    if not existed:
        _launchctl(["unload", str(path)], allow_fail=True)
        _launchctl(["load", str(path)])
    return path


def uninstall_watchdog(*, home: Path | None = None, label: str = DEFAULT_LABEL) -> None:
    """Unload and remove the LaunchAgent plist if it exists."""
    home = home if home is not None else Path.home()
    path = launch_agent_plist_path(label=label, home=home)
    if path.is_file():
        _launchctl(["unload", str(path)], allow_fail=True)
        path.unlink()


def _launchctl(args: list[str], *, allow_fail: bool = False) -> None:
    cmd = ["launchctl", *args]
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None)
    except FileNotFoundError as exc:
        if allow_fail:
            return
        msg = "launchctl not found — install the LaunchAgent manually or run on macOS."
        raise WatchdogError(msg) from exc
    except CommandFailedError as exc:
        if allow_fail:
            return
        raise WatchdogError(str(exc)) from exc
    if result.returncode != 0 and not allow_fail:
        msg = f"launchctl {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        raise WatchdogError(msg)
