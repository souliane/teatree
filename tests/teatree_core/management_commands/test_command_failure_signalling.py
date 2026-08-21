"""Enforcement guard for #932 (keyword set broadened in #939).

A django-typer ``@command`` subcommand that signals failure by *returning*
an error string leaves the process exiting 0 — django-typer serialises the
return value to stdout but never sets a non-zero code. CI / the loop /
headless callers then see success on a real failure (the false-completion
class). The fix is to ``self.stderr.write(...)`` then ``raise
SystemExit(1)`` (canonical: ``tasks.py``, ``db.py`` ``query``/``shell``).

This guard walks every ``core/management/commands`` module with AST and
fails if any ``@command``-decorated method has a ``return`` whose value is
a string literal / f-string matching any failure keyword in
``_FAILURE_KEYWORDS``.

The same defect wears two other shapes the string walk cannot see — ``return 1``
and ``return {"error": …}`` — and both exit 0 exactly like the string form. Those
are detected by :func:`_offending_exit_contract_returns` and ratcheted against
:data:`_KNOWN_EXIT_CONTRACT_OFFENDERS`: the sites that predate the broadened
detector are listed there so they are VISIBLE rather than invisible, and a NEW
one reds immediately. That set may only ever shrink.

Scope (kept narrow so false positives stay low):

- Only ``@command`` methods are scanned, so module-level helpers (e.g. a
    ``_workspace_*`` reaping helper returning a "… failed:" string) keep
    returning failure text for their command (``clean-all``) to inspect and raise on.
- Only statically-decidable returns are inspected: a string / f-string, a
    non-zero int literal, or a dict literal with a failure-keyword key or value.
    A dynamically-built return (``return build_message()``) is out of AST reach.

The #939 broadening covers the sites the original "fail"-only set missed
(messages worded "aborted", "error", "not found", "not configured", "not
running", "unknown", "requires …"). The guard therefore catches the
high-blast subset of the anti-pattern that is statically detectable on
the current command tree — it is not a proof that the anti-pattern can
never regress (a return whose error wording avoids every keyword, or a
dynamically-built message, is still out of AST reach). It is a strong,
low-false-positive backstop, not an absolute one.

If a genuinely-benign command must return a string containing one of
these keywords, this guard should be made aware of it explicitly rather
than relaxed wholesale.
"""

import ast
import textwrap
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "teatree"
_COMMANDS_DIR = _SRC_ROOT / "core" / "management" / "commands"


def _management_command_modules() -> list[Path]:
    """Every module under a ``src/teatree/**/management/commands/`` tree, at any depth.

    The commands tree carries subpackages (``_ship/``, ``_workspace/``,
    ``_test_plan/``) whose modules define ``@command`` methods exactly like the
    top-level ones; a single-level glob left ~a quarter of the command tree
    unscanned, which is where a guard like this is least likely to be noticed.
    """
    return sorted(p for p in _SRC_ROOT.glob("**/management/commands/**/*.py"))


# Case-insensitive substrings that mark a returned string as failure
# signalling. Kept low-false-positive: only words/phrases that, in a
# command's *return value*, denote an error condition the process should
# exit non-zero on. Verified against the full command tree (see
# ``test_broadened_set_flags_only_the_known_sites``).
_FAILURE_KEYWORDS: frozenset[str] = frozenset(
    {
        "fail",
        "error",
        "abort",
        "unknown",
        "not configured",
        "not found",
        "not running",
        "requires ",
    },
)


def _is_command_decorator(decorator: ast.expr) -> bool:
    """True for ``@command`` / ``@command(...)`` / ``@command(name=...)``."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id == "command"
    if isinstance(target, ast.Attribute):
        return target.attr == "command"
    return False


def _static_text(value: ast.expr | None) -> str | None:
    """The static text of a string/f-string expression, else None."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.JoinedStr):
        return "".join(
            piece.value for piece in value.values if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
        )
    return None


def _returned_string_value(node: ast.Return) -> str | None:
    """Return the static text of a returned string/f-string, else None."""
    return _static_text(node.value)


def _matched_keyword(text: str) -> str | None:
    """First failure keyword found in ``text`` (case-insensitive), else None."""
    lowered = text.lower()
    for keyword in _FAILURE_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def _offending_returns(source: str) -> list[tuple[str, int, str]]:
    tree = ast.parse(source)
    offences: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        if not any(_is_command_decorator(d) for d in func.decorator_list):
            continue
        for stmt in ast.walk(func):
            if not isinstance(stmt, ast.Return):
                continue
            text = _returned_string_value(stmt)
            if text and _matched_keyword(text) is not None:
                offences.append((func.name, stmt.lineno, text))
    return offences


def _returned_int(node: ast.Return) -> int | None:
    """The value of a returned non-zero int literal, else None.

    ``return 1`` from a ``@command`` is the exit code the author meant and the
    process does not take: django-typer serialises it and exits 0.
    """
    value = node.value
    if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
        return value.value or None
    return None


def _failure_dict_key(node: ast.Return) -> str | None:
    """The failure-signalling key or value of a returned dict literal, else None.

    ``return {"error": …}`` / ``{"ok": False, "error": …}`` is the structured form
    of the same anti-pattern — the caller reads a payload, the shell reads exit 0.
    """
    value = node.value
    if not isinstance(value, ast.Dict):
        return None
    entries = [*(key for key in value.keys if key is not None), *value.values]
    for text in (_static_text(entry) for entry in entries):
        if text and _matched_keyword(text) is not None:
            return text
    return None


def _offending_exit_contract_returns(source: str) -> list[tuple[str, int, str]]:
    """``(method, lineno, detail)`` for every non-string failure return in a @command."""
    tree = ast.parse(source)
    offences: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        if not any(_is_command_decorator(d) for d in func.decorator_list):
            continue
        for stmt in ast.walk(func):
            if not isinstance(stmt, ast.Return):
                continue
            code = _returned_int(stmt)
            if code is not None:
                offences.append((func.name, stmt.lineno, f"return {code}"))
                continue
            failure = _failure_dict_key(stmt)
            if failure is not None:
                offences.append((func.name, stmt.lineno, f"return dict carrying {failure!r}"))
    return offences


#: ``(src/teatree-relative module path, @command method)`` pairs that signal failure
#: by a non-string return today. They predate this detector. The path is the
#: fully-qualified key rather than the basename, which collides across the command
#: subpackages. The set may only ever SHRINK — a new offender fails
#: ``test_no_new_command_signals_failure_by_a_non_string_return`` immediately, and a
#: fixed one fails ``test_no_known_exit_contract_offender_is_stale`` until deleted.
#:
#: #4234 drained the 4 ``env.py`` bare-int sites (now ``raise SystemExit(N)``) plus
#: ``env.py migrate_secrets`` and ``retro.py review_findings`` (neither of which this
#: AST-only detector could see — see ``tests/teatree_core/management_commands/
#: test_exit_contract_seam.py``). The remaining sites below still literally
#: ``return {"error": …}`` — converting them to a bare raise would destroy the value
#: in-process callers read (e.g. ``CallCommandMergeKeystone.merge_clear`` off ``ticket
#: merge``). They exit non-zero from the shell anyway: their ``Command`` classes all
#: inherit ``RefusalExitTyperCommand`` (#4210), which restores the exit code at the
#: argv boundary alone. ``test_exit_contract_seam.py::TestEveryStructuredRefusalCarriesTheSeam``
#: is the seam-aware guard for this set — it fails if any of these classes ever loses
#: that base, so this static ratchet staying non-empty here is the accepted, verified
#: end state, not an open TODO.
_KNOWN_EXIT_CONTRACT_OFFENDERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("core/management/commands/_merge_keystone_commands.py", "merge"),
        ("core/management/commands/_rubric_commands.py", "rubric_set"),
        ("core/management/commands/e2e.py", "trigger_ci"),
        ("core/management/commands/followup.py", "discover_mrs"),
        ("core/management/commands/lifecycle.py", "record_e2e_run"),
        ("core/management/commands/pr.py", "post_test_plan"),
        ("core/management/commands/pr.py", "sweep"),
        ("core/management/commands/repro.py", "record_green"),
        ("core/management/commands/repro.py", "record_red"),
        ("core/management/commands/repro.py", "waive"),
        ("core/management/commands/review.py", "lock_acquire"),
        ("core/management/commands/review.py", "record_evidence"),
        ("core/management/commands/ticket.py", "clear"),
        ("core/management/commands/ticket.py", "comment"),
        ("core/management/commands/ticket.py", "create_sub"),
        ("core/management/commands/ticket.py", "e2e_bypass"),
        ("core/management/commands/ticket.py", "transition"),
    },
)


def _flagged_exit_contract_offenders() -> set[tuple[str, str]]:
    """Every ``(src-relative path, @command method)`` the detector flags right now."""
    return {
        (module.relative_to(_SRC_ROOT).as_posix(), func_name)
        for module in _management_command_modules()
        for func_name, _lineno, _detail in _offending_exit_contract_returns(module.read_text())
    }


class TestCommandFailureSignalling:
    def test_no_command_returns_a_failure_string(self) -> None:
        violations: list[str] = []
        for module in _management_command_modules():
            for func_name, lineno, text in _offending_returns(module.read_text()):
                violations.append(
                    f"{module.name}:{lineno} — @command `{func_name}` returns "
                    f"a failure string {text!r}; use `self.stderr.write(...)` "
                    f"then `raise SystemExit(1)` (see #932/#939).",
                )
        assert not violations, "Commands must raise SystemExit(1) on failure, not return a string:\n" + "\n".join(
            violations,
        )


def _is_command_or_initialize_decorator(decorator: ast.expr) -> bool:
    """True for ``@command``/``@initialize`` (with or without a call/attribute form)."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id in {"command", "initialize"}
    if isinstance(target, ast.Attribute):
        return target.attr in {"command", "initialize"}
    return False


def _is_typer_exit_raise(node: ast.Raise) -> bool:
    """True for ``raise typer.Exit(...)`` / ``raise Exit(...)`` (the django-typer-swallowed primitive)."""
    exc = node.exc
    target = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(target, ast.Attribute):
        return target.attr == "Exit"
    if isinstance(target, ast.Name):
        return target.id == "Exit"
    return False


def _offending_typer_exits(source: str) -> list[tuple[str, int]]:
    """``(method_name, lineno)`` for every ``raise typer.Exit`` inside a @command/@initialize method."""
    tree = ast.parse(source)
    return [
        (func.name, stmt.lineno)
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef)
        and any(_is_command_or_initialize_decorator(d) for d in func.decorator_list)
        for stmt in ast.walk(func)
        if isinstance(stmt, ast.Raise) and _is_typer_exit_raise(stmt)
    ]


class TestNoTyperExitInManagementCommands:
    """``raise typer.Exit`` is swallowed under ``call_command`` — management commands must raise SystemExit.

    django-typer runs a ``TyperCommand`` subcommand under Django's
    ``call_command``, which catches ``typer.Exit`` and *returns* its code (the
    process exits 0). A categorised non-zero exit therefore reports success to
    cron/CI/the loop unless the command raises ``SystemExit(N)``. typer.Exit
    remains correct in the ``src/teatree/cli/*.py`` typer-runner files, so this
    guard is scoped to ``management/commands`` only.
    """

    def test_no_command_raises_typer_exit(self) -> None:
        violations: list[str] = []
        for module in _management_command_modules():
            for func_name, lineno in _offending_typer_exits(module.read_text()):
                violations.append(
                    f"{module.name}:{lineno} — @command/@initialize `{func_name}` does "
                    f"`raise typer.Exit(...)`; under `call_command` that is swallowed to exit 0. "
                    f"Use `raise SystemExit(N)` instead.",
                )
        assert not violations, (
            "Management commands must raise SystemExit(N), not typer.Exit (swallowed under call_command):\n"
            + "\n".join(violations)
        )


def _typer_exit_source(raise_stmt: str, *, decorator: str = "@command()") -> str:
    """Minimal decorated method whose body is ``raise_stmt``."""
    return textwrap.dedent(
        f"""
        class Command:
            {decorator}
            def sub(self):
                {raise_stmt}
        """,
    )


class TestTyperExitDetection:
    """The AST detector is precise: it flags the swallowed primitive, not SystemExit."""

    @pytest.mark.parametrize(
        "raise_stmt",
        [
            "raise typer.Exit(code=2)",
            "raise typer.Exit()",
            "raise typer.Exit(1)",
            "raise Exit(code=3)",
        ],
    )
    def test_typer_exit_is_flagged(self, raise_stmt: str) -> None:
        offences = _offending_typer_exits(_typer_exit_source(raise_stmt))
        assert offences, f"{raise_stmt!r} should be flagged"
        assert offences[0][0] == "sub"

    @pytest.mark.parametrize(
        "raise_stmt",
        [
            "raise SystemExit(2)",
            "raise SystemExit(code)",
            "raise RuntimeError('boom')",
            "return 'done'",
        ],
    )
    def test_systemexit_and_other_raises_do_not_trip(self, raise_stmt: str) -> None:
        assert _offending_typer_exits(_typer_exit_source(raise_stmt)) == []

    def test_initialize_decorated_method_is_also_scanned(self) -> None:
        source = _typer_exit_source("raise typer.Exit(code=2)", decorator="@initialize()")
        assert _offending_typer_exits(source)

    def test_undecorated_helper_is_not_scanned(self) -> None:
        source = textwrap.dedent(
            """
            def _helper():
                raise typer.Exit(code=2)
            """,
        )
        assert _offending_typer_exits(source) == []

    def test_shipped_management_commands_are_clean(self) -> None:
        """The live command tree raises SystemExit everywhere — no swallowed typer.Exit.

        Pins the claim that the fix landed across the whole tree. A NEW command
        that raises typer.Exit must be fixed to SystemExit, not exempted here.
        """
        flagged: set[tuple[str, str]] = {
            (module.name, func_name)
            for module in _management_command_modules()
            for func_name, _lineno in _offending_typer_exits(module.read_text())
        }
        assert flagged == set(), (
            "Management command(s) raising typer.Exit (swallowed under call_command) — "
            f"fix to raise SystemExit(N): {sorted(flagged)}"
        )


def _command_source(return_value: str) -> str:
    """Minimal @command-decorated method whose return is ``return_value``."""
    return textwrap.dedent(
        f"""
        class Command:
            @command()
            def sub(self):
                return {return_value}
        """,
    )


class TestBroadenedKeywordSet:
    """#939: every newly-covered keyword trips the guard; benign returns don't."""

    @pytest.mark.parametrize(
        ("keyword", "message"),
        [
            ("fail", '"DB import failed for db."'),
            ("error", '"error: could not connect"'),
            ("abort", '"Fresh remote dump aborted"'),
            ("unknown", '"Unknown config key: x"'),
            ("not configured", '"reset-passwords not configured"'),
            ("not found", '"Worktree not found for path"'),
            ("not running", '"Backend not running on port"'),
            ("requires ", '"This command requires BASE_URL"'),
        ],
    )
    def test_each_failure_keyword_is_flagged(self, keyword: str, message: str) -> None:
        assert keyword in _FAILURE_KEYWORDS
        offences = _offending_returns(_command_source(message))
        assert offences, f"keyword {keyword!r} in {message} should be flagged"
        assert offences[0][0] == "sub"

    @pytest.mark.parametrize(
        "message",
        [
            '"completed"',
            '"Pushed: 3 commits"',
            '"No DB import strategy"',
            '"refreshed"',
            '"\\n".join(parts)',  # dynamic, non-string-constant return — out of scope
        ],
    )
    def test_benign_returns_do_not_trip(self, message: str) -> None:
        assert _offending_returns(_command_source(message)) == []

    def test_match_is_case_insensitive(self) -> None:
        assert _offending_returns(_command_source('"FATAL ERROR"'))
        assert _offending_returns(_command_source('"Aborted."'))

    def test_module_level_helper_is_not_scanned(self) -> None:
        source = textwrap.dedent(
            """
            def push_unsynced_branch():
                return "Push failed: remote rejected"
            """,
        )
        assert _offending_returns(source) == []

    def test_broadened_set_flags_only_the_known_sites(self) -> None:
        """Full command tree: no command returns a failure string.

        Pins the low-false-positive claim. If a NEW command legitimately
        returns a keyword string, fix it to raise SystemExit(1) — do not
        relax the keyword set.
        """
        flagged: set[tuple[str, str]] = {
            (module.name, func_name)
            for module in _management_command_modules()
            for func_name, _lineno, _text in _offending_returns(module.read_text())
        }
        assert flagged == set(), (
            "Unexpected command(s) returning a failure string — fix them to "
            f"raise SystemExit(1), do not relax the guard: {sorted(flagged)}"
        )


class TestScannedModuleSet:
    def test_the_subpackage_command_modules_are_scanned(self) -> None:
        # `_ship/`, `_workspace/` and friends define @command methods exactly like
        # the top-level modules; a single-level glob left them unguarded.
        scanned = {str(path.relative_to(_COMMANDS_DIR)) for path in _management_command_modules()}
        nested = {name for name in scanned if "/" in name}
        assert nested, "no nested command module was scanned — the glob is single-level again"
        assert "ticket.py" in scanned, "the top-level modules must stay in scope too"


class TestNonStringFailureReturns:
    """``return 1`` and ``return {"error": …}`` exit 0 exactly like the string form."""

    @pytest.mark.parametrize(
        "return_value",
        [
            "1",
            "2",
            '{"error": "could not connect"}',
            '{"ok": False, "error": str(exc)}',
            '{"recorded": False, "error": f"Ticket {ticket_id} not found"}',
            '{"status": "failed"}',
        ],
    )
    def test_a_non_string_failure_return_is_flagged(self, return_value: str) -> None:
        offences = _offending_exit_contract_returns(_command_source(return_value))
        assert offences, f"{return_value} should be flagged"
        assert offences[0][0] == "sub"

    @pytest.mark.parametrize(
        "return_value",
        [
            "0",
            "None",
            '{"recorded": True, "url": url}',
            '{"pipeline_id": 700, "status": "running"}',
            "build_payload()",
        ],
    )
    def test_a_benign_return_does_not_trip(self, return_value: str) -> None:
        assert _offending_exit_contract_returns(_command_source(return_value)) == []

    def test_a_module_level_helper_is_not_scanned(self) -> None:
        source = textwrap.dedent(
            """
            def _helper():
                return {"error": "remote rejected"}
            """,
        )
        assert _offending_exit_contract_returns(source) == []

    def test_no_new_command_signals_failure_by_a_non_string_return(self) -> None:
        """The ratchet: the known set may shrink, never grow.

        The listed sites each report success on a real failure today; they are
        recorded so the defect is visible and bounded rather than invisible. A
        command added or changed to join them fails here — fix it to
        ``self.stderr.write(...)`` + ``raise SystemExit(1)``, and never extend
        this set.
        """
        new = _flagged_exit_contract_offenders() - _KNOWN_EXIT_CONTRACT_OFFENDERS
        assert new == set(), (
            "Command(s) signalling failure by a non-string return, which django-typer "
            f"serialises while the process exits 0 — raise SystemExit(1) instead: {sorted(new)}"
        )

    def test_no_known_exit_contract_offender_is_stale(self) -> None:
        """The other direction: an entry the detector no longer flags must be deleted.

        Without this, a fixed site keeps its grandfathering and the ratchet silently
        re-widens — the entry would grant a future regression at that exact site a
        free pass. It also proves every recorded path resolves: a typo, or a module
        that moved, reds here rather than quietly exempting nothing.
        """
        stale = _KNOWN_EXIT_CONTRACT_OFFENDERS - _flagged_exit_contract_offenders()
        assert stale == set(), (
            "Grandfathered exit-contract offender(s) the detector no longer flags — "
            f"delete them from _KNOWN_EXIT_CONTRACT_OFFENDERS so the ratchet stays tight: {sorted(stale)}"
        )
