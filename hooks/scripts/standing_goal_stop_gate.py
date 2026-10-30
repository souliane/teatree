"""Stop: standing verified-green stop-gate (PR-25, M8).

While ≥ 1 active :class:`~teatree.core.models.standing_goal.StandingGoal` exists,
this Stop gate re-runs each goal's ``check_command`` at turn-end (short timeout,
briefly cached). If a goal is unmet, it DENIES the stop — the deny leads with the
blunt binary ("``<goal>`` green? NO."), instructs the agent to keep driving OR to
surface the blocker and hold, and offers an audited, per-action escape. A passing
check auto-clears the deny AND retires the goal (``active`` → False). Deferring
via a question or a win-led partial report does NOT bypass — the deny re-fires on
every subsequent stop while unmet.

NEVER-LOCKOUT (critical for a stop-gate — it must fail-open / self-rescue, never
hard-lock the session):

* fires ONLY on a loop-driven turn (an attended human turn is never blocked);
* per-call ``[gate-escape:standing_goal_stop: <reason>]`` token, audited before allow;
* kill-switch ``[teatree] standing_goal_stop_gate_enabled = false`` (flipped by
    ``t3 <overlay> gate standing-goal disable``);
* crash-proof per the Stop-hook contract — ANY internal error, an unbootstrappable
    Django, an unreadable goal, or a check that times out / errors all ALLOW the
    stop (fail-open). Only a check that ran and exited NON-ZERO denies.

The detection stays in this thin transcript/DB-reading wrapper; the model and its
manager own the persistence. Fail-safe-to-silent on any error (a Stop hook must
NEVER crash turn-end).
"""

import json
import subprocess  # noqa: S404 — runs the operator-registered green check_command by design.
import sys
import time

from hooks.scripts.django_bootstrap import orm_failures
from hooks.scripts.gate_escape import escape_allows
from hooks.scripts.gate_result import warn_gate_skipped
from hooks.scripts.managed_repo import teatree_src_on_path

# Alias both identities so a bare ``from standing_goal_stop_gate import ...`` (the
# live hook, whose dir is on sys.path) and ``hooks.scripts.standing_goal_stop_gate``
# (a subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("standing_goal_stop_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.standing_goal_stop_gate", sys.modules[__name__])

_CHECK_CACHE_SUFFIX = "standing-goal-check-cache"
_CHECK_TIMEOUT_SECONDS = 15
_CHECK_CACHE_TTL_SECONDS = 15


def _gate_enabled() -> bool:
    from hooks.scripts.teatree_settings import teatree_bool_setting  # noqa: PLC0415 deferred cold-hook import

    return teatree_bool_setting("standing_goal_stop_gate_enabled")


def handle_standing_goal_stop(data: dict) -> bool | None:
    """Block a Stop while a registered standing goal is unmet; else allow.

    Returns ``True`` (emitting a ``decision: block``) only when an active goal's
    check ran and exited non-zero AND no audited escape was recorded.
    Otherwise returns ``None`` so the session may end normally. An ORM failure
    is reported as SKIPPED and returns ``None``; any other error reaches the router's
    crash boundary, which allows the stop.
    """
    try:
        with teatree_src_on_path():
            return _run(data)
    except orm_failures() as exc:
        warn_gate_skipped("standing goal Stop", f"the ORM check failed ({type(exc).__name__}: {exc})")
        return None


def _gate_is_out_of_scope(data: dict) -> bool:
    """True when this Stop turn is exempt before touching the DB (mirrors the completion gate).

    A ``stop_hook_active`` re-fire (avoids a hot loop), an attended (non-loop-driven)
    turn a human reads, and the kill-switch being off each skip the gate.
    """
    from hooks.scripts.hook_router import _session_drives_loop  # noqa: PLC0415 deferred back-import

    if data.get("stop_hook_active"):
        return True
    if not _session_drives_loop(data.get("session_id", "")):
        return True
    return not _gate_enabled()


def _run(data: dict) -> bool | None:
    from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 deferred cold-hook import
    from hooks.scripts.hook_router import _last_assistant_turn  # noqa: PLC0415 deferred back-import

    if _gate_is_out_of_scope(data):
        return None
    if not bootstrap_teatree_django():
        warn_gate_skipped("standing goal Stop", "the hook interpreter cannot read the ORM control DB")
        return None
    unmet = _first_unmet_goal()
    if unmet is None:
        return None
    turn = _last_assistant_turn(data.get("transcript_path", ""))
    text = turn[0] if turn else ""
    if escape_allows(data, "standing_goal_stop", turn_text=text):
        return None
    json.dump({"decision": "block", "reason": _format_deny(unmet)}, sys.stdout)
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


def _format_deny(goal_name: str) -> str:
    return (
        f"`{goal_name}` green? NO. — the standing verified-green goal is unmet, so this stop is a "
        "CHECKPOINT, not done. A status report is not the deliverable.\n"
        "Either KEEP DRIVING the next fix toward green, OR surface the specific blocker and HOLD "
        "(the goal stays open) by ending your turn with "
        "`[gate-escape:standing_goal_stop: <reason>]`. The escape is audited before this one stop; "
        "the deny re-fires on the next stop while the goal is unmet.\n"
        "To retire the goal or disable the gate: `t3 goal clear <name>` / "
        "`t3 <overlay> gate standing-goal disable`."
    )


# ── brief check-result cache ───────────────────────────────────────────────


def _load_check_cache() -> dict:
    """The parsed check-result cache (``{"ts": float, "results": {...}}``); ``{}`` on any error."""
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

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
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    try:
        _ensure_state_dir()
        _state_file("global", _CHECK_CACHE_SUFFIX).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return
