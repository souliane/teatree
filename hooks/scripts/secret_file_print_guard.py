"""Block Bash commands that route a secret-bearing source to stdout (#2384 PR4).

The privacy scan gates COMMITS, but nothing gates a command that echoes a secret
to the transcript — once it lands there, rotation is the only remedy. This gate
denies a Bash command that would print a known secret-bearing source to stdout:

* cat/head/tail of known secret-bearing paths (credential files, key files,
    ``.env`` files, pass stores);
* ``pass show …`` whose stdout is NOT captured or redirected to a file;
* echo/printf of a pasted token literal (glpat-/ghp_/gho_/xoxb-/xoxp-/sk-).

Allowed (must NOT false-positive): reading a value into a shell variable
(``VAR=$(…)``), piping / redirecting to a file (``… > out.txt``), using the
value via env or header (``curl -H "Token: $VAR"``), cat of ordinary non-secret
files, and echo of prose that merely MENTIONS a secret-file path. Fails OPEN on
any internal error — a gate bug must never wedge the agent (consistent with the
#1164 raw-review-post guard).

Extracted whole from ``hook_router`` (the #2384 Wave-2 router split, PR4) so the
dispatcher shrinks; the router re-exports :func:`handle_block_secret_file_print`
into ``_HANDLERS`` unchanged. The deny routes through the router's shared
``_fail_open_or_deny`` chokepoint (back-imported lazily), so the self-rescue
allowlist and the ``danger_gate_fail_open`` kill-switch apply uniformly and the
``emit_pretooluse_deny`` / ``_write_pretooluse_deny`` deny writer stays in the
router (the never-lockout contract).

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib —
never Django / ``teatree.core``. The shared spine helper ``_fail_open_or_deny``
stays in the router and is back-imported lazily inside the handler body — the
``hooks/scripts`` sibling back-import the import-direction fitness test permits
(it governs only the ``src/teatree/hooks`` leaves).
"""

import re
import sys

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("secret_file_print_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.secret_file_print_guard", sys.modules[__name__])

_SECRET_PATHS_RE = re.compile(  # [skill-load-ok: souliane/teatree repo]
    r"""(?x)
    (?:~|/root|/home/[^/\s]+|/Users/[^/\s]+|\$HOME|\$\{HOME\}|\$\{?HOME\}?)
    /(?:
        \.teatree\.toml
        | \.netrc
        | \.config/gh/hosts\.yml
        | (?:Library/Application\s+Support|\.config)/glab-cli/config\.yml
        | \.ssh/(?:id_[a-z0-9_]+|.*\.pem|.*\.key)
    )
    | (?:^|[\s/])(?:
        \.env(?!\.(?:example|sample|template|dist)\b)(?:\.[a-z]+)?
        | secrets?\.env
        | .*\.credentials?
        | .*\.pem
        | .*\.key
        | .*_account\.json
    )(?:\s|$|['")])
    """,
    re.IGNORECASE,
)

_TOKEN_LITERAL_RE = re.compile(
    r"""(?:^|\s)(?:glpat[-_]|ghp_|gho_|xoxb-|xoxp-|sk-)\S+""",
)

_PRINT_CMDS_RE = re.compile(r"^\s*(?:cat|head|tail)\b")

_PASS_SHOW_RE = re.compile(r"^\s*pass\s+show\b")

_CAPTURE_RE = re.compile(  # [skill-load-ok: souliane/teatree repo]
    r"""
    \$\(            # subshell capture: $(…)
    | >\s*\S+       # stdout redirect to a file or /dev/null
    """,
    re.VERBOSE,
)

_RE_EMITTER_SINK_RE = re.compile(r"^\s*(?:cat|less|more|tee|grep|head|tail)\b")

_ECHO_SAFE_QUOTE_RE = re.compile(r"""^(?:'[^']*'|"[^"]*")$""")

# [skill-load-ok: souliane/teatree repo]
_CREDENTIAL_PRINT_BLOCK_MSG = (
    "BLOCKED: this command would print a secret-bearing file or credential token "
    "to the transcript. Reading a secret into the transcript is irrecoverable — "
    "rotation is the only remedy. Instead, extract the value into a shell variable "
    "(`TOKEN=$(pass show …)`) and use it via env/header without printing it. "
    "Do NOT implement 'mask-then-print' — a masking regex is one edge case away "
    "from leaking. The gate's job is to keep the value off stdout entirely."
)


def _command_captures_or_redirects(command: str) -> bool:
    """Return True when the command's stdout is captured or redirected, not printed.

    A variable-assignment prefix (``VAR=$(…)`` or ``export VAR=$(…)``) or a
    stdout redirect (``> file``) keeps the secret off the transcript. A pipe
    is a capture only when its sink consumes the value — a sink that re-emits
    to the transcript (``cat`` / ``less`` / ``more`` / ``tee`` / ``grep`` /
    ``head`` / ``tail``, incl. ``tee /dev/tty``) still displays the secret and
    is NOT a capture. A plain ``pass show x`` with no such construct prints.
    """
    if _CAPTURE_RE.search(command):
        return True
    segments = command.split("|")
    if len(segments) < 2:  # noqa: PLR2004
        return False
    return not any(_RE_EMITTER_SINK_RE.match(segment) for segment in segments[1:])


def _echo_arg_is_token(command: str) -> bool:
    """Return True when the echo/printf command carries a token literal.

    Prose strings inside quotes that merely MENTION a secret path are not
    treated as token prints — they contain no token literal. A fully-quoted
    arg whose CONTENT is itself a token literal still lands on the transcript,
    so it is treated as a token. Only quoted prose (no token shape) passes.
    """
    parts = command.split(None, 1)
    if len(parts) < 2:  # noqa: PLR2004
        return False
    arg = parts[1].strip()
    if _ECHO_SAFE_QUOTE_RE.match(arg):
        return bool(_TOKEN_LITERAL_RE.search(arg[1:-1]))
    return bool(_TOKEN_LITERAL_RE.search(command))


def _is_secret_print(command: str) -> bool:  # [skill-load-ok: souliane/teatree repo]
    """Whether *command* would print a secret-bearing value to stdout."""
    try:
        if _command_captures_or_redirects(command):
            return False
        if _PRINT_CMDS_RE.match(command):
            return bool(_SECRET_PATHS_RE.search(command))
        if re.match(r"^\s*(?:echo|printf)\b", command):
            return _echo_arg_is_token(command)
        return bool(_PASS_SHOW_RE.match(command))
    except Exception:  # noqa: BLE001
        return False


def handle_block_secret_file_print(data: dict) -> bool:
    """Deny a Bash command that would print a secret-bearing file or token to stdout.

    Blocks cat/head/tail of credential files, ``pass show`` without redirection,
    and echo/printf of pasted token literals. Commands that capture the value
    into a variable or redirect to a file pass through. Returns True when a
    deny was emitted (caller stops the handler chain).

    The deny routes through the router's shared ``_fail_open_or_deny`` chokepoint
    (back-imported lazily), giving the always-allowed self-rescue commands and the
    master ``danger_gate_fail_open`` kill-switch for free (the never-lockout
    contract); the ``emit_pretooluse_deny`` / ``_write_pretooluse_deny`` writer
    stays in the router.
    """
    from hook_router import _fail_open_or_deny  # noqa: PLC0415, PLC2701

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _is_secret_print(command):
        return False
    return _fail_open_or_deny(data, _CREDENTIAL_PRINT_BLOCK_MSG)
