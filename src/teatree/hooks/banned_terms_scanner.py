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
``check-banned-terms.sh`` against the DB-home term list — it
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

"""

import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import extract_secret_scan_text as _extract_secret_scan_text
from teatree.hooks._command_parser import is_publish_command as _is_publish_command
from teatree.hooks._parser_primitives import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._parser_primitives import is_unavailable_body_source_sentinel as _is_unavailable_body_source_sentinel
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnreadableError, BannedTermsUnsetError
from teatree.hooks.term_match import matched_term as _matched_token_term
from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

# Marker returned by ``scan_text`` when the body source cannot be resolved
# (a missing ``--body-file``, an unreadable path). The gate blocks on this
# sentinel rather than slipping an unscanned body through. Callers that need
# to emit a message MUST check for this marker explicitly and use
# ``format_unresolvable_body_message`` — it is NOT a configured banned term.
UNRESOLVABLE_BODY_MARKER: str = "<unresolvable-publish-body>"

# Marker returned by ``scan_text`` when the body source is FUNDAMENTALLY
# unavailable before the command runs — an unexpanded ``$VAR`` or a stdin body
# (``--input -``, ``git commit -F -``). Distinct from
# ``UNRESOLVABLE_BODY_MARKER`` (a missing FILE) so the gate renders the
# actionable "write the body to an absolute file and use ``--body-file
# <abspath>``" message instead of the misleading "body file is missing" one
# (#2369). The gate BLOCKS on it the same way — an unscannable body never
# publishes unscanned — only the operator-facing reason differs.
UNAVAILABLE_BODY_SOURCE_MARKER: str = "<unavailable-publish-body-source>"

# Marker returned by ``scan_text`` when the shell scanner could NOT run — a
# crashing interpreter (an old system ``python3`` below the repo's >= 3.13
# floor crashes importing the matcher), a timeout, or any unexpected exit. The
# gate BLOCKS on this marker: a scanner that cannot run must never resolve to
# ALLOW (#1954). It is NOT a configured banned term; callers emit
# ``format_scanner_unavailable_message`` for it.
SCANNER_UNAVAILABLE_MARKER: str = "<banned-terms-scanner-unavailable>"

# Marker returned when the scanner was KILLED at the budget below rather than
# failing on its own. It BLOCKS exactly like the marker above; it is separate
# because the two call for opposite remedies — widen the budget vs repair the
# install — and one shared string reported a burst of load-driven timeouts as ten
# broken interpreters, sending operators to reinstall a healthy uv.
SCANNER_TIMEOUT_MARKER: str = "<banned-terms-scanner-timeout>"

# Marker returned when the TERM STORE errored on read, so the gate cannot tell an empty
# term list from one it could not see. Separate from the two markers above because their
# message sends the operator to repair an interpreter, which is not the fault here and not
# the remedy: this store is present and corrupt, locked, or missing its table. A store that
# is simply ABSENT is not this — it is a confirmed answer, and carries no marker.
STORE_UNREADABLE_MARKER: str = "<banned-terms-store-unreadable>"

# Marker returned when the store was read but holds no term list. The shell
# scanner is skipped on this path, so the publish gate must refuse directly.
TERMS_UNSET_MARKER: str = "<banned-terms-unset>"

# Its deny reason. The remedy is the operator's own configuration, not a repair.
_TERMS_UNSET_DENY: str = (
    "BLOCKED: banned-terms posting gate. No banned-term list is configured. "
    "Configure banned_term_registry with terms for the scan before publishing. "
    "Failing closed: an unscanned body is not allowed onto a public surface."
)

# Its deny reason. Separate from the scanner messages for the reason one of them was
# itself rewritten: naming the wrong cause sends the operator to repair something that is
# not broken, and nothing here is an interpreter problem. A constant rather than a
# formatter because it interpolates nothing — the shape ``gate._BANNED_TERMS_CREDENTIAL_DENY``
# already uses in this family.
_STORE_UNREADABLE_DENY: str = (
    "BLOCKED: banned-terms posting gate (#1415/#4008). The term store is HERE and would not READ, "
    "so this gate cannot tell an empty term list from a list it simply could not see. The config DB "
    "is corrupt, locked by a live writer, or missing its table, and no published projection answers "
    "this key either — `t3 doctor check` says which. Failing closed: an unscanned body is not "
    "allowed onto a public surface. Ask the owner to resolve the unreadable store."
)

# How long to wait for the shell scanner before failing closed. The harness
# SIGKILLs a hook at its ``hooks.json`` timeout (30s) and a killed hook returns no
# verdict at all, so the default stays well under that ceiling; a venue slower than
# a dev laptop (a contended CI runner) widens it through the env seam instead of
# presenting its own load as a broken scanner.
SCAN_TIMEOUT_DEFAULT_S = 10
SCAN_TIMEOUT_ENV = "T3_BANNED_TERMS_SCAN_TIMEOUT_S"

# The term and allowlist classes are read through ``banned_term_registry``.


class ToolInput(TypedDict, total=False):
    """Subset of the PreToolUse ``tool_input`` payload this gate reads."""

    command: str
    env: dict[str, str]


def _banned_terms_configured(config_path: Path | None) -> bool:
    """Return True iff the banned-term registry is set.

    The gate refuses when nothing is configured, before trying to shell out.
    Configured means the ``banned_term_registry`` is present (DB row or
    ``$TEATREE_TERM_REGISTRY`` secret). *config_path* overrides the
    DB path (else the canonical DB / ``T3_CONFIG_DB``). A malformed registry
    propagates :class:`BannedTermsUnsetError` (fail-loud), never a silent no-op.

    A store that ERRORED on read (locked, corrupt, missing its table) raises
    :class:`BannedTermsUnreadableError` rather than resolving to "not configured":
    that read is indistinguishable from an unset row, and treating it as unset let a
    busy store open this publish-surface gate the same way it opened the commit-only
    shell scanner (#4008). Confirmed absence returns ``False`` so the caller
    can identify the missing installation value separately from an unreadable store.
    """
    from teatree.hooks.banned_term_registry import load_registry  # noqa: PLC0415 — cold-path import

    return load_registry(db_path=config_path) is not None


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


def scan_text(text: str, *, config_path: Path | None = None) -> str | None:
    """Run ``check-banned-terms.sh`` against ``text``; return the matched term, else ``None``.

    The shell scanner reads FILES, not stdin — the payload is written to a
    temp file and the script is invoked exactly as the pre-commit hook
    does (``check-banned-terms.sh --config <toml> <file>``). A non-zero
    exit means a banned term was found; the matched term is parsed back
    out of the script's ``BANNED TERM in <file>:`` report.

    A body the parser could not resolve carries a fail-closed sentinel. It is
    recognised EXPLICITLY as a match and BLOCKS -- the sentinel is not a
    configured banned term, so delegating it to ``check-banned-terms.sh`` would
    return clean and a PUBLIC body the gate cannot read would slip through
    unread. Two sentinel classes map to two markers so the caller can render the
    right operator advice (#2369): a FUNDAMENTALLY-unavailable body source (an
    unexpanded ``$VAR`` / a stdin body) yields
    :data:`UNAVAILABLE_BODY_SOURCE_MARKER`, and a missing/unreadable FILE yields
    :data:`UNRESOLVABLE_BODY_MARKER`. Both BLOCK; only the message differs. The
    sibling ``quote_scanner`` blocks on both sentinels too.

    Returns ``None`` only on a genuine no-op (nothing configured — there is
    nothing to scan, matching the shell hook's own no-op contract). A scanner
    that was supposed to run but could NOT — a crashing interpreter, a timeout,
    an unexpected exit, an exit-1 with no parseable report, OR a MISSING scanner
    script while banned-terms is configured (HLG-7) — returns
    :data:`SCANNER_UNAVAILABLE_MARKER` so the gate FAILS CLOSED, never a silent
    clean scan: a security gate that fails open on a broken scanner is the bug
    class (#1954).
    """
    if not text:
        return None
    if _is_unavailable_body_source_sentinel(text):
        return UNAVAILABLE_BODY_SOURCE_MARKER
    if _is_fail_closed_sentinel(text):
        return UNRESOLVABLE_BODY_MARKER
    return _run_shell_scanner(text, config_path)


def _configured_or_unreadable(config_path: Path | None) -> bool | None:
    """Resolve the registry: present, absent/malformed, or unreadable.

    Isolated from :func:`_run_shell_scanner` so its own unreadable-store handling
    does not add to that function's return-statement count. ``None`` is an
    unreadable store; ``False`` is a confirmed absence or malformed registry.
    Both outcomes produce blocking markers.
    """
    try:
        return _banned_terms_configured(config_path)
    except BannedTermsUnreadableError:
        sys.stderr.write(
            "[teatree] NOTE: banned-terms could not be READ from the config store while "
            "resolving whether the gate is configured. Failing CLOSED rather than reporting "
            "a clean scan — retry, or repair the store.\n"
        )
        return None
    except BannedTermsUnsetError:
        return False


def _scan_timeout_s() -> int:
    """Return the effective scanner budget: the env seam when usable, else the default."""
    raw = os.environ.get(SCAN_TIMEOUT_ENV, "").strip()
    return int(raw) if raw.isdecimal() and int(raw) > 0 else SCAN_TIMEOUT_DEFAULT_S


def _marker_for_scanner_failure(exc: Exception, timeout: int) -> str:
    """Name which failure class stopped the scanner on stderr, and map it to its marker."""
    if isinstance(exc, TimeoutExpired):
        sys.stderr.write(
            f"[teatree] NOTE: banned-terms scanner timed out after {timeout}s. Failing CLOSED. "
            f"If this venue is simply slower than the budget assumes, widen it with "
            f"{SCAN_TIMEOUT_ENV}=<seconds> rather than treating the load as a broken scanner.\n"
        )
        return SCANNER_TIMEOUT_MARKER
    sys.stderr.write(
        f"[teatree] NOTE: banned-terms scanner could not run ({type(exc).__name__}: {exc}). "
        "Failing CLOSED rather than reporting a clean scan.\n"
    )
    return SCANNER_UNAVAILABLE_MARKER


def _no_term_list_verdict(*, configured: bool | None) -> str:
    """Distinguish an unreadable store from an absent term list; refuse both."""
    if configured is None:
        return STORE_UNREADABLE_MARKER
    return TERMS_UNSET_MARKER


def _run_shell_scanner(text: str, config_path: Path | None) -> str | None:
    """Delegate ``text`` to ``check-banned-terms.sh``; return the matched term, else ``None``.

    Writes ``text`` to a temp file and invokes the shell scanner (which reads the
    DB-home term list). Returns ``None`` only after a clean scan. Returns
    :data:`SCANNER_UNAVAILABLE_MARKER` (the gate fails CLOSED) when the scanner
    could not run, INCLUDING a MISSING scanner script while banned-terms IS
    configured (HLG-7), OR the legacy row could not be READ at all (#4008): both
    are a broken install/store, not a clean scan, so both fail loud + closed like
    a crashing interpreter (#1954) rather than silently reporting clean.
    *config_path* overrides the DB path the shell scanner reads (forwarded as
    ``T3_CONFIG_DB`` in the subprocess env).
    """
    configured = _configured_or_unreadable(config_path)
    if configured is not True:
        return _no_term_list_verdict(configured=configured)
    script = _scanner_script()
    if not script.is_file():
        # banned-terms IS configured (checked above) but the scanner script is
        # ABSENT — a broken install, not a clean scan. Silently returning ``None``
        # made a missing script indistinguishable from a real clean scan, so a
        # PUBLIC body slipped through unscanned (HLG-7). Fail LOUD + CLOSED like a
        # crashing interpreter (#1954): a scanner that cannot run must never resolve
        # to ALLOW. The never-lockout escape is the ``banned_terms_gate_enabled``
        # kill-switch.
        sys.stderr.write(
            f"[teatree] NOTE: banned-terms scanner script is missing at {script} while "
            "banned-terms is configured. Failing CLOSED rather than reporting a clean scan — "
            "restore scripts/hooks/check-banned-terms.sh (or disable the gate) before posting "
            "to a public surface.\n"
        )
        return SCANNER_UNAVAILABLE_MARKER

    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as fh:
        fh.write(text)
        scan_file = Path(fh.name)
    env = {**os.environ, "T3_CONFIG_DB": str(config_path)} if config_path is not None else None
    timeout = _scan_timeout_s()
    try:
        # check-banned-terms.sh contract: exit 0 = clean, exit 1 = banned term
        # found (with a BANNED TERM report on stdout), exit 2 = the scanner
        # could not run (an old interpreter / import crash), the term store could
        # not be read, OR the term list is genuinely unset on a deployment that
        # requires one. Any other code is also a scanner failure. A failed scanner
        # fails CLOSED, never ALLOW.
        result = run_allowed_to_fail(
            [str(script), str(scan_file)],
            expected_codes=(0, 1),
            timeout=timeout,
            env=env,
        )
    except (TimeoutExpired, CommandFailedError, OSError) as exc:
        return _marker_for_scanner_failure(exc, timeout)
    finally:
        scan_file.unlink(missing_ok=True)

    if result.returncode == 0:
        return None
    term = _matched_term(result.stdout, _load_allowlist(config_path))
    # Exit 1 with NO parseable BANNED TERM report is the import-crash shape: a
    # Python traceback exits 1 (colliding with "banned term found") but prints
    # nothing on stdout. There is no real match — the scanner crashed — so fail
    # CLOSED rather than read the empty report as a clean scan.
    if term is None:
        return SCANNER_UNAVAILABLE_MARKER
    return term


def _load_allowlist(config_path: Path | None) -> tuple[str, ...]:
    """Return the registry ``allow`` carve-out.

    Mirrors :func:`banned_terms_cli._load_allowlist` so the report-attribution
    path here and the shell scanner's matching path read the SAME carve-out. The
    shell scanner already blanks allow-listed identifier runs when flagging a
    line, so this is only used to keep the REPORTED term in sync — a line flagged
    for a genuine customer codename next to a company identifier must attribute
    the codename, never the carved-out org slug. Reads the consolidated
    ``banned_term_registry`` ``allow`` class from the ``ConfigSetting`` store
    via :mod:`teatree.config.cold_reader`; *config_path* overrides the DB path.
    Empty (default) is a no-op.
    """
    from teatree.hooks.banned_term_registry import allowlist_terms  # noqa: PLC0415 — cold-path import

    return allowlist_terms(config_path)


def _matched_term(report: str, allowlist: tuple[str, ...] = ()) -> str | None:
    """Pull the banned term out of ``check-banned-terms.sh``'s report.

    The script prints ``BANNED TERM in <file>:`` followed by indented
    ``<lineno>:<line>`` rows, then a trailing ``Banned terms: a, b, c``
    line listing every configured term. The offending term is whichever
    configured term's tokens appear as a whole-token run in a flagged line.

    Attribution uses the SAME whole-token matcher the shell scanner used to
    flag the line (``teatree.hooks.term_match``), with the SAME company-identifier
    *allowlist* carve-out, so the reported term can never be a substring
    coincidence (the old ``term in haystack`` check would, for a neutral example,
    name ``acme`` for a line that only said ``acmecorp``) NOR an allow-listed org
    slug carved out of a company identifier (it would otherwise name the org slug
    for a line whose real hit is a customer codename beside that identifier).
    """
    lines = report.splitlines()
    configured: list[str] = []
    flagged: list[str] = []
    for line in lines:
        if line.startswith("Banned terms:"):
            configured = [t.strip() for t in line.removeprefix("Banned terms:").split(",") if t.strip()]
        elif line.startswith("  ") and ":" in line:
            flagged.append(line)
    term = _matched_token_term("\n".join(flagged), tuple(configured), allowlist)
    if term is not None:
        return term
    return configured[0] if configured else None


def format_block_message(term: str) -> str:
    """Render the PreToolUse deny reason for a banned-term match.

    Public egress has no override; the owner reviews a blocked publication.
    """
    return (
        f"BLOCKED: banned-terms posting gate (#1415). The body carries the banned term "
        f"'{term}'. Rephrase without the matched term before posting to the public surface, "
        "or ask the owner if this is a false match. "
        "Ask the owner to review the blocked publication."
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


def format_unavailable_body_source_message() -> str:
    """Render the PreToolUse deny reason when the publish body source is unavailable.

    The body comes from an unexpanded shell variable (``--body "$VAR"``) or a
    stdin stream (``gh api --input -``, ``git commit -F -``), neither of which
    exists for the gate to scan BEFORE the command runs. The fail-closed
    direction is correct — an unscanned body must not publish — but the operator
    advice is the OPPOSITE of the missing-file message: write the body to an
    absolute on-disk file and pass ``--body-file <abspath>`` so the gate can read
    and scan it (#2369).
    """
    return (
        "BLOCKED: banned-terms posting gate (#1415/#2369). The publish body comes from an "
        "unexpanded variable or stdin, which the gate cannot scan before the command runs. "
        "Write the body to an absolute file and post it with --body-file <abspath> (the gate "
        "reads an absolute body file regardless of cwd), or pass the body inline with "
        "-m/--body/--message."
    )


def format_scanner_unavailable_message() -> str:
    """Render the PreToolUse deny reason when the banned-terms scanner could not run.

    The shell scanner could not resolve a runnable interpreter — most often
    because ``uv`` on PATH is a version-manager shim keyed on the CWD repo's
    ``.python-version`` and exits 127 before uv runs, and otherwise because the
    ``python3`` fallback is below the repo's >= 3.13 floor. The gate fails
    CLOSED rather than let an unscanned body through — a security gate that
    fails open on a crash is the bug class (#1954).

    The message names INTERPRETER RESOLUTION as the cause. The previous wording
    ("its interpreter cannot import the matcher — install uv") misdirected the
    operator into reinstalling a uv that was already installed and healthy,
    while the real fault was the shim in front of it.
    """
    return (
        "BLOCKED: banned-terms posting gate (#1415/#1954). The scanner could not run — this "
        "is an interpreter/uv RESOLUTION failure, not a matcher problem. Usually 'uv' on PATH "
        "is a version-manager shim (pyenv/asdf) that picks its interpreter from the CWD repo's "
        ".python-version and exits 127 before uv runs, so reinstalling uv does not help: point "
        "T3_UV at a real uv binary, or make uv / a Python >= 3.13 reachable outside the shim. "
        "Run the hook directly to see the full reason: scripts/hooks/check-banned-terms.sh "
        "<file>. Failing closed: an unscanned body is not allowed onto a public surface."
    )


def format_scanner_timeout_message() -> str:
    """Render the PreToolUse deny reason when the scanner was killed at its budget.

    Separate from :func:`format_scanner_unavailable_message` because the remedy is
    the opposite one: nothing is broken, the venue is slower than the budget
    assumes, so the message names the budget and the seam that widens it instead of
    sending the operator to reinstall a healthy interpreter.
    """
    return (
        f"BLOCKED: banned-terms posting gate (#1415/#1954). The scanner did not finish within its "
        f"{_scan_timeout_s()}s budget and was killed — a LOAD signal, not a broken install, so "
        f"reinstalling uv or a Python will not help. Retry, or give this venue a wider budget with "
        f"{SCAN_TIMEOUT_ENV}=<seconds> (keep it under the harness's own 30s hook ceiling, or the "
        f"hook is killed before it can report at all). Failing closed: an unscanned body is not "
        f"allowed onto a public surface."
    )


# Every marker's deny reason, as a table: a new marker that forgets its row renders no
# message at all, which reads to the caller as a configured term and picks up the
# private-destination downgrade — the fail-OPEN the routing exists to prevent.
_MARKER_DENY_RENDERERS: dict[str, Callable[[], str]] = {
    UNAVAILABLE_BODY_SOURCE_MARKER: format_unavailable_body_source_message,
    UNRESOLVABLE_BODY_MARKER: format_unresolvable_body_message,
    SCANNER_TIMEOUT_MARKER: format_scanner_timeout_message,
    SCANNER_UNAVAILABLE_MARKER: format_scanner_unavailable_message,
    STORE_UNREADABLE_MARKER: lambda: _STORE_UNREADABLE_DENY,
    TERMS_UNSET_MARKER: lambda: _TERMS_UNSET_DENY,
}


def marker_deny_message(term: str) -> str | None:
    """Return the deny reason for a fail-closed marker, or ``None`` for a real term.

    ``scan_text`` returns either a configured banned term or one of the
    fail-closed markers (an unavailable body source, an unresolvable body, a
    timed-out scanner, an unavailable scanner). The markers are NOT configured
    terms, so the caller must render a dedicated message instead of
    ``format_block_message``. A real term returns ``None`` here so the caller takes
    its destination-aware banned-term path — which means every marker MUST be
    routed here: an unrouted one would be mistaken for a configured term and pick
    up that path's private-destination downgrade, failing OPEN.
    """
    renderer = _MARKER_DENY_RENDERERS.get(term)
    return renderer() if renderer is not None else None
