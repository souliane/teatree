"""Deny a Bash command that bypasses the ``t3`` CLI (#2384 PR7).

Agents must drive workspace / server / database / test operations through the
``t3`` CLI, never the underlying tools (``manage.py runserver``, ``docker compose
up``, ``createdb``, ``playwright test``, ``pip install``, ``--no-verify``, the
``git -c core.hooksPath=…`` hook-silencer, the ``git push -o
merge_request.merge_when_pipeline_succeeds`` auto-merge, …). This gate closes
those bypasses at the Bash boundary: a command matching the denylist is denied
with a message naming the sanctioned ``t3`` path; a legitimate ``t3`` invocation
or a read-only command that merely MENTIONS a blocked tool passes through.

Conservative by construction. ``deny_match`` honours a ``t3``/read-only prefix
allowlist (so ``grep 'manage.py' README`` is not blocked) but only when the
command has no shell-chaining operator (F6: ``grep x; pip install y`` chains a
blocked write past a read-only prefix and must be inspected whole). VALUE/CONFIG
patterns are scanned against the RAW command so quoting cannot evade them
(``git -c "core.hooksPath=/dev/null"``); TOOL-INVOCATION patterns are scanned
against a quote-stripped copy so a blocked tool name inside a commit message or
grep argument does not false-block. The ``T3_ALLOW_REMOTE_DUMP=1`` defunct bypass
(#777) is denied first, even before the allowlist.

Extracted whole from ``hook_router`` (the #2384 Wave-2 router split, PR7) so the
dispatcher shrinks; the router re-exports :func:`handle_block_direct_commands`
into ``_HANDLERS`` unchanged, plus :func:`deny_match` (as ``_deny_match``, read by
the denylist tests) and :data:`BLOCKED_COMMANDS` (as ``_BLOCKED_COMMANDS``, the
combined denylist the BLUEPRINT / ship skill / merge-execution prose cite). The
deny routes through the router's shared ``emit_pretooluse_deny`` chokepoint
(back-imported lazily), so the ``_write_pretooluse_deny`` deny writer and the
repeated-denial circuit breaker stay in the router. A narrow targeted-command
gate — it denies only specific ``t3``-CLI-bypass commands, never arbitrary Bash —
so it is on the never-lockout allowlist.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib —
never Django / ``teatree.core``.
"""

import re
import sys
from pathlib import PurePosixPath

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("direct_command_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.direct_command_guard", sys.modules[__name__])

_REMOTE_DUMP_ENV_RE = re.compile(r"\bT3_ALLOW_REMOTE_DUMP\s*=\s*1\b")
_REMOTE_DUMP_DENY_REASON = (
    "BLOCKED: `T3_ALLOW_REMOTE_DUMP=1` is a removed, defunct bypass (#777) — "
    "setting it does nothing and signals an attempt to circumvent the safety gate. "
    "A fresh remote dump is available only via `t3 <overlay> db refresh --fresh-dump`, "
    "which requires an explicit interactive per-invocation human approval the agent "
    "cannot satisfy. Ask the user to run that command themselves."
)

# Commands that are legitimate t3 CLI invocations — never block these.
# `uv run t3 ...` is intentionally NOT whitelisted here: it is caught by the
# blocked-commands list below so agents switch to the globally-installed t3.
_T3_CMD_PREFIX_RE = re.compile(
    r"^(?:\w+=\S+\s+)*t3\s",
)

# Read-only commands that may mention infrastructure tools as arguments
# (e.g. grep for 'playwright', echo about manage.py) — never block these.
_READONLY_CMD_PREFIX_RE = re.compile(
    r"^(?:echo|printf|cat|grep|rg|awk|sed|head|tail|less|wc|file|#)",
)

# Forbidden command patterns → deny messages.  Each entry is
# (compiled regex matching the Bash command, human-readable deny reason).
# Patterns that match a VALUE or CONFIG TOKEN that can legitimately appear
# inside a quoted argument in a real bypass (e.g. ``git -c "core.hooksPath=x"``
# or ``git push -o "merge_request.merge_when_pipeline_succeeds"``).  These
# must be scanned against the RAW command so quoting cannot evade them.
_RAW_SCAN_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (
        # F3: ``git -c core.hooksPath=…`` redirects git's hooks directory,
        # silencing all hooks — semantically identical to ``--no-verify``.
        # The value (e.g. ``/dev/null``) can appear inside single- or
        # double-quoted args: ``git -c "core.hooksPath=/dev/null"`` is a real
        # bypass and must be caught against the raw command.
        re.compile(r"\bgit\b.*-c\s+['\"]?core\.hooksPath\s*=", re.IGNORECASE),
        (
            "BLOCKED: `git -c core.hooksPath=…` bypasses git hooks "
            "(equivalent to `--no-verify`) — fix the hook failure instead."
        ),
    ),
    (
        # F8: ``git push -o merge_request.merge_when_pipeline_succeeds`` schedules
        # a GitLab auto-merge, bypassing the FSM keystone transition
        # (``t3 <overlay> ticket merge``). The ``--push-option=`` long form is
        # equivalent.  The push-option value can appear quoted on the command
        # line, so scan raw.
        re.compile(
            r"\bgit\s+push\b.*"
            r"(?:-o\s+['\"]?merge_request\.merge_when_pipeline_succeeds"
            r"|--push-option=['\"]?merge_request\.merge_when_pipeline_succeeds)"
        ),
        (
            "BLOCKED: `git push -o merge_request.merge_when_pipeline_succeeds` "
            "schedules an auto-merge bypassing the FSM keystone — "
            "use `t3 <overlay> ticket merge` instead."
        ),
    ),
]

# Patterns that match a TOOL INVOCATION that, in any real command, appears
# unquoted at command position.  These are scanned against a quote-stripped
# copy of the command so a tool name that merely appears inside a quoted
# commit message / grep argument does not false-block.
_QUOTE_STRIPPED_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\.venv/bin/"),
        "BLOCKED: `.venv/bin/...` — use `uv run` instead so the resolved environment matches `pyproject.toml`.",
    ),
    (
        re.compile(r"manage\.py\s+runserver"),
        "BLOCKED: `manage.py runserver` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"manage\.py\s+migrate"),
        "BLOCKED: `manage.py migrate` — use `t3 <overlay> worktree provision` instead.",
    ),
    (
        re.compile(r"\bnx\s+serve\b"),
        "BLOCKED: `nx serve` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"\bdocker\s+compose\s+(?:up|start)\b"),
        "BLOCKED: `docker compose up/start` — use `t3 <overlay> worktree start` instead.",
    ),
    (
        re.compile(r"\b(?:createdb|dropdb)\b"),
        "BLOCKED: `createdb`/`dropdb` — use `t3 <overlay> db reset` instead.",
    ),
    (
        re.compile(r"\b(?:npx\s+)?playwright\s+test\b"),
        "BLOCKED: `playwright test` — use `t3 <overlay> e2e` instead.",
    ),
    (
        re.compile(r"\bnpm\s+run\b"),
        (
            "BLOCKED: `npm run` — use `t3 <overlay> run build-frontend` "
            "(rebuild dist) or `t3 <overlay> worktree start` (full stack) instead."
        ),
    ),
    (
        re.compile(r"\b(?:pipenv|pip)\s+install\b"),
        "BLOCKED: `pip/pipenv install` — use `t3 <overlay> worktree provision` instead.",
    ),
    (
        re.compile(r"\b(?:pg_restore|pg_dump)\b"),
        "BLOCKED: `pg_restore`/`pg_dump` — use `t3 <overlay> db refresh` instead.",
    ),
    (
        re.compile(r"\bdslr\s+(?:restore|import|snapshot|rename|export)\b"),
        (
            "BLOCKED: mutating `dslr` subcommand — use "
            "`t3 <overlay> db refresh --dslr-snapshot <name>` instead. "
            "Only `dslr list` and `dslr delete` are allowed."
        ),
    ),
    (
        re.compile(r"\bgit\s+\S+.*--no-verify\b"),
        "BLOCKED: `--no-verify` — fix the hook failure instead of bypassing it.",
    ),
    (
        re.compile(r"\bgit\s+\S+.*--no-gpg-sign\b"),
        "BLOCKED: `--no-gpg-sign` — do not bypass signing without explicit user approval.",
    ),
    # NOTE: ``gh pr merge`` / ``glab mr merge`` are NOT static-blocked here.
    # A pure regex cannot tell a teatree-managed repo (must use the keystone
    # `t3 <overlay> ticket merge` transition) from a lightweight repo with no
    # ticket/overlay FSM (which had no way to merge at all — a permanent
    # lockout). The cwd-aware ``handle_block_out_of_band_merge`` gate enforces
    # this with a managed-repo carve-out instead (#126).
    (
        re.compile(r"\bsafety\s+(?:check|scan)\b"),
        "BLOCKED: `safety` — use `pip-audit` instead (#1264; `uv audit` is preview-only).",
    ),
    (
        re.compile(r"\buv\s+run\s+(?:\S+\s+)*?t3(?:\s|$)"),
        (
            "BLOCKED: `uv run t3` — teatree is installed globally; call `t3` directly. "
            "If `t3` is missing on this machine, install teatree "
            "(`uv tool install --from git+https://github.com/souliane/teatree.git teatree` "
            "or `uv tool install --editable <teatree-repo>`)."
        ),
    ),
]

# Keep the combined list for any existing code that references _BLOCKED_COMMANDS
# directly (e.g. downstream tests that import it). Both partitions are included
# so the union is identical to the original list.
BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    *_RAW_SCAN_BLOCKED,
    *_QUOTE_STRIPPED_BLOCKED,
]


_SHELL_CHAIN_RE = re.compile(r"[;|`]|\$\(|&&|\|\|")
# Strip both single- and double-quoted literals for the tool-invocation scan so
# that a blocked tool name mentioned inside any quoted argument (e.g. a git
# commit message or a grep pattern) does not false-block the command.
# Value/config patterns (F3, F8) are scanned against the raw command instead,
# so stripping both quote styles here is safe.
_QUOTED_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _has_shell_chain(command: str) -> bool:
    """True if *command* contains a shell-chaining operator after the first token.

    Used by F6 fix: a command like ``grep '' /dev/null; blocked-cmd`` starts
    with a read-only prefix but chains a blocked command. The allowlist must
    not short-circuit when a chain operator is present.
    """
    return bool(_SHELL_CHAIN_RE.search(command))


# Leaders whose heredoc body is post/commit DATA (a PR/issue/MR body or a commit
# message), never executed — so a blocked-tool phrase inside such a heredoc is
# documentation, not an invocation, and must not trip the denylist. A heredoc fed
# to an INTERPRETER (``bash <<EOF docker compose up EOF``) is NOT in this set, so
# its body stays scanned and a real bypass piped to a shell still blocks.
_FORGE_HEREDOC_LEADERS: frozenset[str] = frozenset({"gh", "glab", "git"})

# A heredoc: ``<<['"]?DELIM['"]?`` on a command line, then a body up to a line
# that is just DELIM. ``body`` is the content between the intro line and the
# closing delimiter.
_HEREDOC_BODY_RE = re.compile(
    r"<<-?\s*['\"]?(?P<delim>\w+)['\"]?[^\n]*\n(?P<body>.*?)\n[ \t]*(?P=delim)\b",
    re.DOTALL,
)

# Command separators that end one command and begin the next.
_CMD_SEP_SPLIT_RE = re.compile(r"\|\||&&|[;|&\n]")

# A command substitution runs its inner command whatever leads the segment.
_SUBSTITUTION_RE = re.compile(r"\$\(|`")


def _heredoc_owner_leader(prefix: str) -> str:
    """Return the executable leading the command that introduces a heredoc.

    ``prefix`` is the command text up to the ``<<`` operator; a heredoc attaches
    to the command on its line, so the owner is the first non-env-assignment
    token of the last command segment in ``prefix`` (after the final
    ``;``/``|``/``&&``/``||``/newline). The basename is returned so a path-form
    leader (``/usr/bin/gh``) matches a bare ``gh``.
    """
    last_segment = _CMD_SEP_SPLIT_RE.split(prefix)[-1]
    for token in last_segment.split():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        return PurePosixPath(token).name
    return ""


def _strip_forge_heredoc_bodies(command: str) -> str:
    """Blank heredoc bodies fed to a ``gh``/``glab``/``git`` command.

    A heredoc feeding a forge/git posting command (``gh pr create --body-file -
    <<EOF … EOF``, ``git commit -F - <<EOF … EOF``) is the PR/issue/MR body or
    commit message — pure DATA the command never executes. A blocked-tool phrase
    inside it (``docker compose up`` documented in a PR body) is text, not an
    invocation, so it must not trip the denylist. Only forge/git-owned heredocs
    are blanked: a heredoc fed to an interpreter (``bash <<EOF docker compose up
    EOF``) keeps its body scanned, so a real bypass piped to a shell still blocks.
    """

    def blank(match: "re.Match[str]") -> str:
        if _heredoc_owner_leader(command[: match.start()]) not in _FORGE_HEREDOC_LEADERS:
            return match.group(0)
        body_start = match.start("body") - match.start()
        body_end = match.end("body") - match.start()
        whole = match.group(0)
        return whole[:body_start] + " " + whole[body_end:]

    return _HEREDOC_BODY_RE.sub(blank, command)


def _executing_segments(quote_stripped: str) -> str:
    """*quote_stripped* minus the segments a ``t3``/read-only leader owns (#3562).

    A segment led by ``grep``/``cat``/``t3`` READS a blocked tool's name as an
    argument; it cannot invoke it. Scanning it produced the Class-W false positive
    (``rg manage.py runserver src && t3 loop status`` denied for grepping the
    phrase), because F6 disables the leader allowlist for the WHOLE command as soon
    as a chain operator appears. Dropping only the read-only segments keeps every
    other segment scanned, so ``grep x; pip install y`` still denies on segment two.

    A segment carrying a command substitution keeps its scan: ``echo $(pip install
    x)`` runs the inner command despite the read-only leader.
    """
    kept = [
        segment
        for segment in _CMD_SEP_SPLIT_RE.split(quote_stripped)
        if _SUBSTITUTION_RE.search(segment) or not _leader_is_allowlisted(segment)
    ]
    return "\n".join(kept)


def _leader_is_allowlisted(segment: str) -> bool:
    leader = segment.lstrip()
    return bool(_T3_CMD_PREFIX_RE.match(leader) or _READONLY_CMD_PREFIX_RE.match(leader))


def deny_match(command: str) -> str | None:
    """Return a deny reason for *command*, or None if it should pass through."""
    # Checked FIRST — even before t3/read-only bypass — because agents must
    # never opt in to remote pg_dump regardless of the surrounding command.
    if _REMOTE_DUMP_ENV_RE.search(command):
        return _REMOTE_DUMP_DENY_REASON
    stripped = command.lstrip()
    # F6: only honor the readonly/t3 prefix allowlist when there is no shell
    # chaining operator in the command. ``grep x /dev/null; pip install y``
    # starts with a read-only prefix but chains a blocked write — the gate
    # must inspect the full command rather than short-circuiting on the prefix.
    if not _has_shell_chain(command) and (_T3_CMD_PREFIX_RE.match(stripped) or _READONLY_CMD_PREFIX_RE.match(stripped)):
        return None
    # A heredoc feeding a gh/glab/git posting command is the PR/commit BODY —
    # pure data. Blank those bodies before the denylist scans so a blocked-tool
    # phrase documented in a body (``docker compose up`` in a PR description) is
    # not read as an invocation; a heredoc fed to an interpreter keeps its body.
    scan_command = _strip_forge_heredoc_bodies(command)
    # Scan VALUE/CONFIG patterns against the (body-stripped) raw command so that
    # quoting the value (e.g. ``git -c "core.hooksPath=/dev/null"``) cannot evade
    # the gate — a real bypass is never inside a forge heredoc body.
    for pattern, reason in _RAW_SCAN_BLOCKED:
        if pattern.search(scan_command):
            return reason + " If `t3` fails, fix the CLI — do not work around it."
    # Scan TOOL-INVOCATION patterns against a quote-stripped copy so that a
    # blocked tool name that appears only inside a quoted commit message or grep
    # argument (e.g. ``git commit -m 'fix: handle pip install edge case'``) does
    # not false-block the command.  Real blocked invocations are unquoted and
    # still match the stripped target.
    quote_stripped = _QUOTED_LITERAL_RE.sub(" ", scan_command)
    for pattern, reason in _QUOTE_STRIPPED_BLOCKED:
        if pattern.search(_executing_segments(quote_stripped)):
            return reason + " If `t3` fails, fix the CLI — do not work around it."
    return None


def handle_block_direct_commands(data: dict) -> bool:
    """Block Bash commands that bypass the t3 CLI.

    Returns True when a deny was emitted (caller should stop the handler chain).
    The deny routes through the router's shared ``emit_pretooluse_deny`` chokepoint
    (back-imported lazily; the ``_write_pretooluse_deny`` writer + circuit breaker
    stay in the router).
    """
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return False
    reason = deny_match(command)
    if reason is None:
        return False
    return emit_pretooluse_deny(reason)
