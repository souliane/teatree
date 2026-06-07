"""Banned-terms posting gate (#1415).

The commit-only ``scripts/hooks/check-banned-terms.sh`` hook runs only on
``git commit`` — it misses every non-commit write to a public surface
(``gh issue/pr create|edit|comment``, ``glab mr|issue note|create``, the
``gh api`` / ``glab api`` REST paths), which is exactly where overlay-
and customer-specific terms have leaked.

This module is the sibling of the #1213 quote-scanner gate. It reuses
the *same* publish-surface detection and body extraction
(``teatree.hooks._command_parser``) so a single token-aware parser feeds
both gates, then delegates the *matching* to the existing
``check-banned-terms.sh`` against the ``~/.teatree.toml`` term list — it
adds no new term config. The shell scanner and this module both match on
WHOLE TOKENS (``teatree.hooks.term_match``): a configured term matches
only when its own tokens appear as a contiguous run of whole tokens, so a
short term never surfaces inside a longer unbroken word (a neutral example:
a term ``acme`` no longer matches ``acmecorp`` / ``pacme``). That same
matcher attributes which term tripped a flagged line, so the reported term
is never a substring coincidence.

The module is pure detection. The PreToolUse hook in
``hooks/scripts/hook_router.py`` is the only place that knows about
``stdout`` / ``permissionDecision`` JSON.

Override via the ``--allow-banned-term`` flag in the first command
segment, a leading ``ALLOW_BANNED_TERM=1`` inline env-assignment token on
the publish segment itself (``ALLOW_BANNED_TERM=1 glab ...`` or
``cd <worktree> && ALLOW_BANNED_TERM=1 git commit ...`` — bash scopes the
assignment to that command), the ``ALLOW_BANNED_TERM=1`` process env var,
or ``ALLOW_BANNED_TERM=1`` in the tool-input env mapping — mirroring the
quote-scanner's ``--quote-ok`` / ``QUOTE_OK=1`` escape hatch.
"""

import os
import tempfile
from pathlib import Path
from typing import TypedDict

from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import extract_secret_scan_text as _extract_secret_scan_text
from teatree.hooks._command_parser import first_segment_words as _first_segment_words
from teatree.hooks._command_parser import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._command_parser import is_publish_command as _is_publish_command
from teatree.hooks._publish_detection import segment_word_lists_raw as _segment_word_lists_raw
from teatree.hooks.term_match import matched_term as _matched_token_term
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

_OVERRIDE_FLAG = "--allow-banned-term"
_OVERRIDE_ENV = "ALLOW_BANNED_TERM"

# Marker returned by ``scan_text`` when the body source cannot be resolved
# (a missing ``--body-file``, an unreadable path). The gate blocks on this
# sentinel rather than slipping an unscanned body through. Callers that need
# to emit a message MUST check for this marker explicitly and use
# ``format_unresolvable_body_message`` — it is NOT a configured banned term.
UNRESOLVABLE_BODY_MARKER: str = "<unresolvable-publish-body>"

# Marker returned by ``scan_text`` when the shell scanner could NOT run — a
# crashing interpreter (an old system ``python3`` below the repo's >= 3.13
# floor crashes importing the matcher), a timeout, or any unexpected exit. The
# gate BLOCKS on this marker: a scanner that cannot run must never resolve to
# ALLOW (#1954). It is NOT a configured banned term; callers emit
# ``format_scanner_unavailable_message`` for it.
SCANNER_UNAVAILABLE_MARKER: str = "<banned-terms-scanner-unavailable>"

# How long to wait for the shell scanner before failing closed. A hook that
# hangs blocks the user, so the budget is deliberately tight.
_SCAN_TIMEOUT_S = 10


class ToolInput(TypedDict, total=False):
    """Subset of the PreToolUse ``tool_input`` payload this gate reads."""

    command: str
    env: dict[str, str]


def resolve_config() -> Path | None:
    """Resolve the ``~/.teatree.toml`` term-list config.

    ``T3_BANNED_TERMS_CONFIG`` overrides the default (used by tests to
    avoid touching the real config). Returns ``None`` when no config file
    exists — the gate then fails open, matching ``check-banned-terms.sh``
    itself (no config ⇒ no-op).
    """
    override = os.environ.get("T3_BANNED_TERMS_CONFIG")
    candidate = Path(override) if override else Path.home() / ".teatree.toml"
    return candidate if candidate.is_file() else None


def _scanner_script() -> Path:
    """Locate ``check-banned-terms.sh`` relative to this module's repo.

    The hook script runs in the user's session shell with no guarantee
    that the CWD is the repo, so the path is resolved from this module's
    own location: ``src/teatree/hooks/`` → repo root → ``scripts/hooks``.
    """
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "scripts" / "hooks" / "check-banned-terms.sh"


def extract_publish_payload(tool_name: str, tool_input: ToolInput, cwd: Path | None = None) -> str | None:
    """Return the text-to-scan from a tool invocation, or ``None`` if not a publish.

    Reuses the #1213 ``_command_parser`` so the publish-surface catalogue
    and body extraction (``--body``, ``--body-file``, ``-d``/``--field``
    JSON, ``-m``, heredocs) stay in one place across both gates.

    ``cwd`` is the harness-provided working directory; it is the fallback base
    for resolving a ``git commit -F <relpath>`` body file when the command
    names no commit dir of its own, so a relative body file unreadable from
    the cold hook's reset cwd is still scanned.
    """
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "")
    if not _is_publish_command(command):
        return None
    return _extract_bash_payload(command, fail_closed_body_file=True, cwd=cwd)


def secret_scan_text(tool_name: str, tool_input: ToolInput) -> str:
    """Return EVERY surface a secret must be blocked on, regardless of destination.

    A secret leaks on ALL surfaces -- a body, a title, a short ``-t`` flag, a
    ``gh api`` field, a ``git -C`` commit subject -- so this widens beyond
    :func:`extract_publish_payload` (body only) and is scanned with
    :func:`publish_surface.contains_secret` BEFORE the destination skip can
    short-circuit. Empty for a non-Bash tool.
    """
    if tool_name != "Bash":
        return ""
    return _extract_secret_scan_text(tool_input.get("command", ""))


def _segment_leads_with_override(words: list[str]) -> bool:
    """Return True iff ``words`` leads with ``ALLOW_BANNED_TERM=1`` before its command.

    Only the leading run of ``KEY=value`` env-assignment tokens is inspected:
    bash applies a leading inline assignment to that command's environment, while
    a ``KEY=val``-shaped token after the command name is an argument, not an
    override. The first non-assignment token ends the run.
    """
    for word in words:
        name, sep, value = word.partition("=")
        if not sep:
            return False  # command name reached: later KEY=val tokens are args
        if name == _OVERRIDE_ENV:
            return value.strip() == "1"
    return False


def _has_leading_env_override(command: str) -> bool:
    """Return True iff the segment carrying the publish leads with ``ALLOW_BANNED_TERM=1``.

    The Claude Code harness forwards a ``Bash`` command verbatim and lets
    NEITHER an inline ``env`` block reach the gate NOR ``glab``/``gh`` accept
    a ``--allow-banned-term`` flag (they reject the unknown flag). The one
    spelling the agent CAN reliably emit is a leading inline env assignment
    on the command itself — ``ALLOW_BANNED_TERM=1 glab mr note ...`` — which
    bash applies to that command's environment.

    Bash scopes a leading inline assignment to that one command, so the override
    is honoured iff the segment it leads IS ITSELF the publish/commit the gate
    would scan (checked via :func:`_is_publish_command` on the standalone
    segment). This honours the common sub-agent shape that navigates first
    (``cd <worktree> && ALLOW_BANNED_TERM=1 git commit ...`` — override on the
    commit segment) while a decoy override on a harmless segment cannot vouch for
    a chained publish elsewhere (``ALLOW_BANNED_TERM=1 echo hi && gh issue create
    …`` and ``gh issue create … ; ALLOW_BANNED_TERM=1 echo`` both still fire).
    """
    for words in _segment_word_lists_raw(command):
        if _segment_leads_with_override(words) and _is_publish_command(" ".join(words)):
            return True
    return False


def has_override(tool_name: str, tool_input: ToolInput) -> bool:
    """Return True iff the caller explicitly opted out of the gate.

    The ``--allow-banned-term`` flag is honoured only when it appears as a
    token in the FIRST command segment (anything after a command-separator
    metacharacter is a separate command and must not bypass the gate). A
    leading ``ALLOW_BANNED_TERM=1`` inline env-assignment token in the first
    segment (``ALLOW_BANNED_TERM=1 glab ...``) is ALSO honoured: the harness
    forwards neither an inline ``env`` block nor a ``--allow-banned-term``
    flag glab/gh would accept, so the leading env-assignment is the spelling
    that actually reaches the gate.

    ``ALLOW_BANNED_TERM=1`` is honoured from the process environment
    (``os.environ``). The Claude Code PreToolUse payload for a ``Bash``
    tool carries NO ``env`` block, so the agent's ``ALLOW_BANNED_TERM=1``
    lives in the hook subprocess's own environment; reading only
    ``tool_input["env"]`` meant the documented override never reached the
    wrapper and forced numeric-id + paraphrase workarounds (#126).
    ``tool_input["env"]`` is still consulted for any harness build that
    DOES populate it. Mirrors ``quote_scanner.has_quote_ok_override``.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if _OVERRIDE_FLAG in _first_segment_words(command):
            return True
        if _has_leading_env_override(command):
            return True
    if os.environ.get(_OVERRIDE_ENV, "").strip() == "1":
        return True
    env = tool_input.get("env") or {}
    return env.get(_OVERRIDE_ENV, "").strip() == "1"


def scan_text(text: str, *, config_path: Path | None = None) -> str | None:
    """Run ``check-banned-terms.sh`` against ``text``; return the matched term, else ``None``.

    The shell scanner reads FILES, not stdin — the payload is written to a
    temp file and the script is invoked exactly as the pre-commit hook
    does (``check-banned-terms.sh --config <toml> <file>``). A non-zero
    exit means a banned term was found; the matched term is parsed back
    out of the script's ``BANNED TERM in <file>:`` report.

    A body the parser could not resolve carries the fail-closed sentinel
    (``FAIL_CLOSED_SENTINEL``). It is recognised EXPLICITLY as a match and
    BLOCKS -- the sentinel is not a configured banned term, so delegating it
    to ``check-banned-terms.sh`` would return clean and a PUBLIC file-body post
    whose body the gate cannot read would slip through unread. The sibling
    ``quote_scanner`` already blocks on this same sentinel; this closes the
    banned-terms parity gap.

    Returns ``None`` only on a genuine no-op (no config, no script — there is
    nothing to scan, matching the shell hook's own no-op contract). A scanner
    that was supposed to run but could NOT (a crashing interpreter, a timeout,
    an unexpected exit, or an exit-1 with no parseable report) returns
    :data:`SCANNER_UNAVAILABLE_MARKER` so the gate FAILS CLOSED — a security
    gate that fails open on a crash is the bug class (#1954).
    """
    if not text:
        return None
    if _is_fail_closed_sentinel(text):
        return UNRESOLVABLE_BODY_MARKER
    return _run_shell_scanner(text, config_path)


def _run_shell_scanner(text: str, config_path: Path | None) -> str | None:
    """Delegate ``text`` to ``check-banned-terms.sh``; return the matched term, else ``None``.

    Writes ``text`` to a temp file and invokes the shell scanner exactly as the
    pre-commit hook does. Returns ``None`` on a genuine no-op (no config /
    script). Returns :data:`SCANNER_UNAVAILABLE_MARKER` (the gate fails CLOSED)
    when the scanner could not run.
    """
    cfg = config_path if config_path is not None else resolve_config()
    if cfg is None or not cfg.is_file():
        return None
    script = _scanner_script()
    if not script.is_file():
        return None

    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as fh:
        fh.write(text)
        scan_file = Path(fh.name)
    try:
        # check-banned-terms.sh contract: exit 0 = clean, exit 1 = banned term
        # found (with a BANNED TERM report on stdout), exit 2 = the scanner
        # could not run (an old interpreter / import crash). Any other code is
        # also a scanner failure. A failed scanner fails CLOSED, never ALLOW.
        result = run_allowed_to_fail(
            [str(script), "--config", str(cfg), str(scan_file)],
            expected_codes=(0, 1),
            timeout=_SCAN_TIMEOUT_S,
        )
    except (TimeoutExpired, CommandFailedError, OSError):
        return SCANNER_UNAVAILABLE_MARKER
    finally:
        scan_file.unlink(missing_ok=True)

    if result.returncode == 0:
        return None
    term = _matched_term(result.stdout)
    # Exit 1 with NO parseable BANNED TERM report is the import-crash shape: a
    # Python traceback exits 1 (colliding with "banned term found") but prints
    # nothing on stdout. There is no real match — the scanner crashed — so fail
    # CLOSED rather than read the empty report as a clean scan.
    if term is None:
        return SCANNER_UNAVAILABLE_MARKER
    return term


def _matched_term(report: str) -> str | None:
    """Pull the banned term out of ``check-banned-terms.sh``'s report.

    The script prints ``BANNED TERM in <file>:`` followed by indented
    ``<lineno>:<line>`` rows, then a trailing ``Banned terms: a, b, c``
    line listing every configured term. The offending term is whichever
    configured term's tokens appear as a whole-token run in a flagged line.

    Attribution uses the SAME whole-token matcher the shell scanner used to
    flag the line (``teatree.hooks.term_match``), so the reported term can
    never be a substring coincidence (the old ``term in haystack`` check
    would, for a neutral example, name ``acme`` for a line that only said
    ``acmecorp``).
    """
    lines = report.splitlines()
    configured: list[str] = []
    flagged: list[str] = []
    for line in lines:
        if line.startswith("Banned terms:"):
            configured = [t.strip() for t in line.removeprefix("Banned terms:").split(",") if t.strip()]
        elif line.startswith("  ") and ":" in line:
            flagged.append(line)
    term = _matched_token_term("\n".join(flagged), tuple(configured))
    if term is not None:
        return term
    return configured[0] if configured else None


def format_block_message(term: str) -> str:
    """Render the PreToolUse deny reason for a banned-term match."""
    return (
        f"BLOCKED: banned-terms posting gate (#1415). The body carries the banned term "
        f"'{term}'. Remove the overlay/customer term before posting to the public surface. "
        f"If the match is a false positive, re-issue the command with {_OVERRIDE_FLAG} "
        f"(or set {_OVERRIDE_ENV}=1 in the tool env)."
    )


def format_unresolvable_body_message() -> str:
    """Render the PreToolUse deny reason when the publish body cannot be read.

    A missing ``--body-file`` or an unreadable path is blocked rather than
    passed unscanned. Use ``-m``/``--body`` with an inline value, or write
    the body to a file the command can resolve before posting.
    """
    return (
        "BLOCKED: banned-terms posting gate (#1415). The publish body could not be read "
        "(the body file is missing or unresolvable at scan time). Use an inline body "
        "(-m/--body/--message) or write the file content in the same command before posting."
    )


def format_scanner_unavailable_message() -> str:
    """Render the PreToolUse deny reason when the banned-terms scanner could not run.

    The shell scanner crashed or its interpreter cannot import the matcher
    (an old system ``python3`` below the repo's >= 3.13 floor). The gate fails
    CLOSED rather than let an unscanned body through — a security gate that
    fails open on a crash is the bug class (#1954). The fix the operator needs
    is a working ``uv`` or a Python >= 3.13 on PATH so the scanner runs.
    """
    return (
        "BLOCKED: banned-terms posting gate (#1415/#1954). The scanner could not run "
        "(its interpreter cannot import the matcher — install uv, or a Python >= 3.13, "
        "so the scanner runs). Failing closed: an unscanned body is not allowed onto a "
        "public surface."
    )


def marker_deny_message(term: str) -> str | None:
    """Return the deny reason for a fail-closed marker, or ``None`` for a real term.

    ``scan_text`` returns either a configured banned term or one of the
    fail-closed markers (an unresolvable body, an unavailable scanner). The
    markers are NOT configured terms, so the caller must render a dedicated
    message instead of ``format_block_message``. A real term returns ``None``
    here so the caller takes its destination-aware banned-term path.
    """
    if term == UNRESOLVABLE_BODY_MARKER:
        return format_unresolvable_body_message()
    if term == SCANNER_UNAVAILABLE_MARKER:
        return format_scanner_unavailable_message()
    return None
