"""Class guard: no PreToolUse deny gate may hard-lock the factory.

The loop-registration gate hard-locked the whole factory several times — it
denied *every* Bash/Edit/Write (including sub-agents, who have no ``CronCreate``
tool and so no way out) with no kill-switch and no self-rescue. Fixing that one
gate is necessary but not sufficient: any *future* PreToolUse deny gate added on
the broad ``Bash|Edit|Write`` matcher path could reintroduce the same lockout
class. This meta-test is the once-and-for-all structural guard.

The invariant (static, no runtime dependency): every PreToolUse handler that can
emit a deny — i.e. that reaches the deny writer
:func:`hook_router._write_pretooluse_deny` transitively through the module's own
call graph — must EITHER route its deny through
:func:`hook_router._fail_open_or_deny` (which gives it the always-allowed
self-rescue commands like ``t3 <overlay> gate disable`` and the master
``[teatree] gate_fail_open`` kill-switch for free) OR be named in the explicit,
documented :data:`_NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS` allowlist, each entry
carrying a one-line rationale.

The deny universe is keyed on the WRITER (``_write_pretooluse_deny``), not on the
``emit_pretooluse_deny`` wrapper that today is its only caller. Keying on the
writer changes nothing about today's classification (every deny path reaches the
writer only through the wrapper) but closes the future-proofing crack: a handler
that calls ``_write_pretooluse_deny(...)`` directly would emit a real hard-lock
yet evade a wrapper-keyed contract. The single-funnel invariant — the writer has
no caller outside ``emit_pretooluse_deny`` — is itself pinned by a dedicated
test so the wrapper's fail-open/circuit-breaker routing can never be sidestepped.

A new broad-deny handler that does neither FAILS this test, forcing the author to
either route it through ``_fail_open_or_deny`` or make a deliberate, reviewable
allowlist entry — never a silent bare ``emit_pretooluse_deny`` lockout.

The allowlist is closed and explicit (the enumerate-once discipline, not a
per-recurrence patch): the public-egress leak path is the canonical hard-safety
exemption that intentionally stays fail-closed (relaxing a public quote/banned
leak is a privacy regression, not a lockout rescue — see the HARD INVARIANT note
in ``hook_router``), and the narrow gates that deny only a single targeted
command (not arbitrary Bash) are not lockout-prone. Handlers that carry their own
never-lockout escapes (kill-switch + per-call opt-out + sub-agent exemption) but
do not yet route through ``_fail_open_or_deny`` are allowlisted with a
``# TODO(never-lockout)`` marker so they are tracked, not forgotten.
"""

import ast
from pathlib import Path
from typing import Final

import hooks.scripts.hook_router as router

_HOOK_ROUTER_SRC: Final[Path] = Path(router.__file__)

# ``_write_pretooluse_deny`` is the actual stdout writer — the true funnel every
# deny path transitively reaches. We key the deny-universe detection on the
# WRITER, not on the ``emit_pretooluse_deny`` wrapper, so a future handler that
# calls ``_write_pretooluse_deny(...)`` DIRECTLY (bypassing the wrapper) still
# emits a real hard-lock AND is caught by this contract instead of evading it.
_DENY_WRITER: Final[str] = "_write_pretooluse_deny"
_DENY_EMITTER: Final[str] = "emit_pretooluse_deny"
_FAIL_OPEN_ROUTER: Final[str] = "_fail_open_or_deny"

# Handlers that legitimately reach ``emit_pretooluse_deny`` without routing
# through ``_fail_open_or_deny``. Each MUST carry a one-line rationale. Two
# documented classes are exempt from the never-lockout routing requirement:
#
#   1. PUBLIC-EGRESS LEAK PATH (hard safety, intentionally fail-closed) — the
#      quote / banned-terms scanners. Relaxing a public leak is
#      a privacy regression, NOT a lockout rescue; they MUST NEVER read
#      ``gate_fail_open`` (the HARD INVARIANT in hook_router). They carry their
#      own per-call ``[quote-ok:]`` / ``[banned-ok:]`` / ``--quote-ok`` escapes.
#   2. NARROW TARGETED-COMMAND gates — deny only a specific dangerous command
#      (a bypass of the t3 CLI, a raw merge, a raw review-post), never arbitrary
#      Bash, so they cannot wedge a session doing unrelated work.
#
# A handler with its own never-lockout escapes (kill-switch + per-call opt-out +
# sub-agent exemption) that has not YET been migrated to ``_fail_open_or_deny``
# is tracked here with a TODO rather than silently bare-denying.
_NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS: Final[dict[str, str]] = {
    # Public-egress leak path — fail-closed by design (privacy, not lockout).
    "handle_quote_scanner_pretool": "public-egress quote leak; fail-closed by design, [quote-ok:] escape",
    "handle_banned_terms_pretool": "public-egress banned-term leak; fail-closed by design, [banned-ok:] escape",
    "handle_block_ai_signature": "public-egress AI-signature leak on commit/publish; fail-closed by design",
    "handle_dispatch_prompt_quote_scanner": (
        "public-egress verbatim-quote leak in an Agent/Task dispatch prompt; fail-closed by design, [quote-ok:] escape"
    ),
    # Narrow targeted-command gates — deny one specific command, never arbitrary Bash.
    "handle_block_direct_commands": "denies only specific t3-CLI-bypass commands (_deny_match denylist)",
    "handle_block_out_of_band_merge": "denies only raw `gh pr merge` / `glab mr merge` on managed repos",
    "handle_block_raw_review_post": "denies only raw review-post commands that bypass the FSM",
    "handle_validate_mr_metadata": "denies only `glab mr create/update` with missing metadata; broken-env escape",
    # Routing conversion, not a content/enforcement deny.
    "handle_route_away_mode_question": "converts AskUserQuestion to DeferredQuestion (away-mode); not a Bash deny",
    # Broad-deny gates carrying their OWN never-lockout escapes, pending migration.
    "handle_enforce_plan_gate": (
        "broad Edit/Write deny; opt-in per overlay (plan_gate=true), cleared by /plan or a source-read. "
        "TODO(never-lockout): route through _fail_open_or_deny"
    ),
    "handle_enforce_orchestrator_boundary": (
        "broad heavy-Bash deny; kill-switch orchestrator_bash_gate_enabled=false + [fg-ok:] + sub-agent exempt. "
        "TODO(never-lockout): route through _fail_open_or_deny"
    ),
}


def _module_tree() -> ast.Module:
    return ast.parse(_HOOK_ROUTER_SRC.read_text(encoding="utf-8"))


def _pretooluse_handler_names(tree: ast.Module) -> list[str]:
    """The PreToolUse handler names, read from the live ``_HANDLERS`` registry literal."""
    for node in ast.walk(tree):
        target = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            target, value = (names[0] if names else None), node.value
        if target != "_HANDLERS" or not isinstance(value, ast.Dict):
            continue
        for key, val in zip(value.keys, value.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == "PreToolUse" and isinstance(val, ast.List):
                return [e.id for e in val.elts if isinstance(e, ast.Name)]
    msg = "_HANDLERS['PreToolUse'] not found in hook_router source"
    raise AssertionError(msg)


def _callee_names(func: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name):
                names.add(callee.id)
            elif isinstance(callee, ast.Attribute):
                names.add(callee.attr)
    return names


def _reachable_callees(start: str, funcs: dict[str, ast.FunctionDef]) -> set[str]:
    """Transitive set of function names called from ``start`` within this module."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen or current not in funcs:
            continue
        seen.add(current)
        stack.extend(_callee_names(funcs[current]))
    return seen


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _callers_of(name: str, funcs: dict[str, ast.FunctionDef]) -> set[str]:
    """The names of module functions that call ``name`` directly."""
    return {fname for fname, func in funcs.items() if name in _callee_names(func)}


def _never_lockout_offenders(tree: ast.Module) -> list[str]:
    """PreToolUse handlers that reach the deny WRITER without a never-lockout escape.

    A handler offends when it transitively reaches ``_write_pretooluse_deny`` (the
    actual stdout writer every deny path funnels through) but neither routes through
    ``_fail_open_or_deny`` nor is named in the documented exemption allowlist. Keyed
    on the writer (not the ``emit_pretooluse_deny`` wrapper) so a direct-write bypass
    is caught, not evaded. Shared by the real-source check and the synthetic-source
    proof so both exercise the identical classifier.
    """
    funcs = _module_functions(tree)
    handlers = _pretooluse_handler_names(tree)
    offenders: list[str] = []
    for handler in handlers:
        reachable = _reachable_callees(handler, funcs)
        if _DENY_WRITER not in reachable:
            continue  # never emits a deny — not a deny gate
        if _FAIL_OPEN_ROUTER in reachable:
            continue  # routes through the fail-open / self-rescue chokepoint
        if handler in _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS:
            continue  # documented hard-safety / narrow exemption
        offenders.append(handler)
    return offenders


def test_every_pretooluse_deny_handler_is_never_lockout() -> None:
    """A PreToolUse deny gate must fail-open-route OR be a documented exemption."""
    tree = _module_tree()
    handlers = _pretooluse_handler_names(tree)
    assert handlers, "no PreToolUse handlers discovered — registry parse regression"

    offenders = _never_lockout_offenders(tree)

    assert not offenders, (
        "PreToolUse deny handler(s) can hard-lock the factory: they reach "
        f"{_DENY_WRITER} without routing through {_FAIL_OPEN_ROUTER} and "
        "are not on the documented never-lockout allowlist.\n"
        f"  offenders: {sorted(offenders)}\n"
        f"Fix: route the deny through {_FAIL_OPEN_ROUTER}(data, reason) (gets the "
        "self-rescue commands + gate_fail_open kill-switch), OR add a deliberate "
        "entry to _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS with a one-line rationale."
    )


def test_loop_registration_gate_routes_through_fail_open() -> None:
    """The incident gate itself must now fail-open-route (regression pin)."""
    tree = _module_tree()
    funcs = _module_functions(tree)
    reachable = _reachable_callees("handle_enforce_loop_registration", funcs)
    assert _DENY_WRITER in reachable, "loop-registration gate must still be able to deny"
    assert _FAIL_OPEN_ROUTER in reachable, (
        "handle_enforce_loop_registration must route its deny through "
        f"{_FAIL_OPEN_ROUTER} so the self-rescue + gate_fail_open escapes apply"
    )
    assert "handle_enforce_loop_registration" not in _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS, (
        "the loop-registration gate is FIXED (fail-open-routed); it must not be on the exemption allowlist"
    )


def test_exemption_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted handler must still be a live PreToolUse deny gate.

    Prevents the allowlist from collecting dead exemptions: an entry that is no
    longer a PreToolUse deny handler (renamed, removed, or migrated to
    fail-open-routing) must be pruned, not left as a silent broadening.
    """
    tree = _module_tree()
    funcs = _module_functions(tree)
    handlers = set(_pretooluse_handler_names(tree))

    stale: list[str] = []
    for handler in _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS:
        if handler not in handlers:
            stale.append(f"{handler} (not a registered PreToolUse handler)")
            continue
        reachable = _reachable_callees(handler, funcs)
        if _DENY_WRITER not in reachable:
            stale.append(f"{handler} (no longer reaches {_DENY_WRITER})")

    assert not stale, (
        f"stale never-lockout exemption(s) — prune them from _NEVER_LOCKOUT_EXEMPT_DENY_HANDLERS: {sorted(stale)}"
    )


def test_write_pretooluse_deny_has_single_funnel() -> None:
    """The deny writer must have exactly one caller: ``emit_pretooluse_deny``.

    ``_write_pretooluse_deny`` is the actual stdout writer; every deny path is
    expected to reach it ONLY through ``emit_pretooluse_deny``, which applies the
    repeated-denial circuit breaker before writing. A second caller would emit a
    real hard-lock while skipping the breaker AND — if the deny-universe were keyed
    on the wrapper — evade the never-lockout contract. Pinning the single-funnel
    invariant keeps the writer-keyed classification honest and the wrapper's
    fail-open / circuit-breaker routing un-sidesteppable.
    """
    tree = _module_tree()
    funcs = _module_functions(tree)
    assert _DENY_WRITER in funcs, f"{_DENY_WRITER} not found in hook_router — writer rename regression"

    callers = _callers_of(_DENY_WRITER, funcs)
    assert callers == {_DENY_EMITTER}, (
        f"{_DENY_WRITER} must have exactly one caller ({_DENY_EMITTER}); found {sorted(callers)}.\n"
        f"A direct caller of {_DENY_WRITER} bypasses the circuit breaker and the never-lockout "
        f"routing. Route the deny through emit_pretooluse_deny (and {_FAIL_OPEN_ROUTER} for "
        "over-deny gates) instead."
    )


_DIRECT_WRITE_BYPASS_SOURCE: Final[str] = '''
def _write_pretooluse_deny(reason):
    return True


def emit_pretooluse_deny(reason):
    return _write_pretooluse_deny(reason)


def handle_sneaky_direct_writer(data):
    """Denies by calling the writer DIRECTLY, bypassing emit_pretooluse_deny."""
    return _write_pretooluse_deny("BLOCKED: arbitrary hard-lock")


_HANDLERS = {
    "PreToolUse": [handle_sneaky_direct_writer],
}
'''


def test_contract_flags_direct_write_pretooluse_deny_bypass() -> None:
    """A handler denying via a DIRECT ``_write_pretooluse_deny`` call must be flagged.

    This is the crack the writer-keyed detection seals: a handler that calls the
    stdout writer directly (not through ``emit_pretooluse_deny``) emits a genuine
    hard-lock. A wrapper-keyed contract would not see it. The classifier, keyed on
    the writer, flags it because it neither routes through ``_fail_open_or_deny``
    nor is on the exemption allowlist.
    """
    tree = ast.parse(_DIRECT_WRITE_BYPASS_SOURCE)
    offenders = _never_lockout_offenders(tree)
    assert "handle_sneaky_direct_writer" in offenders, (
        "a handler that denies by calling _write_pretooluse_deny directly evaded the "
        "never-lockout contract — the deny universe is not keyed on the writer"
    )


_BARE_EMIT_BYPASS_SOURCE: Final[str] = '''
def _write_pretooluse_deny(reason):
    return True


def emit_pretooluse_deny(reason):
    return _write_pretooluse_deny(reason)


def handle_bare_emit_gate(data):
    """Denies via emit_pretooluse_deny without routing through _fail_open_or_deny."""
    return emit_pretooluse_deny("BLOCKED: arbitrary hard-lock")


_HANDLERS = {
    "PreToolUse": [handle_bare_emit_gate],
}
'''


def test_contract_flags_bare_emit_handler() -> None:
    """A handler denying via a bare ``emit_pretooluse_deny`` must be flagged.

    Proves the classifier is not vacuously green: a new broad-deny handler that
    skips ``_fail_open_or_deny`` and is not allowlisted is caught. Without this the
    real-source test could pass merely because today's handlers all comply.
    """
    tree = ast.parse(_BARE_EMIT_BYPASS_SOURCE)
    offenders = _never_lockout_offenders(tree)
    assert "handle_bare_emit_gate" in offenders, (
        "a bare emit_pretooluse_deny handler (no fail-open routing, not allowlisted) "
        "must be flagged by the never-lockout contract"
    )
