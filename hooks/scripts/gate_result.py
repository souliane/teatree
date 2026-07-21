"""Typed gate-evaluation outcome — a validator CRASH is NOT a content DENY (#1528).

A PreToolUse validator gate shells out to an external validator (e.g.
``t3 tool validate-mr``) and today collapses two very different results into one
``deny``: the validator RAN and reported the CONTENT is invalid (a genuine
deny), and the validator ITSELF crashed — an uncaught traceback, an unreadable
source, a broken interpreter — exiting non-zero for a reason that says nothing
about the content. Reading the second case as a deny is the lockout class #1528
names: a broken validator hard-blocks the tool with a Python traceback pasted in
as the "reason", and the agent has no way through.

This module is the framework seam that keeps the two apart. :class:`GateOutcome`
is the typed verdict; :func:`classify_validator_run` maps a finished validator
subprocess to ALLOW / DENY / CANNOT_EVALUATE, reading a crash SIGNATURE (a Python
traceback) rather than the ambiguous exit code alone (a clean "invalid" and an
uncaught exception both exit ``1``). :class:`ValidatorTimedOut` covers the third
can't-evaluate shape — the validator was too SLOW to finish inside its allowance.
A caller maps every CANNOT_EVALUATE to fail-open-WITH-A-LOUD-WARN — never a deny —
so a crashing or over-slow validator produces one loud stderr line and lets the
tool through; the remote backstop still catches genuinely non-compliant content
later.

The seam also owns the time ALLOWANCE every such gate gives its validator
(:func:`validator_timeout_seconds`) and the warn a breach emits
(:func:`warn_validator_timed_out`), so the allowance is one knob shared by every
``t3 tool …`` shell-out rather than a magic number per call site.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the stdlib-only ``teatree_settings`` sibling (whose DB read is itself lazy).
"""

import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from hooks.scripts.teatree_settings import teatree_int_setting

_TRACEBACK_SIGNATURE = "Traceback (most recent call last):"

#: Time allowance for a ``t3 tool …`` validator subprocess, in seconds. The floor
#: is the validator's own cost, dominated by the ``t3`` CLI's cold start: ~13s
#: unloaded and 25-50s under concurrent load on the reference box. 60s clears the
#: loaded range with headroom, and an allowance still too small on some future
#: slower box now degrades to warn-and-allow rather than to a silent deny — the
#: knob below is the fix, not a hardcoded bump.
_HOOK_VALIDATOR_TIMEOUT_DEFAULT_SECONDS = 60


def validator_timeout_seconds() -> int:
    """The DB-home ``[teatree] hook_validator_timeout_seconds`` allowance."""
    return teatree_int_setting(
        "hook_validator_timeout_seconds", default=_HOOK_VALIDATOR_TIMEOUT_DEFAULT_SECONDS, minimum=1
    )


def warn_validator_timed_out(gate: str, allowance_seconds: int) -> None:
    """Emit the one loud line that keeps a timeout distinguishable from a rejection."""
    sys.stderr.write(
        f"NOTE: the {gate} validator did not finish within its {allowance_seconds}s "
        "allowance (CANNOT_EVALUATE — a timeout is not a verdict on the content) — "
        "allowing the call to proceed (fail-open-with-warn). Raise the allowance with "
        "`t3 <overlay> config_setting set hook_validator_timeout_seconds <seconds>`. "
        "The remote CI job remains the backstop.\n"
    )


@dataclass(frozen=True)
class ValidatorTimedOut:
    """The validator was still running when its time allowance expired.

    A distinct marker rather than a bare ``None``, because "too slow to render a
    verdict" and "no validator exists at all" are different environments that
    route to different postures: a timeout is CANNOT_EVALUATE (warn and allow),
    an absent validator stays fail-closed. ``allowance_seconds`` is carried so
    the warn names the budget the caller actually gave it.
    """

    allowance_seconds: int


class CompletedRun(Protocol):
    """The structural slice of ``subprocess.CompletedProcess`` a verdict needs.

    Typed as a Protocol so this cold-hook leaf never imports ``subprocess`` (it
    spawns no process): a real ``CompletedProcess[str]`` satisfies it structurally.
    The members are read-only (``@property``) so a concrete ``stdout: str`` matches
    the ``str | None`` slot covariantly.
    """

    @property
    def returncode(self) -> int: ...
    @property
    def stdout(self) -> str | None: ...
    @property
    def stderr(self) -> str | None: ...


class GateOutcome(StrEnum):
    """A validator gate's typed verdict at the framework seam (#1528).

    ``ALLOW`` — the validator ran and the content passed.
    ``DENY`` — the validator ran and the content is genuinely non-compliant.
    ``CANNOT_EVALUATE`` — the validator could not render a verdict: it crashed or
    its source was unreadable. Routes to fail-open-with-warn, NEVER a deny — the
    crash-not-deny lockout fix.
    """

    ALLOW = "allow"
    DENY = "deny"
    CANNOT_EVALUATE = "cannot_evaluate"


def output_is_crash(text: str) -> bool:
    """Whether validator output carries a crash SIGNATURE (a Python traceback).

    A clean validation failure prints a concise message (``Title is empty.``); an
    uncaught exception prints a ``Traceback (most recent call last):`` header. The
    signature is the deterministic tell that a non-zero exit is a CRASH, not a
    content verdict — the exit code alone is ambiguous (both exit ``1``).
    """
    return _TRACEBACK_SIGNATURE in (text or "")


def classify_validator_run(completed: CompletedRun | None, *, ok_returncode: int = 0) -> GateOutcome:
    """Map a finished validator subprocess to a typed :class:`GateOutcome`.

    ``None`` (the subprocess raised before completing) is CANNOT_EVALUATE — the
    validator never rendered a verdict. A completed run is ALLOW at
    ``ok_returncode``; a non-zero run is CANNOT_EVALUATE when its combined output
    carries a crash signature (:func:`output_is_crash`), else DENY (a genuine
    content rejection).
    """
    if completed is None:
        return GateOutcome.CANNOT_EVALUATE
    if completed.returncode == ok_returncode:
        return GateOutcome.ALLOW
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if output_is_crash(combined):
        return GateOutcome.CANNOT_EVALUATE
    return GateOutcome.DENY
