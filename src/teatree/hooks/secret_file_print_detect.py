"""Detect a shell command that routes a secret-bearing source to stdout.

The pure matcher behind the PreToolUse secret-file-print gate (#2384 PR4), carried
in a :mod:`teatree.hooks` leaf so BOTH the cold PreToolUse subprocess (via
``hooks/scripts/secret_file_print_guard.py``) AND Lane B's shared hard-deny
registry refuse the SAME set. A command is a secret print when it would route a
credential/key/``.env`` file or a pass store to stdout:

* cat/head/tail of a known secret-bearing path;
* ``pass show …`` whose stdout is neither captured nor redirected;
* echo/printf of a pasted token literal (``glpat-``/``ghp_``/``xoxb-``/``sk-`` …).

Allowed (must NOT false-positive): a variable capture (``VAR=$(…)``), a file
redirect (``… > out``), env/header use (``curl -H "Token: $VAR"``), cat of an
ordinary file, and echo of prose that merely MENTIONS a secret path. Pure and
stdlib-only (regex + ``str`` ops), so it never raises on a shell-command string.
"""

import re

# A pipe with a downstream sink — ``head | tail`` etc. — has at least this many
# segments; fewer means no sink to consume the secret.
_MIN_PIPE_SEGMENTS = 2

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

_TOKEN_LITERAL_RE = re.compile(r"""(?:^|\s)(?:glpat[-_]|ghp_|gho_|xoxb-|xoxp-|sk-)\S+""")
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

_STDOUT_LEAK_DENY_REASON = (  # [skill-load-ok: souliane/teatree repo]
    "BLOCKED: this command would print a secret-bearing file or credential token "
    "to the transcript. Reading a secret into the transcript is irrecoverable — "
    "rotation is the only remedy. Instead, extract the value into a shell variable "
    "(`TOKEN=$(pass show …)`) and use it via env/header without printing it. "
    "Do NOT implement 'mask-then-print' — a masking regex is one edge case away "
    "from leaking. The gate's job is to keep the value off stdout entirely."
)


def _command_captures_or_redirects(command: str) -> bool:
    """Whether the command's stdout is captured or redirected (kept off the transcript)."""
    if _CAPTURE_RE.search(command):
        return True
    segments = command.split("|")
    if len(segments) < _MIN_PIPE_SEGMENTS:
        return False
    return not any(_RE_EMITTER_SINK_RE.match(segment) for segment in segments[1:])


def _echo_arg_is_token(command: str) -> bool:
    """Whether the echo/printf command carries a token literal (not just quoted prose)."""
    verb_and_arg = 2
    parts = command.split(None, 1)
    if len(parts) < verb_and_arg:
        return False
    arg = parts[1].strip()
    if _ECHO_SAFE_QUOTE_RE.match(arg):
        return bool(_TOKEN_LITERAL_RE.search(arg[1:-1]))
    return bool(_TOKEN_LITERAL_RE.search(command))


def is_secret_print(command: str) -> bool:  # [skill-load-ok: souliane/teatree repo]
    """Whether *command* would print a secret-bearing value to stdout."""
    if _command_captures_or_redirects(command):
        return False
    if _PRINT_CMDS_RE.match(command):
        return bool(_SECRET_PATHS_RE.search(command))
    if re.match(r"^\s*(?:echo|printf)\b", command):
        return _echo_arg_is_token(command)
    return bool(_PASS_SHOW_RE.match(command))


def secret_print_deny_reason(command: str) -> str | None:
    """Return the deny reason for a secret-print command, or ``None`` when allowed."""
    if not command or not is_secret_print(command):
        return None
    return _STDOUT_LEAK_DENY_REASON


__all__ = ["is_secret_print", "secret_print_deny_reason"]
