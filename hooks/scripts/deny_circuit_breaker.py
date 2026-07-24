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

from hooks.scripts.state_files import append_line as _append_line
from hooks.scripts.state_files import read_lines as _read_lines

# Alias the bare and ``hooks.scripts.`` identities so the breaker the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("deny_circuit_breaker", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.deny_circuit_breaker", sys.modules[__name__])

_DENY_STREAK_SUFFIX = "deny-streak"
_CIRCUIT_BROKEN_SUFFIX = "circuit-broken"
_FP_GRANT_SUFFIX = "fp-grants"
_DENY_CIRCUIT_BREAKER_DEFAULT_THRESHOLD = 3

# ``[fp-confirmed: <non-empty-reason>]`` in the CURRENT tool call is the agent's
# confirmation that a repeated deny is a false positive (#3252). It suppresses
# THIS deny and records a session-scoped, per-fingerprint grant so the IDENTICAL
# false positive stops re-prompting on every subsequent identical action —
# without re-authorising a *different* gated action. An empty reason does not
# confirm.
_FP_CONFIRMED_RE = re.compile(r"\[fp-confirmed:\s*\S[^\]]*?\s*\]")

# Escape / override tokens stripped from a call signature before fingerprinting,
# so the SAME command with-and-without an escape token maps to ONE fingerprint.
# The confirm-once-then-reuse contract (#3252) needs the tokened confirming call
# and the later tokenless call to share a fingerprint; the other tokens are
# folded in so an escape marker never splits a genuine retry loop into two.
# Canonical catalog of every escape marker + kill-switch: hooks/CLAUDE.md
# § "Escape markers & kill-switches".
_SIGNATURE_STRIP_RE = re.compile(
    r"\[(?:fp-confirmed|fg-ok|skip-skill-gate|skill-load-ok|skip-plan-gate|quote-ok|reviewer-ok|config-overwrite-ok):[^\]]*\]"
    r"|\b(?:ALLOW_BANNED_TERM|QUOTE_OK|T3_MR_VALIDATE_ALLOW_BROKEN_ENV)=\S+"
)

# The PUBLIC-egress leak gate is fail-CLOSED always (BLUEPRINT §17 hard
# invariant): its deny is NEVER suppressible by a confirmed-FP grant. A denied
# leak can only be a privacy regression, not a false positive worth granting.
_LEAK_GATE_MARKERS: tuple[str, ...] = ("banned-terms", "quote-scanner", "leak")

# Tool-input fields a call may carry the ``[fp-confirmed:]`` token in, mirroring
# the skill-loading gate's per-call token surface (command for Bash;
# new_string / content / file_path for Edit / Write). Each is capped so a huge
# pasted body cannot slow the fast hook.
_TOKEN_FIELDS: tuple[str, ...] = ("command", "new_string", "content", "file_path")
_TOKEN_SCAN_CAP = 512

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
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("deny_circuit_breaker_enabled", default=True)


def deny_circuit_breaker_threshold() -> int:
    """Consecutive-denial count K at which the breaker trips (default 3).

    DB-first read of ``[teatree] deny_circuit_breaker_threshold`` via the router's
    shared ``_teatree_int_setting`` adapter, TOML as never-lockout fallback.
    ``minimum=1`` keeps a non-positive / non-int value falling to the default so a
    malformed config can never disable the breaker by setting an impossible
    threshold.
    """
    from hooks.scripts.hook_router import _teatree_int_setting  # noqa: PLC0415 deferred back-import

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


def _call_signature(data: dict) -> str:
    """Verbatim-ish identity of the CURRENT tool call, distinguishing commands (#3252).

    A deny reason alone is NOT enough to tell a genuine retry loop (the SAME
    command re-issued) from a batch of DISTINCT commands that happen to trip one
    command-independent gate (``--no-verify`` on three different commits, or a
    denied self-bypass followed by unrelated calls) — the latter shares a reason
    and, on a reason-only fingerprint, falsely accumulates a streak that poisons
    the distinct later commands. Folding this signature into the fingerprint
    keeps distinct commands apart while a real retry (identical call) still
    matches. Bash keys on the command, Edit / Write on the file path, everything
    else on the tool name — with escape/override tokens stripped so a call
    with-and-without a token shares one fingerprint. Never raises.
    """
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_name = data.get("tool_name", "") if isinstance(data, dict) else ""
    if tool_name == "Bash":
        identity = str(tool_input.get("command", ""))
    elif tool_name in {"Edit", "Write"}:
        identity = str(tool_input.get("file_path", ""))
    else:
        identity = tool_name
    # ALL whitespace is removed (not collapsed) so a stripped token can never
    # leave a stray gap that splits the tokened-vs-tokenless fingerprint.
    stripped = _SIGNATURE_STRIP_RE.sub(" ", identity)
    return re.sub(r"\s+", "", stripped).lower()


def _deny_fingerprint(gate_id: str, reason: str, signature: str) -> str:
    """Stable short hash of gate identity + a volatility-normalised reason + call signature.

    Volatile substrings (SHAs, paths, bare counts) are stripped from the REASON
    so the same logical denial fingerprints identically across retries, while a
    genuinely different denial (different gate, different unloaded-skill set)
    fingerprints apart. The call *signature* is folded in verbatim (#3252) so two
    DISTINCT commands that trip one command-independent gate never share a
    streak — a denied self-bypass no longer poisons unrelated later commands.
    Returns a short hex digest; never raises.
    """
    # Strip escape/override tokens from the reason FIRST — a gate that echoes the
    # offending command into its reason (the heavy-Bash gate) would otherwise let
    # the ``[fp-confirmed:]`` token split the tokened confirming call and the
    # later tokenless call into two fingerprints, breaking confirm-once-reuse.
    normalised = _SIGNATURE_STRIP_RE.sub(" ", reason)
    for pattern in _DENY_FP_VOLATILE_RES:
        normalised = pattern.sub(" ", normalised)
    # ALL whitespace is removed (not collapsed) so a stripped escape token that
    # was echoed into the reason leaves no boundary gap — the tokened confirming
    # call and the later tokenless call normalise to one fingerprint.
    normalised = re.sub(r"\s+", "", normalised).lower()
    digest = hashlib.sha256(f"{gate_id}\x00{normalised}\x00{signature}".encode()).hexdigest()
    return digest[:16]


def _deny_is_leak_gate(reason: str) -> bool:
    """True iff *reason* is a PUBLIC-egress leak deny (never grantable, #3252).

    The banned-terms / quote-scanner public-egress path is fail-CLOSED always;
    a confirmed-FP grant must never suppress it.
    """
    low = reason.lower()
    return any(marker in low for marker in _LEAK_GATE_MARKERS)


def _fp_confirmed(data: dict) -> bool:
    """True when the current call carries a non-empty ``[fp-confirmed:]`` token."""
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    for field in _TOKEN_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and _FP_CONFIRMED_RE.search(value[:_TOKEN_SCAN_CAP]):
            return True
    return False


def _fp_grant_exists(session_id: str, fingerprint: str) -> bool:
    """True when *fingerprint* has a recorded session-scoped confirmed-FP grant.

    Crash-proof: any IO error reads as "no grant" so a state fault can never
    manufacture a suppression of a genuine deny.
    """
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

    if not session_id:
        return False
    try:
        path = _state_file(session_id, _FP_GRANT_SUFFIX)
        return any(line == fingerprint for line in _read_lines(path))
    except OSError:
        return False


def _record_fp_grant(session_id: str, fingerprint: str) -> None:
    """Persist a session-scoped confirmed-FP grant for *fingerprint* (deduped).

    Best-effort: a record failure must never propagate out of the deny path.
    """
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    if not session_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        path = _state_file(session_id, _FP_GRANT_SUFFIX)
        if any(line == fingerprint for line in _read_lines(path)):
            return
        _append_line(path, fingerprint)


def _bump_deny_streak(session_id: str, fingerprint: str) -> int:
    """Increment the consecutive-denial count for *fingerprint*; return the new count.

    If the stored fingerprint matches, the count increments; otherwise the
    streak resets to 1 for the new fingerprint. Crash-proof: any IO/JSON error
    is swallowed and the call is counted as 1 (a single isolated denial), so a
    state-file fault can never manufacture a trip nor crash the gate.
    """
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

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
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

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
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    if not session_id:
        return
    with contextlib.suppress(OSError):
        _ensure_state_dir()
        path = _state_file(session_id, _CIRCUIT_BROKEN_SUFFIX)
        for line in _read_lines(path):
            if line.split("\t", 1)[0] == fingerprint:
                return
        _append_line(path, f"{fingerprint}\t{gate_id}\t{count}")


def _confirmed_fp_decision(data: dict, reason: str, session_id: str, fingerprint: str) -> _BreakerDecision | None:
    """Suppress a deny when *fingerprint* is a confirmed false positive, else ``None``.

    A previously-granted identical FP is suppressed WITHOUT re-prompting; a fresh
    ``[fp-confirmed:]`` token confirms this call and records the grant. The
    PUBLIC-egress leak gate is never grantable (fail-closed always) — ``None`` so
    the breaker keeps denying it. ``None`` means "no grant applies" and the caller
    proceeds to normal streak accounting.
    """
    if _deny_is_leak_gate(reason):
        return None
    if _fp_grant_exists(session_id, fingerprint):
        return _BreakerDecision(allow=True, reason=reason)
    if _fp_confirmed(data):
        _record_fp_grant(session_id, fingerprint)
        sys.stderr.write(
            f"CONFIRMED FALSE POSITIVE: gate '{_deny_gate_id(reason)}' deny suppressed and granted "
            "for this session — the identical false positive will not re-prompt.\n"
        )
        return _BreakerDecision(allow=True, reason=reason)
    return None


def apply_deny_circuit_breaker(reason: str) -> _BreakerDecision:
    """Route one PreToolUse deny through the repeated-denial circuit breaker.

    Returns a :class:`_BreakerDecision`: ``allow=True`` means SUPPRESS the deny
    (a confirmed false-positive grant, or a looped UX gate auto-relaxed this
    call); otherwise ``reason`` is the deny reason to emit (escalation-augmented
    for a looped safety gate).

    The fingerprint folds in the CURRENT call's signature (#3252) so two DISTINCT
    commands that trip one command-independent gate never share a streak — a
    denied self-bypass cannot poison unrelated later commands. A session-scoped
    confirmed-FP grant (a ``[fp-confirmed:]`` token, or a UX gate the breaker
    concluded is an unsatisfiable false positive) suppresses the IDENTICAL FP
    thereafter without re-prompting; the PUBLIC-egress leak gate is never
    grantable (fail-closed always).

    Crash-proof: on a disabled breaker, a non-PreToolUse invocation, or ANY
    internal error, the original deny is preserved unchanged (fall back to the
    gate's original decision — the breaker never blocks nor wrongly allows on
    its own fault).
    """
    from hooks.scripts.hook_router import _current_hook_context  # noqa: PLC0415 deferred back-import

    try:
        event, data = _current_hook_context()
        if event != "PreToolUse" or not deny_circuit_breaker_enabled():
            return _BreakerDecision(allow=False, reason=reason)
        session_id = data.get("session_id", "") if isinstance(data, dict) else ""
        threshold = deny_circuit_breaker_threshold()
        gate_id = _deny_gate_id(reason)
        signature = _call_signature(data) if isinstance(data, dict) else ""
        fingerprint = _deny_fingerprint(gate_id, reason, signature)

        # Confirmed false-positive grant (session-scoped, per-fingerprint, #3252)
        # — a previously-confirmed or freshly-``[fp-confirmed:]``-tokened identical
        # FP is suppressed without re-prompting; never a leak deny.
        if isinstance(data, dict):
            granted = _confirmed_fp_decision(data, reason, session_id, fingerprint)
            if granted is not None:
                return granted

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
            # The breaker has concluded this identical UX demand is an
            # unsatisfiable false positive; grant it so it stops re-prompting on
            # every subsequent identical action (#3252), not just this one call.
            _record_fp_grant(session_id, fingerprint)
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
