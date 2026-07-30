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
ordinary file, and echo of prose that merely MENTIONS a secret path.

The command is decomposed with the shared quote-accurate shell lexer
(:mod:`teatree.hooks._shell_lexer`) into per-STATEMENT pipelines, so the
print-verb, the capture/redirect, and the pipe sink are evaluated on the segment
that actually produces the secret -- never on the whole command. Hand-rolled
whole-command matching false-NEGATIVED two shapes the lexer closes: a redirect on
an UNRELATED segment (``cat ~/.ssh/id_rsa; echo ok > /dev/null``) suppressed the
leak, and a print verb NOT at the whole-command start (``true; cat ~/.netrc``)
was never seen.
"""

import re

from teatree.hooks._shell_lexer import TokenKind, tokenize

_SECRET_PATHS_RE = re.compile(
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

# Verbs that route their file operand(s) to stdout, and the pass-store reader.
_PRINT_VERBS = frozenset({"cat", "head", "tail"})
_ECHO_VERBS = frozenset({"echo", "printf"})

# A downstream pipe stage that itself re-emits its stdin to stdout, so the secret
# still reaches the transcript. A non-re-emitting sink (``wc``, ``gpg``, ``base64
# -d``) consumes the secret, keeping it off stdout.
_RE_EMITTER_SINKS = frozenset({"cat", "less", "more", "tee", "grep", "head", "tail"})

# A producer stage needs at least the verb plus one operand (the token literal
# for echo/printf, ``show`` for pass) before it can emit a secret.
_VERB_PLUS_OPERAND = 2

# Statement separators — a ``|`` stays WITHIN one statement (it connects the
# producer's stdout to the next stage); these END the statement.
_STATEMENT_SEPARATORS = frozenset({";", "&&", "||", "&", "\n"})

_STDOUT_REDIRECT_PREFIXES = (">", "1>", "&>")

_STDOUT_LEAK_DENY_REASON = (
    "BLOCKED: this command would print a secret-bearing file or credential token "
    "to the transcript. Reading a secret into the transcript is irrecoverable — "
    "rotation is the only remedy. Instead, extract the value into a shell variable "
    "(`TOKEN=$(pass show …)`) and use it via env/header without printing it. "
    "Do NOT implement 'mask-then-print' — a masking regex is one edge case away "
    "from leaking. The gate's job is to keep the value off stdout entirely."
)


def _pipelines(command: str) -> list[list[list[str]]]:
    """Group *command* into statements → pipeline stages → decoded word lists.

    Statements split on ``;``/``&&``/``||``/``&``/newline; a ``|`` splits stages
    WITHIN one statement. Word VALUES are shell-decoded (quotes/escapes resolved)
    so a separator inside a quoted string never splits a statement.
    """
    statements: list[list[list[str]]] = []
    stages: list[list[str]] = []
    words: list[str] = []
    for tok in tokenize(command):
        if tok.kind is TokenKind.OP and tok.value in _STATEMENT_SEPARATORS:
            if words:
                stages.append(words)
                words = []
            if stages:
                statements.append(stages)
                stages = []
        elif tok.kind is TokenKind.OP and tok.value == "|":
            if words:
                stages.append(words)
                words = []
        else:
            words.append(tok.value)
    if words:
        stages.append(words)
    if stages:
        statements.append(stages)
    return statements


def _stage_redirects_stdout(words: list[str]) -> bool:
    """Whether a stage redirects its OWN stdout to a file (keeping it off the transcript).

    Only stdout redirects (``>``/``>>``/``1>``/``&>``) count — a ``2>`` stderr
    redirect leaves stdout on the transcript, so it does NOT capture the secret.
    """
    return any(word.startswith(_STDOUT_REDIRECT_PREFIXES) for word in words)


def _stage_reads_secret(words: list[str]) -> bool:
    """Whether the producer stage would emit a secret to its stdout."""
    if not words:
        return False
    verb = words[0]
    if verb in _PRINT_VERBS:
        return bool(_SECRET_PATHS_RE.search(" ".join(words)))
    if verb in _ECHO_VERBS:
        return len(words) >= _VERB_PLUS_OPERAND and bool(_TOKEN_LITERAL_RE.search(" ".join(words[1:])))
    return verb == "pass" and len(words) >= _VERB_PLUS_OPERAND and words[1] == "show"


def _statement_prints_secret(stages: list[list[str]]) -> bool:
    """Whether a statement's producer stage prints a secret that reaches the transcript."""
    producer = stages[0]
    if not _stage_reads_secret(producer) or _stage_redirects_stdout(producer):
        return False
    downstream = stages[1:]
    if not downstream:
        return True
    # The secret still displays iff SOME downstream stage re-emits to stdout; a
    # pipeline whose sinks all consume (``| gpg``, ``| wc``) keeps it off stdout.
    return any(bool(stage) and stage[0] in _RE_EMITTER_SINKS for stage in downstream)


def is_secret_print(command: str) -> bool:
    """Whether *command* would print a secret-bearing value to stdout."""
    return any(_statement_prints_secret(stages) for stages in _pipelines(command))


def secret_print_deny_reason(command: str) -> str | None:
    """Return the deny reason for a secret-print command, or ``None`` when allowed."""
    if not command or not is_secret_print(command):
        return None
    return _STDOUT_LEAK_DENY_REASON


__all__ = ["is_secret_print", "secret_print_deny_reason"]
