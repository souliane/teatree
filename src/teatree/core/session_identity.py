"""The active Claude session id — the loop's session-scoped identity (#1073).

Loop ownership is keyed by the *Claude session* that started the loop, NOT
the OS pid: ``loops_tick`` re-acquires the per-tick mutex with a fresh
``pid-<pid>`` every tick, so a pid-keyed identity rests unowned between
ticks and any session running ``t3 loops tick`` would win the unowned CAS
and do loop work (the #1073 hijack). The session id is stable across a
session's ticks and across context compaction, so it is the correct
identity for the persistent ``t3-master`` claim.

This is the single source of truth for that primitive; both
``teatree.loop.session_identity`` (the loop-side callers) and
``teatree.outbound_claim`` (``_resolve_agent_session_id``) re-export it.
It lives in ``teatree.core`` rather than ``teatree.loop`` because the
module-boundary graph forbids ``teatree.outbound_claim`` (core-only)
depending on ``teatree.loop``; ``core`` is the lowest common module both
re-exporters are allowed to reach.

#1107 — the headline incident root cause: Claude Code delivers the
session id ONLY in the hook JSON payload, never as an env var inside a
Bash-tool subprocess, so in agent-driven mode the session-id env vars are
empty and ``current_session_id()`` returned ``""`` → ``t3 loop claim``
hard-refused → t3-master could never be claimed → every owner-gated slot
(#1075 reactive answer, self-improve, the claim-next spawn pump) was
permanently dead (131 user DMs reacted/answered never). The precedence is
now:

    ``CLAUDE_SESSION_ID`` → ``CLAUDE_CODE_SESSION_ID`` → ``T3_LOOP_SESSION_ID`` → loop-registry → ``""``

#3554 — Claude Code exports the live session id as ``CLAUDE_CODE_SESSION_ID``,
not ``CLAUDE_SESSION_ID``, so a resolver reading only the old name fell
through to ``""`` in every interactive session (``handover create``
refused, ``loop whoami`` reported no session). ``CLAUDE_SESSION_ID`` is
kept ahead of it for backward compatibility. The accepted names live in
one place — :data:`SESSION_ID_ENV_VARS` — so a future upstream rename is a
one-line change caught by a single pinning test rather than silent
degradation at every call site.

The durable session *pid* (the t3-master lease anchor) resolves with a
parallel precedence so an env-restricted subprocess that cannot read the
registry still gets the long-lived session pid rather than the transient
tick-shell pid (#1722):

    ``T3_LOOP_SESSION_PID`` → loop-registry → ``None``

The registry fallback is correct-not-hack: ``loop-registry.json``'s
``t3-loop-tick-owner`` record IS the durable owner-identity the
``hook_router`` writes at ``SessionStart``, so the claim path resolving
its principal from it removes an inconsistency rather than inventing a
new identity. It is an identity SOURCE only — the t3-master gate itself
reads the DB lease (:mod:`teatree.core.gates.t3_master_gate`, #3968), because
nothing prunes this file and a stale record locked the reactive loops out
permanently. The module-boundary graph forbids ``teatree.core`` importing
``teatree.loop``/hooks, so the registry key constant is deliberately
redeclared here; it is read with only ``os``/``pathlib`` + ``json`` and
fails open (any OSError/JSON error → ``""``).
"""

import json
import os
from pathlib import Path

from teatree.utils.hook_registry import loop_registry_dir

# The session id lands under whichever name the harness exports it, most-
# to least-preferred. ``CLAUDE_CODE_SESSION_ID`` is what a live Claude Code
# session exports (#3554); ``CLAUDE_SESSION_ID`` is the legacy name kept for
# backward compatibility; ``T3_LOOP_SESSION_ID`` is the test/manual override.
# Single source of truth: ``teatree.hooks._hook_state`` redeclares the same
# list (it cannot import across the module-boundary graph) and a test pins
# the two in sync.
SESSION_ID_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "T3_LOOP_SESSION_ID",
)

#: The loop runner's own durable principal (#3810). The runner (``t3 worker``)
#: is a long-lived daemon with NO Claude session, so every resolver above comes
#: back empty for it and :func:`current_session_id` fell through to the loop
#: registry — a shared file EVERY ``SessionStart`` hook rewrites. The runner's
#: identity therefore rotated between its OWN consecutive ticks: tick N claimed
#: ``loop:<name>`` under registry id A, a foreign session started and the
#: record became B, and tick N+1 read B, found A's still-live lease and
#: SKIPped. The loop then ran once per lease TTL instead of once per cadence,
#: and a ``claim`` could not be matched by the ``release`` seconds later because
#: the two reads of that shared file disagreed.
#:
#: A CONSTANT (not a per-process uuid) is deliberate: there is exactly one loop
#: runner per teatree deployment, and its restarts must NOT rotate its identity
#: — a restarted runner claiming under a fresh id would be locked out of its own
#: leases for a full TTL, which is the very starvation this fixes. Liveness is
#: carried by :data:`RUNNER_PID_ENV` instead, which is what a pid is for.
LOOP_RUNNER_SESSION_ID = "loop-runner"

#: Env vars carrying the runner principal into a tick subprocess. They are read
#: as a PAIR and outrank every session-id source, so a runner launched from
#: inside a Claude session never inherits that session's identity for work the
#: runner — not the session — is doing.
RUNNER_SESSION_ENV = "T3_LOOP_RUNNER_SESSION"
RUNNER_PID_ENV = "T3_LOOP_RUNNER_PID"

# Deliberately redeclared (not imported) — ``teatree.core`` must not
# depend on ``teatree.loop``/hooks. Mirrors ``hook_router._OWNER_LOOP``
# and the literal already accepted in ``loop_slack_answer``. The
# ``gitleaks:allow`` is a false-positive suppression: a registry slot
# name, not a credential (same literal lives in ``loop_slack_answer.py``
# line 51 as a dict-key arg and goes unflagged there).
_OWNER_KEY = "t3-loop-tick-owner"  # gitleaks:allow


def _loop_registry_path() -> Path:
    """The tick-owner registry the hook tier writes."""
    return loop_registry_dir() / "loop-registry.json"


def _owner_record_from_loop_registry() -> dict | None:
    """Read the tick-owner record from the loop registry, ``None`` on any error.

    Fail-open spans the whole resolve+read because ``_loop_registry_path``
    can itself raise (``Path.home()`` raises ``RuntimeError`` when neither
    ``HOME`` nor ``XDG_DATA_HOME`` nor ``T3_LOOP_REGISTRY_DIR`` is set —
    seen in CI sandboxes that clear the env). A read failure here must
    NEVER block claim resolution; the right behaviour is "no registry
    fallback available → return ``None``" so the caller proceeds to its
    own no-fallback outcome rather than crashing.
    """
    try:
        path = _loop_registry_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError, RuntimeError):
        return None
    owner = data.get(_OWNER_KEY) if isinstance(data, dict) else None
    return owner if isinstance(owner, dict) else None


def owner_record() -> dict | None:
    """The durable ``t3-loop-tick-owner`` registry record, or ``None`` (public accessor).

    Exposes the same record :func:`_session_id_from_loop_registry` /
    :func:`_pid_from_loop_registry` read (session id + pid of the tick owner)
    so the driver-detection seam (:func:`teatree.loop.driver_detection.detect_driver`)
    can decide self-pump without redeclaring the registry path logic a third
    time. Fails open to ``None`` on any read error.
    """
    return _owner_record_from_loop_registry()


def _session_id_from_loop_registry() -> str:
    """The tick-owner's durable session id from the loop registry, ``""`` on any error."""
    owner = _owner_record_from_loop_registry()
    return (owner or {}).get("session_id") or ""


def _coerce_pid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        pid = value
    elif isinstance(value, str) and value.strip().isdigit():
        pid = int(value)
    else:
        return None
    return pid if pid > 0 else None


def _pid_from_loop_registry() -> int | None:
    return _coerce_pid((_owner_record_from_loop_registry() or {}).get("pid"))


def current_session_pid() -> int | None:
    """The owning *session* process pid, or ``None`` when not resolvable (#1073).

    The persistent ``t3-master`` lease is anchored to the session that
    started the loop, so its ``owner_pid`` must be that session's
    long-lived process — NOT ``os.getppid()`` of the ephemeral tick
    subprocess. ``t3 loop tick`` runs inside a Bash-tool shell that the
    harness spawns per tool call and tears down seconds later, so
    ``os.getppid()`` there is a transient shell pid that is dead almost
    immediately. Anchoring the lease on it makes the pid-liveness check
    (``_session_lease_is_live``) see a dead owner within seconds of every
    tick, collapsing the pid-anchored protection back to TTL-only: once
    the 30-min TTL lapses for a busy/idle owner, a fresh SessionStart
    finds "no live owner" and STEALS the loop.

    Precedence mirrors :func:`current_session_id`:

        ``T3_LOOP_SESSION_PID`` env → loop-registry owner record → ``None``

    The Stop self-pump exports ``T3_LOOP_SESSION_PID`` (the durable session
    pid, the same value ``SessionStart`` records in the registry) into the
    tick command, so the env path resolves the durable pid even in an
    env-restricted subprocess where the loop registry is unreadable. The
    registry path is the lower-precedence fallback: the ``SessionStart``
    hook records the durable session pid alongside the session id
    (``hook_router._tick_owner_record`` stores ``os.getppid()`` of the
    SessionStart hook, whose parent IS the persistent session process).
    Without either source the resolver returns ``None`` so the caller
    decides its own no-fallback outcome rather than silently anchoring on
    the transient tick-shell pid.
    """
    return _coerce_pid(os.environ.get("T3_LOOP_SESSION_PID")) or _pid_from_loop_registry()


def session_id_from_env() -> str | None:
    """The active session id from :data:`SESSION_ID_ENV_VARS`, ``None`` when none is set.

    The env-only slice of the resolver, shared with the loop-slot commands
    (``loop_slack_answer`` / ``loop_self_improve``) whose t3-master gate
    consults the registry separately and only needs the env value.
    """
    for name in SESSION_ID_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def current_session_id() -> str:
    """The active Claude session id, or ``""`` when not resolvable.

    Claude Code exports the id as ``CLAUDE_CODE_SESSION_ID`` (#3554);
    ``CLAUDE_SESSION_ID`` is the legacy name kept ahead of it for backward
    compatibility, and ``T3_LOOP_SESSION_ID`` is the test/manual override
    (all three in :data:`SESSION_ID_ENV_VARS`). When none is set (#1107:
    agent-driven Bash-tool subprocesses never see the id as an env var) the
    loop registry's ``t3-loop-tick-owner`` record is the lowest-precedence
    fallback. Empty string means anonymous (no session) — the t3-master
    gate treats an anonymous caller as a non-owner whenever a live owner
    exists.
    """
    return session_id_from_env() or _session_id_from_loop_registry() or ""


def runner_identity_env(pid: int) -> dict[str, str]:
    """The env pair a loop-runner-spawned subprocess carries to declare its principal.

    Written by the runner at spawn time (:func:`teatree.loops.deadlined_tick.
    tick_subprocess_env`) and read back by :func:`runner_principal` in the child.
    """
    return {RUNNER_SESSION_ENV: LOOP_RUNNER_SESSION_ID, RUNNER_PID_ENV: str(pid)}


def runner_principal() -> tuple[str, int] | None:
    """``(session_id, owner_pid)`` when this process runs as the loop runner, else ``None``.

    Both env vars must be present and the pid well-formed: a half-set pair is
    treated as absent so a partially-inherited environment can never produce a
    principal with no checkable process behind it.
    """
    session_id = (os.environ.get(RUNNER_SESSION_ENV) or "").strip()
    pid = _coerce_pid(os.environ.get(RUNNER_PID_ENV))
    if not session_id or pid is None:
        return None
    return session_id, pid


def loop_principal() -> tuple[str, int | None]:
    """``(session_id, owner_pid)`` for whoever is claiming a loop lease right now (#3810).

    The ONE identity seam every loop-ownership call site shares — the per-loop
    tick, ``t3 loop claim`` and ``t3 loop release`` — so a claim and the release
    that follows it can never resolve to two different principals. The loop
    runner's explicit principal wins; otherwise this is a Claude session and the
    session resolvers answer exactly as before.

    Returns ``("", None)`` when nothing resolves. Callers decide what an
    anonymous principal means for them: the tick treats it as the #1107
    pure-cron case, ``t3 loop claim`` refuses outright.
    """
    runner = runner_principal()
    if runner is not None:
        return runner
    return current_session_id(), current_session_pid()


def is_loop_runner_session(session_id: str) -> bool:
    """Whether *session_id* is the ``t3 worker``'s durable principal (#3968).

    Every boundary that asks "is a COMPETING session holding this?" routes here,
    because the worker is the machine-wide driver rather than a competitor: it owns
    ``t3-master`` while it drives ticks, so a boundary that reads it as a rival
    locks every interactive session out — of the reactive-loop cycles, of the
    tick-owner election, and of a clean statusline.
    """
    return session_id == LOOP_RUNNER_SESSION_ID


__all__ = [
    "LOOP_RUNNER_SESSION_ID",
    "RUNNER_PID_ENV",
    "RUNNER_SESSION_ENV",
    "SESSION_ID_ENV_VARS",
    "current_session_id",
    "current_session_pid",
    "is_loop_runner_session",
    "loop_principal",
    "owner_record",
    "runner_identity_env",
    "runner_principal",
    "session_id_from_env",
]
