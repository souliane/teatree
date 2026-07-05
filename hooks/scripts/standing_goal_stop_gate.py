"""Stop: standing verified-green stop-gate (PR-25, M8).

While ≥ 1 active :class:`~teatree.core.models.standing_goal.StandingGoal` exists,
this Stop gate re-runs each goal's ``check_command`` at turn-end (short timeout,
briefly cached). If a goal is unmet, it DENIES the stop — the deny leads with the
blunt binary ("``<goal>`` green? NO."), instructs the agent to keep driving OR to
surface the blocker and hold, and MINTS a single-use escape token. A passing
check auto-clears the deny AND retires the goal (``active`` → False). Deferring
via a question or a win-led partial report does NOT bypass — the deny re-fires on
every subsequent stop while unmet.

NEVER-LOCKOUT (critical for a stop-gate — it must fail-open / self-rescue, never
hard-lock the session):

* fires ONLY on a loop-driven turn (an attended human turn is never blocked);
* per-call single-use token — end the turn with
    ``[standing-goal-hold: <token> <reason>]`` to hold this ONE stop; the token
    dies after one use, so it is never a blanket bypass;
* kill-switch ``[teatree] standing_goal_stop_gate_enabled = false`` (flipped by
    ``t3 <overlay> gate standing-goal disable``);
* crash-proof per the Stop-hook contract — ANY internal error, an unbootstrappable
    Django, an unreadable goal, or a check that times out / errors all ALLOW the
    stop (fail-open). Only a check that ran and exited NON-ZERO denies.

The detection stays in this thin transcript/DB-reading wrapper; the model and its
manager own the persistence. Fail-safe-to-silent on any error (a Stop hook must
NEVER crash turn-end).
"""

import contextlib
import json
import re
import secrets
import subprocess  # noqa: S404 — runs the operator-registered green check_command by design.
import sys
import time
from pathlib import Path

# Alias both identities so a bare ``from standing_goal_stop_gate import ...`` (the
# live hook, whose dir is on sys.path) and ``hooks.scripts.standing_goal_stop_gate``
# (a subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("standing_goal_stop_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.standing_goal_stop_gate", sys.modules[__name__])

# ``[standing-goal-hold: <token> <reason>]`` — the token then a non-empty reason.
_HOLD_TOKEN_RE = re.compile(r"\[standing-goal-hold:\s*(\S+)\s+(\S[^\]]*?)\s*\]")

_HOLD_SUFFIX = "standing-goal-hold"
_CHECK_CACHE_SUFFIX = "standing-goal-check-cache"
_CHECK_TIMEOUT_SECONDS = 15
_CHECK_CACHE_TTL_SECONDS = 15
_USED_TOKEN_CAP = 50


def _gate_enabled() -> bool:
    from teatree_settings import teatree_bool_setting  # noqa: PLC0415 — deferred import

    return teatree_bool_setting("standing_goal_stop_gate_enabled", default=True)


def handle_standing_goal_stop(data: dict) -> bool | None:
    """Block a Stop while a registered standing goal is unmet; else allow.

    Returns ``True`` (emitting a ``decision: block``) only when an active goal's
    check ran and exited non-zero AND the turn carries no valid single-use hold
    token. Otherwise returns ``None`` so the session may end normally. Fail-safe-
    to-silent: any malformed input or unexpected error returns ``None``.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run(data)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _gate_is_out_of_scope(data: dict) -> bool:
    """True when this Stop turn is exempt before touching the DB (mirrors the completion gate).

    A ``stop_hook_active`` re-fire (avoids a hot loop), an attended (non-loop-driven)
    turn a human reads, and the kill-switch being off each skip the gate.
    """
    from hook_router import _session_drives_loop  # noqa: PLC0415, PLC2701

    if data.get("stop_hook_active"):
        return True
    if not _session_drives_loop(data.get("session_id", "")):
        return True
    return not _gate_enabled()


def _run(data: dict) -> bool | None:
    from django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 — deferred import
    from hook_router import _last_assistant_turn  # noqa: PLC0415, PLC2701

    if _gate_is_out_of_scope(data):
        return None
    if not bootstrap_teatree_django():
        return None
    unmet = _first_unmet_goal()
    if unmet is None:
        return None
    session_id = data.get("session_id", "")
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    text = turn[0] if turn else ""
    token = _hold_token(text)
    if token and _valid_hold(session_id, token):
        _consume_hold(session_id, token)
        sys.stderr.write(f"NOTE: standing-goal stop honoured a single-use hold token while {unmet!r} is unmet.\n")
        return None
    minted = _mint_hold(session_id)
    json.dump({"decision": "block", "reason": _format_deny(unmet, minted)}, sys.stdout)
    return True


def _first_unmet_goal() -> str | None:
    """The name of the first active goal whose check FAILED, or ``None``.

    A goal whose check PASSED is auto-retired (best-effort); a goal whose check
    could not run (timeout / error) is treated as not-unmet (fail-open — a broken
    check never wedges the stop). Results are briefly cached so rapid successive
    stops do not re-run an expensive check.
    """
    from teatree.core.models import StandingGoal  # noqa: PLC0415 — deferred import

    goals = list(StandingGoal.objects.active_goals())
    if not goals:
        return None
    cache = _load_check_cache()
    now = time.time()
    fresh = now - float(cache.get("ts", 0.0)) < _CHECK_CACHE_TTL_SECONDS
    results: dict[str, str] = dict(cache.get("results", {})) if fresh else {}
    unmet: str | None = None
    dirty = False
    for goal in goals:
        key = f"{goal.name}\x00{goal.check_command}"
        verdict = results.get(key)
        if verdict is None:
            verdict = _evaluate_goal(goal.check_command)
            results[key] = verdict
            dirty = True
        if verdict == "pass":
            _retire_goal(goal.name)
        elif verdict == "fail" and unmet is None:
            unmet = goal.name
    if dirty:
        _save_check_cache({"ts": now, "results": results})
    return unmet


def _evaluate_goal(command: str) -> str:
    """Run *command*; ``"pass"`` (exit 0), ``"fail"`` (non-zero), ``"error"`` (timeout/exception).

    The green ``check_command`` is expected to be FAST (a status probe / marker
    check), not a full suite run — it must finish inside the short timeout well
    under the Stop-hook's 30s ceiling. A timeout or a raised error is ``"error"``
    (fail-open, no deny), never a false ``"fail"``.
    """
    try:
        result = subprocess.run(  # noqa: S602 — operator-registered command, run by design.
            command,
            shell=True,
            capture_output=True,
            timeout=_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001 — a broken/slow check must never deny (never-lockout).
        sys.stderr.write("NOTE: standing-goal check could not be evaluated (timeout/error) — failing open.\n")
        return "error"
    return "pass" if result.returncode == 0 else "fail"


def _retire_goal(name: str) -> None:
    """Best-effort auto-retire of a goal whose check passed; never raises."""
    try:
        from teatree.core.models import StandingGoal  # noqa: PLC0415 — deferred import

        StandingGoal.objects.retire(name)
    except Exception:  # noqa: BLE001 — a retire failure must never crash the Stop gate.
        return


def _format_deny(goal_name: str, token: str) -> str:
    """The blunt-binary deny reason that mints the single-use escape token."""
    return (
        f"`{goal_name}` green? NO. — the standing verified-green goal is unmet, so this stop is a "
        "CHECKPOINT, not done. A status report is not the deliverable.\n"
        "Either KEEP DRIVING the next fix toward green, OR surface the specific blocker and HOLD "
        "(the goal stays open) by ending your turn with the single-use token below:\n"
        f"    [standing-goal-hold: {token} <one-line reason>]\n"
        "The token dies after one use, so a hold is one checkpoint — not a blanket bypass; the deny "
        "re-fires on the next stop while the goal is unmet.\n"
        "To retire the goal or disable the gate: `t3 <overlay> goal clear <name>` / "
        "`t3 <overlay> gate standing-goal disable`."
    )


# ── single-use hold-token state ────────────────────────────────────────────


def _hold_token(text: str) -> str | None:
    """The token from a ``[standing-goal-hold: <token> <reason>]`` in the turn text, else None."""
    match = _HOLD_TOKEN_RE.search(text)
    if match is None:
        return None
    token = match.group(1).strip()
    reason = match.group(2).strip()
    return token if token and reason else None


def _hold_state(session_id: str) -> dict | None:
    """The parsed hold-state (``{"minted": tok, "used": [...]}``) or ``None`` on an IO/JSON error.

    ``None`` (unreadable state) is distinct from ``{}`` (no state yet): the caller
    fail-opens the escape on ``None`` so a broken state dir never wedges a hold.
    """
    from hook_router import _state_file  # noqa: PLC0415, PLC2701

    path = _state_file(session_id, _HOLD_SUFFIX)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else {}


def _valid_hold(session_id: str, token: str) -> bool:
    """Whether *token* is the currently-minted, not-yet-consumed hold token.

    Fail-open: an unreadable state (``None``) accepts any well-formed token so a
    broken state dir can never lock the session out of the escape.
    """
    state = _hold_state(session_id)
    if state is None:
        return True
    used = state.get("used", [])
    return token == state.get("minted") and token not in used


def _consume_hold(session_id: str, token: str) -> None:
    """Mark *token* used single-use and clear the minted slot; best-effort, never raises."""
    from hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415, PLC2701

    try:
        _ensure_state_dir()
        state = _hold_state(session_id) or {}
        used = [*state.get("used", []), token][-_USED_TOKEN_CAP:]
        path = _state_file(session_id, _HOLD_SUFFIX)
        path.write_text(json.dumps({"minted": None, "used": used}), encoding="utf-8")
    except OSError:
        return


def _mint_hold(session_id: str) -> str:
    """Mint a fresh single-use token, persist it as the active minted slot, and return it.

    Best-effort persistence: a write failure still returns the token (the deny
    surfaces it and the fail-open branch of :func:`_valid_hold` accepts it next
    stop), so a broken state dir never denies the agent an escape.
    """
    token = secrets.token_hex(4)
    from hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415, PLC2701

    try:
        _ensure_state_dir()
        state = _hold_state(session_id) or {}
        path = _state_file(session_id, _HOLD_SUFFIX)
        path.write_text(json.dumps({"minted": token, "used": state.get("used", [])}), encoding="utf-8")
    except OSError:
        return token
    return token


# ── brief check-result cache ───────────────────────────────────────────────


def _load_check_cache() -> dict:
    """The parsed check-result cache (``{"ts": float, "results": {...}}``); ``{}`` on any error."""
    from hook_router import _state_file  # noqa: PLC0415, PLC2701

    path = _state_file("global", _CHECK_CACHE_SUFFIX)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_check_cache(payload: dict) -> None:
    """Persist the check-result cache; best-effort, never raises."""
    from hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415, PLC2701

    try:
        _ensure_state_dir()
        _state_file("global", _CHECK_CACHE_SUFFIX).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return
