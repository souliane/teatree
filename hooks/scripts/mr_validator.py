"""The ``validate-mr-metadata`` PreToolUse gate: resolve, run and judge the overlay validator.

Split out of ``hook_router`` for module health; the router re-exports
:func:`handle_validate_mr_metadata` into ``_HANDLERS``, which is what puts this
module inside the never-lockout contract's call graph. Field extraction stays in the
router (``_extract_mr_fields``), and every deny goes through the router's
``emit_pretooluse_deny`` / ``_fail_open_or_deny``, back-imported lazily.

The typed verdict has three outcomes: ALLOW, DENY, and CANNOT_EVALUATE. A timeout,
failed spawn, or crash warns and allows. An absent validator or a launcher that
cannot run in this venue has no verdict and fails CLOSED, with the
``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in, the master fail-open switch and the
self-rescue commands as escapes.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
stdlib-only siblings.
"""

import json
import os
import sys
from pathlib import Path

from hooks.scripts.gate_result import (
    CompletedRun,
    GateOutcome,
    GateSkipped,
    ValidatorTimedOut,
    announce_cannot_evaluate,
    classify_validator_run,
    run_validator,
    warn_gate_skipped,
)
from hooks.scripts.mr_cli_fields import cli_update_is_title_only, extract_mr_target_repo, mr_deny_reason
from hooks.scripts.t3_invocation import t3_argv, venue_fault

# Alias the bare and ``hooks.scripts.`` identities so the helpers the router
# imports and a test patching one here operate on ONE module object.
sys.modules.setdefault("mr_validator", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.mr_validator", sys.modules[__name__])


def mr_validate_argv() -> list[str] | None:
    """Resolve the command that validates MR metadata.

    Default (no opt-in): ``t3 tool validate-mr`` — runs the active overlay's
    ``validate_pr``, the same verdict ``t3 <overlay> pr create`` uses, so a bad
    title/description is rejected BEFORE the push every time (#119).
    ``T3_MR_VALIDATE_SCRIPT`` remains an explicit override escape hatch.
    ``None`` means no validator is resolvable — the fail-closed broken-env path.
    """
    script = os.environ.get("T3_MR_VALIDATE_SCRIPT", "")
    if script and Path(script).is_file():
        return ["python3", script]
    return t3_argv("tool", "validate-mr")


_EXEC_FAILED_REASON = (
    "the overlay validator could not be EXECUTED at all ({exc}), so the title and description were never checked"
)


def run_mr_validator(
    argv: list[str], title: str, description: str, target_repo: str | None = None, *, sections_optional: bool = False
) -> "CompletedRun | ValidatorTimedOut | GateSkipped | None":
    """Run the validator; a marker when it rendered no verdict, ``None`` if absent.

    The title/description pair goes on STDIN as one JSON object; only the short, bounded
    flags ride argv. GitLab accepts a 1 MiB description and a description-only edit
    back-fills ``title=description``, so on argv the body rode the exec TWICE and
    breached ``ARG_MAX`` (1048576 here) at roughly half the size the forge itself allows.
    ``target_repo`` (when parseable) is forwarded as ``--repo <slug>`` so the validator
    keys overlay resolution to the MR's TARGET, not the agent's cwd. ``sections_optional``
    forwards ``--sections-optional`` for a title-only update whose description is
    untouched (#3254).

    A spawn that fails outright returns :class:`GateSkipped` — CANNOT_EVALUATE, which
    the caller announces and allows. Letting the ``OSError`` propagate instead made the
    gate fail OPEN in SILENCE: the router wraps every handler in ``except Exception:
    continue``, so an over-``ARG_MAX`` body exited 0 with empty stdout, byte-identical
    to a clean pass on text no validator had read.
    """
    repo_args = ["--repo", target_repo] if target_repo else []
    section_args = ["--sections-optional"] if sections_optional else []
    try:
        return run_validator(
            [*argv, *repo_args, *section_args], stdin_text=json.dumps({"title": title, "description": description})
        )
    except OSError as exc:
        return GateSkipped(reason=_EXEC_FAILED_REASON.format(exc=exc))


_UNRESOLVABLE = "is not resolvable"

_MR_VALIDATE_BROKEN_ENV_DENY = (
    "Cannot validate MR title/description — the overlay validator (`t3 tool validate-mr`) {cause}. Refusing to "
    "create the MR with unvalidated metadata (fail closed). Fix the environment, or set "
    "T3_MR_VALIDATE_ALLOW_BROKEN_ENV=1 to deliberately bypass."
)

_MR_VALIDATE_BROKEN_ENV_SKIP = (
    "the overlay validator (`t3 tool validate-mr`) {cause} and T3_MR_VALIDATE_ALLOW_BROKEN_ENV is set, so the "
    "fail-closed deny was deliberately bypassed"
)


def _handle_broken_validate_env(data: dict, cause: str = _UNRESOLVABLE) -> bool:
    """Decide the gate's action when no validator could render a verdict here.

    The MR-metadata gate FAILS CLOSED by default (deny): a non-compliant title
    must never reach GitLab just because the env could not validate it. The
    explicit ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV`` opt-in is the per-gate
    self-rescue — and taking it is announced, never mute, so an MR that goes out
    unvalidated says so. The broken-env deny additionally routes through
    ``_fail_open_or_deny`` so the master ``danger_gate_fail_open`` switch and
    the always-allowed self-rescue commands relax it too (NEVER-LOCKOUT).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — deferred back-import

    if os.environ.get("T3_MR_VALIDATE_ALLOW_BROKEN_ENV", "").strip().lower() in {"1", "true", "yes"}:
        warn_gate_skipped("MR-metadata", _MR_VALIDATE_BROKEN_ENV_SKIP.format(cause=cause))
        return False
    return _fail_open_or_deny(data, _MR_VALIDATE_BROKEN_ENV_DENY.format(cause=cause), gate_id="mr_metadata")


def _mr_validator_verdict(data: dict, result: "CompletedRun | ValidatorTimedOut | GateSkipped | None") -> bool:
    """Map a validator run (or its failure to run) to the gate's block decision."""
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 — deferred back-import

    if isinstance(result, ValidatorTimedOut | GateSkipped):
        return announce_cannot_evaluate("MR-metadata", result)
    if result is None:
        return _handle_broken_validate_env(data)
    fault = venue_fault(result.returncode, result.stderr)
    if fault is not None:
        return _handle_broken_validate_env(data, f"could not run: {fault}")

    outcome = classify_validator_run(result)
    if outcome is GateOutcome.CANNOT_EVALUATE:
        # The validator RAN but crashed (a traceback, not a clean verdict).
        # Crash ≠ deny (#1528): warn loudly and allow — the remote CI
        # MR-title/description job is the backstop for real non-compliance.
        sys.stderr.write(
            "NOTE: the MR-metadata validator crashed (could not evaluate) — "
            "allowing the MR to proceed (fail-open-with-warn). The remote "
            "MR-title/description CI job remains the backstop.\n"
        )
        return False
    if outcome is GateOutcome.DENY:
        return emit_pretooluse_deny(mr_deny_reason(data, result.stderr or result.stdout or ""), gate_id="mr_metadata")
    return False


def handle_validate_mr_metadata(data: dict) -> bool:
    """Block a non-compliant ``glab mr``/``gh pr`` create/update before it runs.

    Validates by default via the TARGET overlay's ``validate_pr`` (no env-var
    opt-in) so the pre-push gate is always live (#119 Part 3). The MR's TARGET
    repo is parsed from the command and threaded as ``--repo`` so an MR is graded
    against the TARGET overlay's rules, not the cwd overlay's weaker ones. A
    validator that RAN but crashed, and one too SLOW to finish inside its
    allowance, are both CANNOT_EVALUATE — crash ≠ deny (#1528), and neither is a
    timeout: each warns and allows, with the remote CI job as backstop. The
    UNRESOLVABLE validator (no ``t3``, no script) and a launcher that cannot run in
    this venue (exit 69) FAIL CLOSED; the ``T3_MR_VALIDATE_ALLOW_BROKEN_ENV``
    opt-in restores fail-open there.

    Every outcome in which the gate does NOT actually validate the MR — an
    unresolvable ``$(…)``/``$VAR`` field, an update setting no governed field,
    the broken-env opt-in, a crash, a timeout — emits one loud named line. Only
    "this is not an MR mutation" and a clean PASS are silent, so a mute gate can
    never be mistaken for one that swallowed the call.
    """
    from hooks.scripts.hook_router import _extract_mr_fields  # noqa: PLC0415 — deferred back-import

    fields = _extract_mr_fields(data)
    if fields is None:
        return False
    if isinstance(fields, GateSkipped):
        return announce_cannot_evaluate("MR-metadata", fields)
    title, description = fields
    command = data.get("tool_input", {}).get("command", "") if data.get("tool_name") == "Bash" else ""
    target_repo = extract_mr_target_repo(command) if command else None
    # Title-only update: no description touched → skip required-section check (#3254).
    sections_optional = bool(command) and cli_update_is_title_only(command)

    argv = mr_validate_argv()
    if argv is None:
        return _handle_broken_validate_env(data)

    return _mr_validator_verdict(
        data, run_mr_validator(argv, title, description, target_repo, sections_optional=sections_optional)
    )
