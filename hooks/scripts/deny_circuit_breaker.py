"""Repeated-denial circuit breaker — stop runaway loops burning tokens (#2384 PR3).

A stuck session can hit the SAME gate denial over and over: a real session hit
one skill-loading denial 16 times consecutively across ~683 model turns, burning
~2M output / ~190M total tokens (cache re-reads dominate a runaway loop). The
model cannot satisfy a false/unsatisfiable demand by retrying, so it retries
forever. This breaker trips at the K-th CONSECUTIVE identical denial and breaks
the loop, tiered by gate class:

* UX / non-safety gates (allow-list — the skill-loading gate id at minimum):
    FAIL OPEN this one call so the loop can make progress, on the theory that K
    identical UX denials means the demand is false or unsatisfiable. The streak
    is reset so the next genuine denial starts a fresh count.
* SAFETY gates (everything NOT on the allow-list — merge/substrate,
    banned-terms, privacy/leak, out-of-band-merge, orchestrator-boundary): NEVER
    auto-relax. Keep denying, but escalate the reason so the model stops retrying
    and uses the documented self-rescue / escalates to the user.

State: a per-session ``<session>.deny-streak`` JSON file holding the current
denial fingerprint and its consecutive count, in the same STATE_DIR pattern as
``.pending`` / ``.skills``. A genuine ALLOW (a PreToolUse call that ran the whole
chain without a deny) resets the streak in ``main`` so only CONSECUTIVE identical
denials accumulate. The circuit-broken event is recorded as a durable
``loop_circuit_broken`` signal through the same per-session state-file seam the
SubagentStop no-commit signal uses (``<session>.circuit-broken``); a loud
one-line stderr warning is the live channel. Everything is wrapped so the breaker
is crash-proof and fast: on ANY internal error it falls back to the gate's
ORIGINAL decision — a breaker bug must never itself block a call nor wrongly
allow one.

Extracted whole from ``hook_router`` (the #2384 Wave-2 router split, PR3) so the
dispatcher shrinks; ``emit_pretooluse_deny`` keeps calling
:func:`apply_deny_circuit_breaker` and ``main`` keeps calling
:func:`reset_deny_streak`, both re-exported into the router unchanged.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the already-extracted ``state_files`` sibling — never Django / ``teatree.core``.
The shared spine helpers (``_state_file`` / ``_ensure_state_dir`` /
``_teatree_bool_setting`` / ``_teatree_int_setting``) and the per-process hook
context (``_current_hook_context``) stay in the router and are back-imported
lazily inside the function bodies — the ``hooks/scripts`` sibling back-import the
import-direction fitness test permits (it governs only the ``src/teatree/hooks``
leaves).
"""

import contextlib
import dataclasses
import hashlib
import json
import re
import sys

from state_files import append_line as _append_line
from state_files import read_lines as _read_lines

# Alias the bare and ``hooks.scripts.`` identities so the breaker the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("deny_circuit_breaker", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.deny_circuit_breaker", sys.modules[__name__])

_DENY_STREAK_SUFFIX = "deny-streak"
_CIRCUIT_BROKEN_SUFFIX = "circuit-broken"
_DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD = 3

# Reason-prefix markers identifying UX / non-safety gates that MAY auto-relax
# when looped. Conservative allow-list: a deny whose reason does not start with
# one of these is treated as a SAFETY gate and NEVER auto-opens. The
# skill-loading gate is the documented minimum.
_DENY_CIRCUIT_UX_GATE_PREFIXES: tuple[str, ...] = ("SKILL LOADING ENFORCEMENT", "LOOP REGISTRATION")

# Volatile substrings stripped from a deny reason before fingerprinting so "the
# same denial" matches across retries even when the reason embeds a changing
# SHA / path / count. Skill-name lists and gate identity are preserved (they ARE
# the denial's identity), so two DIFFERENT denials still fingerprint apart.
_DENY_FP_VOLATILE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),  # git SHAs
    re.compile(r"(?:/[^\s/]+)+/?"),  # absolute/relative paths
    re.compile(r"\b\d+\b"),  # bare counts/line numbers
)


@dataclasses.dataclass(frozen=True)
class _BreakerDecision:
    """Outcome of the circuit breaker for one deny.

    ``allow`` True means SUPPRESS the deny (auto-relax this call). ``reason`` is
    the (possibly escalation-augmented) reason to emit when ``allow`` is False.
    """

    allow: bool
    reason: str


def deny_circuit_breaker_enabled() -> bool:
    """Whether the repeated-denial circuit breaker is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the breaker keeps its
    protective default; an explicit ``false`` is the one-line kill-switch that
    makes the breaker a pure pass-through (never a code edit). Routes through the
    router's shared ``_teatree_bool_setting`` so every gate reads the bare-boolean
    config the same single way.
    """
    from hook_router import _teatree_bool_setting  # noqa: PLC0415, PLC2701

    return _teatree_bool_setting("deny_circuit_breaker_enabled", default=True)


def deny_circuit_breaker_threshold() -> int:
    """Consecutive-denial count K at which the breaker trips (default 3).

    DB-first read of ``[teatree] deny_circuit_breaker_threshold`` via the router's
    shared ``_teatree_int_setting`` adapter, TOML as never-lockout fallback.
    ``minimum=1`` keeps a non-positive / non-int value falling to the default so a
    malformed config can never disable the breaker by setting an impossible
    threshold.
    """
    from hook_router import _teatree_int_setting  # noqa: PLC0415, PLC2701

    return _teatree_int_setting(
        "deny_circuit_breaker_threshold", default=_DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD, minimum=1
    )


def _deny_gate_id(reason: str) -> str:
    """Stable gate identity derived from the deny reason's leading marker.

    A reason like ``SKILL LOADING ENFORCEMENT: …`` or ``BLOCKED: `nx serve` …``
    carries its gate identity in a leading marker that is constant across
    retries (the variable tail — the offending skill list / command — is part
    of the FINGERPRINT, not the gate id). The id is the marker up to the first
    ``:`` (or the first few words when there is none), normalised to a compact
    token so the same gate maps to the same id.
    """
    head = reason.split(":", 1)[0] if ":" in reason else " ".join(reason.split()[:4])
    return re.sub(r"\s+", "-", head.strip().lower())[:64] or "unknown-gate"


def deny_is_ux_gate(reason: str) -> bool:
    """True iff *reason* belongs to an allow-listed UX / non-safety gate.

    Conservative: anything not matching an allow-list prefix is a SAFETY gate
    and is never auto-relaxed by the breaker.
    """
    stripped = reason.lstrip()
    return any(stripped.startswith(prefix) for prefix in _DENY_CIRCUIT_UX_GATE_PREFIXES)


def _deny_fingerprint(gate_id: str, reason: str) -> str:
    """Stable short hash of gate identity + a volatility-normalised reason.

    Volatile substrings (SHAs, paths, bare counts) are stripped so the same
    logical denial fingerprints identically across retries, while a genuinely
    different denial (different gate, different unloaded-skill set) fingerprints
    apart. Returns a short hex digest; never raises.
    """
    normalised = reason
    for pattern in _DENY_FP_VOLATILE_RES:
        normalised = pattern.sub(" ", normalised)
    normalised = re.sub(r"\s+", " ", normalised).strip().lower()
    digest = hashlib.sha256(f"{gate_id}\x00{normalised}".encode()).hexdigest()
    return digest[:16]


def _bump_deny_streak(session_id: str, fingerprint: str) -> int:
    """Increment the consecutive-denial count for *fingerprint*; return the new count.

    If the stored fingerprint matches, the count increments; otherwise the
    streak resets to 1 for the new fingerprint. Crash-proof: any IO/JSON error
    is swallowed and the call is counted as 1 (a single isolated denial), so a
    state-file fault can never manufacture a trip nor crash the gate.
    """
    from hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415, PLC2701

    if not session_id:
        return 1
    path = _state_file(session_id, _DENY_STREAK_SUFFIX)
    try:
        _ensure_state_dir()
        count = 0
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("fp") == fingerprint:
                raw = stored.get("count", 0)
                count = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        new_count = count + 1
        path.write_text(json.dumps({"fp": fingerprint, "count": new_count}), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return 1
    return new_count


def reset_deny_streak(session_id: str) -> None:
    """Clear the per-session deny-streak so only CONSECUTIVE denials accumulate.

    Called on every ALLOWED PreToolUse call (genuine progress) and after the
    breaker relaxes a UX gate. Best-effort — a failure to clear is harmless (the
    next bump with a new fingerprint resets the count anyway).
    """
    from hook_router import _state_file  # noqa: PLC0415, PLC2701

    if not session_id:
        return
    with contextlib.suppress(OSError):
        _state_file(session_id, _DENY_STREAK_SUFFIX).unlink(missing_ok=True)


def _record_circuit_broken_signal(session_id: str, gate_id: str, fingerprint: str, count: int) -> None:
    r"""Persist + log one ``loop_circuit_broken`` signal (deduped by fingerprint).

    Durable channel: append a deduped ``<gate_id>\t<fingerprint>\t<count>`` line
    to the per-session ``<session>.circuit-broken`` state file — the same seam
    the SubagentStop no-commit signal uses (``<session>.no-commit``), which the
    PreCompact recovery snapshot already knows how to read back. Best-effort: a
    record failure must never propagate out of the deny path.
    """
    from hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415, PLC2701

    if not session_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        path = _state_file(session_id, _CIRCUIT_BROKEN_SUFFIX)
        for line in _read_lines(path):
            if line.split("\t", 1)[0] == fingerprint:
                return
        _append_line(path, f"{fingerprint}\t{gate_id}\t{count}")


def apply_deny_circuit_breaker(reason: str) -> _BreakerDecision:
    """Route one PreToolUse deny through the repeated-denial circuit breaker.

    Returns a :class:`_BreakerDecision`: ``allow=True`` means SUPPRESS the deny
    (a looped UX gate auto-relaxed this call); otherwise ``reason`` is the deny
    reason to emit (escalation-augmented for a looped safety gate).

    Crash-proof: on a disabled breaker, a non-PreToolUse invocation, or ANY
    internal error, the original deny is preserved unchanged (fall back to the
    gate's original decision — the breaker never blocks nor wrongly allows on
    its own fault).
    """
    from hook_router import _current_hook_context  # noqa: PLC0415, PLC2701

    try:
        event, data = _current_hook_context()
        if event != "PreToolUse" or not deny_circuit_breaker_enabled():
            return _BreakerDecision(allow=False, reason=reason)
        session_id = data.get("session_id", "") if isinstance(data, dict) else ""
        threshold = deny_circuit_breaker_threshold()
        gate_id = _deny_gate_id(reason)
        fingerprint = _deny_fingerprint(gate_id, reason)
        count = _bump_deny_streak(session_id, fingerprint)
        if count < threshold:
            return _BreakerDecision(allow=False, reason=reason)

        _record_circuit_broken_signal(session_id, gate_id, fingerprint, count)
        if deny_is_ux_gate(reason):
            sys.stderr.write(
                f"CIRCUIT BREAKER: gate '{gate_id}' denied {count} times consecutively "
                "— auto-relaxing this call to break the loop; root cause is likely a "
                "false or unsatisfiable demand. Investigate the gate, do not just retry.\n"
            )
            reset_deny_streak(session_id)
            return _BreakerDecision(allow=True, reason=reason)

        sys.stderr.write(
            f"CIRCUIT BREAKER: safety gate '{gate_id}' denied {count} times consecutively "
            "— NOT auto-relaxing a safety gate; escalating to break the loop.\n"
        )
        escalation = (
            f"\n\nCIRCUIT BREAKER: this identical call has been denied {count} times — "
            "you are LOOPING. STOP retrying; retrying only burns tokens and changes "
            "nothing. Use the documented self-rescue / escape, or escalate to the user."
        )
        return _BreakerDecision(allow=False, reason=f"{reason}{escalation}")
    except Exception:  # noqa: BLE001 — breaker failure falls back to the gate's original deny.
        return _BreakerDecision(allow=False, reason=reason)
