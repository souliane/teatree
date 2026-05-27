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
does NOT reimplement matching or add any new term config.

The module is pure detection. The PreToolUse hook in
``hooks/scripts/hook_router.py`` is the only place that knows about
``stdout`` / ``permissionDecision`` JSON.

Override via the ``--allow-banned-term`` flag in the first command
segment, or ``ALLOW_BANNED_TERM=1`` in the tool-input env mapping —
mirroring the quote-scanner's ``--quote-ok`` / ``QUOTE_OK=1`` escape
hatch.
"""

import os
import tempfile
from pathlib import Path
from typing import TypedDict

from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import first_segment_words as _first_segment_words
from teatree.hooks._command_parser import is_publish_command as _is_publish_command
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

_OVERRIDE_FLAG = "--allow-banned-term"
_OVERRIDE_ENV = "ALLOW_BANNED_TERM"

# How long to wait for the shell scanner before failing open. A hook that
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


def extract_publish_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    """Return the text-to-scan from a tool invocation, or ``None`` if not a publish.

    Reuses the #1213 ``_command_parser`` so the publish-surface catalogue
    and body extraction (``--body``, ``--body-file``, ``-d``/``--field``
    JSON, ``-m``, heredocs) stay in one place across both gates.
    """
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "")
    if not _is_publish_command(command):
        return None
    return _extract_bash_payload(command)


def has_override(tool_name: str, tool_input: ToolInput) -> bool:
    """Return True iff the caller explicitly opted out of the gate.

    The ``--allow-banned-term`` flag is honoured only when it appears as a
    token in the FIRST command segment (anything after a command-separator
    metacharacter is a separate command and must not bypass the gate).
    ``ALLOW_BANNED_TERM=1`` in the tool-input env mapping also bypasses.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if _OVERRIDE_FLAG in _first_segment_words(command):
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

    Fails open (returns ``None``) on a missing config, a missing script,
    or any subprocess error — a crashing gate is worse than no scan.
    """
    if not text:
        return None
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
        # check-banned-terms.sh: exit 0 = clean, exit 1 = banned term found.
        # Any other code means the script itself failed — fail open.
        result = run_allowed_to_fail(
            [str(script), "--config", str(cfg), str(scan_file)],
            expected_codes=(0, 1),
            timeout=_SCAN_TIMEOUT_S,
        )
    except (TimeoutExpired, CommandFailedError, OSError):
        return None
    finally:
        scan_file.unlink(missing_ok=True)

    if result.returncode == 0:
        return None
    return _matched_term(result.stdout)


def _matched_term(report: str) -> str | None:
    """Pull the banned term out of ``check-banned-terms.sh``'s report.

    The script prints ``BANNED TERM in <file>:`` followed by indented
    ``<lineno>:<line>`` rows, then a trailing ``Banned terms: a, b, c``
    line listing every configured term. The offending term is whichever
    configured term appears in a flagged line — the first such match is
    reported.
    """
    lines = report.splitlines()
    configured: list[str] = []
    flagged: list[str] = []
    for line in lines:
        if line.startswith("Banned terms:"):
            configured = [t.strip() for t in line.removeprefix("Banned terms:").split(",") if t.strip()]
        elif line.startswith("  ") and ":" in line:
            flagged.append(line)
    haystack = "\n".join(flagged).lower()
    for term in configured:
        if term.lower() in haystack:
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
