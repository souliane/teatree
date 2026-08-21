"""PreToolUse: plan-before-code gate, with the Bash write arm (#4091).

The gate denies a file write while the cwd's worktree ticket is still in
``STARTED`` — the deterministic half of "a plan is recorded before coding
begins". It keyed on the ``Edit``/``Write`` TOOL NAMES, so a file written
through the shell reached it as nothing at all: a measured full day of
implementation, every source file written by a ``python3 - <<PY`` heredoc or
``sed``, and the gate never fired once (#4091).

The ``Bash`` arm closes that. A command whose WRITE TARGETS (resolved by the
shared :mod:`teatree.hooks.write_targets`) land inside the cwd's repo is gated
exactly as an ``Edit`` to the same path. It is PRECISION-biased: a target the
resolver cannot pin statically WARNS rather than denying, because a false
negative is exactly the pre-fix behaviour while a false positive blocks
legitimate shell work. The classification runs BEFORE the ticket-state lookup,
so an ordinary read-only ``Bash`` call never pays for a Django bootstrap.

The handler lives here rather than in ``hook_router`` because that dispatcher is
a shrink-only god-module; the router re-exports it into ``_HANDLERS``. Its cwd →
git toplevel → ``Worktree`` row → ``Ticket.state`` resolver
(:func:`_ticket_state_for_cwd` over :func:`_resolve_worktree_state`) sits here for
the same reason, and the router re-exports it too — the gate is its only caller.
The deny still routes through the router's ``_fail_open_or_deny`` chokepoint (lazy
back-import), so the self-rescue allowlist and the master
``danger_gate_fail_open`` switch apply unchanged.
"""

import contextlib
import os
import re
import subprocess  # noqa: S404 — stdlib subprocess for the trusted internal `git rev-parse` call
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hooks.scripts.managed_repo import teatree_src_on_path

# Alias both identities so a bare ``from plan_edit_gate import ...`` (the live
# hook, whose dir is on sys.path) and ``hooks.scripts.plan_edit_gate`` (a
# subprocess/test import) resolve the SAME module object.
sys.modules.setdefault("plan_edit_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.plan_edit_gate", sys.modules[__name__])

# Per-call escape for the plan-edit gate: ``[skip-plan-gate: <non-empty-reason>]``
# in the current tool call's new_string/content/file_path/command unblocks that
# single call. Mirrors ``_SKILL_LOAD_OK_RE`` / ``_SKIP_SKILL_GATE_RE`` in shape
# and 512-char truncation scope — buried tokens do not silently escape.
SKIP_PLAN_GATE_RE: re.Pattern[str] = re.compile(r"\[skip-plan-gate:\s*(\S[^\]]*?)\s*\]")

_TOKEN_SCAN_LIMIT = 512
_SKIP_TOKEN_FIELDS = ("new_string", "content", "file_path", "command")
_GATED_TOOLS: Final[frozenset[str]] = frozenset({"Edit", "Write", "Bash"})


@dataclass(frozen=True, slots=True)
class BashWriteVerdict:
    """What a ``Bash`` command would write inside the cwd's repo.

    ``gated_paths`` are in-repo writes the gate denies. ``ambiguous`` is True
    when the command clearly writes but no target could be pinned — a suspicion,
    not a proof, so it warns instead of denying.
    """

    gated_paths: tuple[Path, ...]
    ambiguous: bool

    @property
    def is_silent(self) -> bool:
        return not (self.gated_paths or self.ambiguous)


_NO_WRITE: Final[BashWriteVerdict] = BashWriteVerdict(gated_paths=(), ambiguous=False)


def skip_plan_gate_token(data: dict) -> str | None:
    """Return the reason from a ``[skip-plan-gate: <reason>]`` token, else None.

    Scans the current tool call's ``new_string``, ``content``, ``file_path`` and
    ``command`` within the first 512 characters of each field — mirroring the
    skill-loading gate's token scanner — so a buried token in a long body does
    not silently authorise the call. An empty reason returns None.
    """
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    for field in _SKIP_TOKEN_FIELDS:
        value = tool_input.get(field, "")
        if not isinstance(value, str) or not value:
            continue
        match = SKIP_PLAN_GATE_RE.search(value[:_TOKEN_SCAN_LIMIT])
        if not match:
            continue
        reason = match.group(1).strip()
        if reason:
            return reason
    return None


def classify_bash_write(command: str, cwd: str) -> BashWriteVerdict:
    """Which of *command*'s write targets land inside the repo enclosing *cwd*.

    Relative targets are anchored to the command's EFFECTIVE dir (honouring a
    leading ``cd`` / ``-C`` / ``--git-dir``), so the gate keys off the repo the
    command actually writes rather than the ambient cwd. Every resolution
    failure yields no gated path — this gate's standing fail-OPEN posture.
    """
    targets = _write_targets(command)
    if targets is None or not targets.writes_something:
        return _NO_WRITE
    root = _repo_root(cwd)
    if root is None:
        return _NO_WRITE
    gated = tuple(path for path in targets.resolved_paths(_command_base(command, cwd)) if _is_inside(root, path))
    return BashWriteVerdict(gated_paths=gated, ambiguous=targets.unresolved and not gated)


def handle_block_edit_before_planned(data: dict) -> bool:
    """Deny a file write when the worktree's ticket is still in STARTED state.

    The FSM already prevents ``code()`` from STARTED (TransitionNotAllowed), so
    this gate provides an earlier, clearer DX signal: write attempts while the
    ticket has not yet been planned are denied with an actionable message. It
    covers ``Edit``/``Write`` and — since #4091 — a ``Bash`` command that writes
    a path inside the repo. Fail-open on every resolution failure so the gate
    never wedges an agent when the DB is unavailable or the cwd is not a managed
    worktree.

    **Never-lockout escapes (mirror the skill-loading gate):**

    1. Per-call token ``[skip-plan-gate: <non-empty-reason>]`` in ``new_string``
        / ``content`` / ``file_path`` / ``command`` (first 512 chars).
    2. Config kill-switch — the DB-home ``plan_edit_gate_enabled = false`` setting
        (flipped by ``t3 <overlay> gate plan disable``).

    The existing ``_fail_open_or_deny`` safety chain (self-rescue allowlist +
    master ``danger_gate_fail_open``) is unchanged — the escapes above are
    ADDITIONS to it, not replacements.
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _fail_open_or_deny,
        _plan_edit_gate_enabled,
    )

    tool_name = data.get("tool_name", "")
    if tool_name not in _GATED_TOOLS or not _plan_edit_gate_enabled():
        return False
    cwd = data.get("cwd", "") or str(Path.cwd())
    verdict = classify_bash_write(_bash_command(data), cwd) if tool_name == "Bash" else None
    if (verdict is not None and verdict.is_silent) or not _ticket_is_unplanned(cwd):
        return False
    if _call_is_excused(data, verdict):
        return False
    # Stamp the non-privacy ``plan_gate`` marker so the transcript-conformance
    # eval (``no_code_edit_before_planned``) keys on THIS gate's deny without
    # reading the raw reason.
    return _fail_open_or_deny(data, _deny_reason(tool_name, verdict), gate_id="plan_gate")


def _ticket_is_unplanned(cwd: str) -> bool:
    """True iff *cwd*'s worktree ticket is STARTED; any resolver failure is False (allow)."""
    from hooks.scripts.hook_router import _ticket_state_for_cwd  # noqa: PLC0415 deferred back-import

    try:
        return _ticket_state_for_cwd(cwd) == "started"
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False


def _call_is_excused(data: dict, verdict: BashWriteVerdict | None) -> bool:
    """True iff the call escapes the deny — an explicit token, or an unpinnable write.

    An unpinnable write target is a suspicion, not a proof, so it emits the warn
    the house doctrine prescribes for an ambiguous case and lets the call through.
    """
    if reason_token := skip_plan_gate_token(data):
        sys.stderr.write(f"NOTE: plan-gate edit-block skipped via [skip-plan-gate: {reason_token}].\n")
        return True
    if verdict is not None and not verdict.gated_paths:
        sys.stderr.write(
            "NOTE: plan-gate could not pin this command's write target — the ticket is still "
            "STARTED, so record a plan before it writes source.\n"
        )
        return True
    return False


def _deny_reason(tool_name: str, verdict: BashWriteVerdict | None) -> str:
    subject = (
        f"Bash denied: this command writes `{verdict.gated_paths[0]}`, and " if verdict else f"{tool_name} denied: "
    )
    return (
        f"{subject}the worktree's ticket is still in STARTED state — "
        "a plan must be recorded before coding can begin. "
        "Run the planning phase first so the ticket advances to PLANNED. "
        "If this is a trivial mechanical edit, add `[skip-plan-gate: <reason>]` to proceed."
    )


def _bash_command(data: dict) -> str:
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    return command if isinstance(command, str) else ""


def _write_targets(command: str):  # noqa: ANN202 — returns a lazily-imported type; annotating would pull it to module scope
    """The shared write-target resolver's verdict, or None in a cold hook env."""
    if not command:
        return None
    try:
        with teatree_src_on_path():
            from teatree.hooks.write_targets import bash_write_targets  # noqa: PLC0415 — deferred: cold-hook import

            return bash_write_targets(command)
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN (no write targets).
        return None


def _command_base(command: str, cwd: str) -> Path | None:
    """The dir a relative write target is anchored to (``cd`` / ``-C`` aware)."""
    try:
        from hooks.scripts.main_clone_guard import _effective_command_dir  # noqa: PLC0415 deferred sibling import

        return _effective_command_dir(command, Path(cwd))
    except Exception:  # noqa: BLE001 — an unresolvable base drops relative targets, never guesses one.
        return None


def _repo_root(cwd: str) -> Path | None:
    """The nearest enclosing repo root of *cwd*, found by walking up to a ``.git``.

    Filesystem-only on purpose: it must answer for a freshly-branched worktree
    with no commit yet (where ``git rev-parse --abbrev-ref HEAD`` errors), and it
    keeps this gate's fast pre-check free of a subprocess. A ``.git`` FILE (a
    linked worktree) counts exactly as a ``.git`` dir does.
    """
    start = _canonical(Path(cwd))
    if start is None:
        return None
    return next((candidate for candidate in (start, *start.parents) if (candidate / ".git").exists()), None)


def _canonical(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _is_inside(root: Path, path: Path) -> bool:
    candidate = _canonical(path)
    return candidate is not None and root in candidate.parents


def _resolve_worktree_state(toplevel: str) -> str | None:
    """Return the ticket FSM state for the worktree at on-disk *toplevel*.

    Delegates the path → ``Worktree`` row resolution to the canonical
    :func:`teatree.core.intake.resolve.match_worktree_by_path` (the single source of
    truth for matching an on-disk path against ``extra['worktree_path']``,
    incl. the macOS ``/var`` ↔ ``/private/var`` symlink variants and the
    subdirectory walk) rather than a hand-rolled query — a hand-rolled
    ``Worktree.objects.filter(path=…)`` is exactly the #1957 dead-gate bug:
    ``Worktree`` has no ``path`` field (the on-disk path lives in
    ``extra['worktree_path']``), so every call raised ``FieldError``. Raises on
    a programming error so the caller can log it loudly rather than swallow it
    into a silent fail-open.
    """
    from teatree.core.intake.resolve import match_worktree_by_path  # noqa: PLC0415 — deferred: cold-hook import

    worktree = match_worktree_by_path(toplevel)
    if worktree is None or worktree.ticket is None:
        return None
    return str(worktree.ticket.state)


def _ticket_state_for_cwd(cwd: str) -> str | None:
    """Return the ticket's FSM state for the worktree at *cwd*, or ``None``.

    Resolves the cwd → git toplevel → Worktree DB row → Ticket.state. Fails
    open (returns ``None``) on an OPERATIONAL failure — teatree unavailable,
    cwd not a managed worktree, git/subprocess error — so the hook never wedges
    an agent. A PROGRAMMING error (wrong field name, bad import — the #1957
    class) is NOT swallowed silently: it emits a loud stderr NOTE before the
    fail-open so a dead gate is diagnosable instead of invisible.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        import django  # noqa: PLC0415 — deferred: Django import at call time
        from django.core.exceptions import FieldError  # noqa: PLC0415 — deferred: Django import at call time

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
        django.setup()

        try:
            toplevel = subprocess.check_output(  # noqa: S603 — trusted internal subprocess; fixed argv, no shell
                ["git", "-C", cwd, "--no-optional-locks", "rev-parse", "--show-toplevel"],  # noqa: S607 — trusted internal git invocation with a fixed argv
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            return None
        try:
            return _resolve_worktree_state(toplevel)
        except (FieldError, TypeError, AttributeError, ImportError) as exc:
            # Programming-error class (the #1957 dead-gate root cause): stay
            # crash-proof (return None) but make it LOUD, never a silent ALLOW.
            sys.stderr.write(f"NOTE: plan-gate edit-block resolver hit a programming error ({exc!r}); failing open.\n")
            return None
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))
