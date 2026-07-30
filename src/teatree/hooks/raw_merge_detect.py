"""Action-aware detection of a raw forge-merge invocation — the out-of-band-merge gate (#2387).

The PreToolUse gate (BLUEPRINT §17.1 invariant 8) blocks a raw ``gh pr merge`` /
``glab mr merge`` on a teatree-managed repo because it bypasses the FSM keystone
merge. The original matcher searched for the subcommand phrase as a SUBSTRING
anywhere in the Bash command text, so a command that merely *documents* the merge
command — a ``cat >> note.md <<EOF … gh pr merge … EOF`` heredoc, an
``echo "run gh pr merge"`` string, or a ``# gh pr merge`` comment — was wrongly
blocked (same content-not-action over-block class as #1415).

This detection is a STRICT tightening of that substring matcher, not a
re-scoping. It removes ONLY the provably-non-invocation false positives — a
heredoc body, a ``#`` comment, and a quoted-string operand — and otherwise errs
toward BLOCK: any plausible invocation of the merge subcommand fires.

A merge is an INVOCATION when the merge subcommand is the executed program of a
command segment. Before the command-position check the detector strips a leading
``NAME=val`` env-assignment run and a known wrapper prefix
(``command``/``time``/``nohup``/``exec``/``xargs``/``env``), matches the BASENAME
of a path-qualified program word (``/usr/bin/gh``), descends through shell
grouping/compound keywords (``(`` / ``{`` / ``if`` / ``then`` / …), and recurses
into command substitutions (``$(…)`` / backticks) so a merge invoked inside a
substitution still fires. Only a heredoc body (stripped), a comment (dropped by
the lexer), and a quoted-string operand (a non-command-position token) are
allowed through.
"""

import re

from teatree.hooks._shell_lexer import split_commands, tokenize

# A ``gh``/``glab api`` REST call — the out-of-band merge/mutation surface.
_GLAB_GH_API_RE = re.compile(r"\b(?:glab|gh)\s+api\b")
# The REST merge endpoint: ``(merge_requests|pulls)/<iid>/merge`` (GitLab + GitHub
# shapes). The iid is ``[^/\s]+`` -- ANY non-slash, non-space token -- not just a
# numeric ``\d+``: a shell-variable or brace-templated iid
# (``pulls/$PR/merge``, ``pulls/{id}/merge``, ``merge_requests/$IID/merge``)
# resolves to a real merge at run time, so the numeric-only pattern let it evade
# the out-of-band-merge hard-deny (#F7.8). Errs toward BLOCK, consistent with the
# fail-closed doctrine; a GET to the same endpoint is still allowed via the
# effective-method check.
_MERGE_ENDPOINT_RE = re.compile(r"(?:merge_requests|pulls)/[^/\s]+/merge\b")
# GitHub GraphQL merge-effecting mutations (each merges a PR / branch out of band).
_GRAPHQL_MERGE_MUTATION_RE = re.compile(r"(?:mergePullRequest|enablePullRequestAutoMerge|mergeBranch)\s*\(")
# The gh/glab HTTP-method flag, both empirically-valid forms: spaced/``=`` (``-X PUT``,
# ``--method=POST``) and the no-space pflag shorthand (``-XPUT``). Consumers flatten the
# two capture groups and keep last-wins effective-method semantics — the same classifier
# ``hook_router._effective_method_is_write`` encodes, carried self-contained here so the
# leaf stays importable by Lane B and the cold PreToolUse subprocess alike.
_METHOD_FLAG_RE = re.compile(r"(?:-X|--method)[\s=]+['\"]?([A-Za-z]+)\b|(?<=-X)([A-Za-z]+)\b")
_BODY_FLAG_RE = re.compile(r"(?:^|\s)(?:-f|--field|-F|--raw-field|--input|-d|--data)\b")

_RAW_MERGE_DENY_REASON = (
    "BLOCKED: raw `gh pr merge` / `glab mr merge` on a teatree-managed repo — "
    "an out-of-band merge bypasses the FSM coherence mechanism (ledger update, "
    "MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged). "
    "Use the sanctioned keystone transition `t3 <overlay> ticket merge <clear_id>` "
    "(BLUEPRINT §17.1 invariant 8 / §17.4). kill-switch: `t3 <overlay> gate raw-merge disable`."
)


def _effective_method_is_write(command: str) -> bool:
    """Whether a gh/glab REST command's EFFECTIVE HTTP method is a write (not GET).

    The LAST ``-X``/``--method`` value wins; with no method flag the forge defaults
    to POST when a body/field flag is present, else GET. A GET is the only read.
    """
    methods = [m.upper() for pair in _METHOD_FLAG_RE.findall(command) for m in pair if m]
    if methods:
        return methods[-1] != "GET"
    return bool(_BODY_FLAG_RE.search(command))


def is_raw_merge_api_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a ``.../<n>/merge`` endpoint.

    True only when the command targets a ``.../pulls/<n>/merge`` or
    ``.../merge_requests/<n>/merge`` endpoint AND its effective HTTP method is not
    GET (a GET reads merge status and must NOT be denied).
    """
    if not command or not _GLAB_GH_API_RE.search(command):
        return False
    if not _MERGE_ENDPOINT_RE.search(command):
        return False
    return _effective_method_is_write(command)


def invokes_graphql_merge_mutation(command: str) -> bool:
    """Whether *command* is a ``gh``/``glab api`` GraphQL merge-effecting mutation.

    A ``mergePullRequest`` / ``enablePullRequestAutoMerge`` / ``mergeBranch`` call
    has an unresolvable node-id target, so any occurrence in a forge-``api`` command
    is treated as a merge (fail-closed). A query moved out of argv (``-F query=@file``
    / ``--input``) is an accepted residual, matching the router gate.
    """
    if not command or not _GLAB_GH_API_RE.search(command):
        return False
    return bool(_GRAPHQL_MERGE_MUTATION_RE.search(command))


def raw_merge_deny_reason(command: str) -> str | None:
    """Return the raw-merge deny reason for *command*, or ``None`` when it is allowed.

    Fires on any of the three out-of-band merge vectors — the literal subcommand
    (``gh pr merge`` / ``glab mr merge``, action-aware), the REST-API merge write, or
    a GraphQL merge mutation. This is the PURE detector shared by
    :mod:`teatree.hooks.hard_deny_registry` (Lane B) and delegated to by the router's
    cwd-aware merge gate; the unmanaged-repo carve-out (#126) is hook_router context
    layered ON TOP of this detector, never part of it — Lane B is always jailed to a
    managed worktree, so it denies every raw merge unconditionally.
    """
    if not command:
        return None
    if (
        invokes_raw_merge_subcommand(command)
        or is_raw_merge_api_write(command)
        or invokes_graphql_merge_mutation(command)
    ):
        return _RAW_MERGE_DENY_REASON
    return None


# A heredoc body span: ``<<['"]?DELIM['"]?\n … \nDELIM``. Stripped before lexing
# so a body line that BEGINS with the merge phrase cannot land at a command
# position. Mirrors the shape used by ``_body_file_resolution._HEREDOC_RE``.
_HEREDOC_BODY_RE = re.compile(r"(<<-?\s*['\"]?\w+['\"]?\s*\n).*?(\n\s*\w+\b)", re.DOTALL)

# The two forge programs and the merge subcommand words that follow.
_MERGE_PROGRAMS: frozenset[str] = frozenset({"gh", "glab"})
_MERGE_SUBWORDS: dict[str, tuple[str, str]] = {"gh": ("pr", "merge"), "glab": ("mr", "merge")}

# A leading ``NAME=val`` env-assignment run (consumed before the program word).
_ENV_ASSIGN_RE = re.compile(r"^\w+=")

# Wrapper programs whose first non-flag operand is the real executed program.
_WRAPPER_PROGRAMS: frozenset[str] = frozenset({"command", "time", "nohup", "exec", "xargs", "env"})

# Shell grouping / compound keywords that PRECEDE a command word in a segment.
_COMPOUND_KEYWORDS: frozenset[str] = frozenset(
    {"(", ")", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "case", "esac", "!"},
)


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc body content, keeping the redirect head and the delimiter line."""
    return _HEREDOC_BODY_RE.sub(lambda m: m.group(1) + m.group(2), command)


def _command_substitution_bodies(command: str) -> list[str]:
    """Return the inner text of every ``$(…)`` and backtick command substitution.

    A merge invoked inside a substitution still executes, so each body is fed
    back through the detector. ``$(`` spans are matched by paren balance (so
    nested substitutions are captured whole); backtick spans run to the next
    unescaped backtick.
    """
    bodies: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(command[i + 2 : j - 1 if depth == 0 else n])
            i = j
            continue
        if command[i] == "`":
            j = i + 1
            while j < n and command[j] != "`":
                j += 2 if command[j] == "\\" else 1
            bodies.append(command[i + 1 : j])
            i = j + 1
            continue
        i += 1
    return bodies


def _program_words(segment_words: list[str]) -> list[str]:
    """Return the words from the program word onward, env/wrapper/compound prefixes stripped.

    Consumes a leading ``NAME=val`` env run, leading shell grouping/compound
    keywords, and one wrapper prefix (with that wrapper's own leading
    ``NAME=val`` args for ``env``). The first remaining word is the executed
    program; the basename test is applied by the caller.
    """
    index = 0
    consumed_wrapper = False
    while index < len(segment_words):
        word = segment_words[index]
        if _ENV_ASSIGN_RE.match(word) or word in _COMPOUND_KEYWORDS:
            index += 1
            continue
        if not consumed_wrapper and _basename(word) in _WRAPPER_PROGRAMS:
            consumed_wrapper = True
            index += 1
            continue
        break
    return segment_words[index:]


def _basename(word: str) -> str:
    """The final path component of a program word (``/usr/bin/gh`` → ``gh``)."""
    return word.rsplit("/", 1)[-1]


def _segment_invokes_merge(segment_words: list[str]) -> bool:
    """Whether a single command segment's executed program is a forge merge."""
    words = _program_words(segment_words)
    if not words:
        return False
    program = _basename(words[0])
    if program not in _MERGE_PROGRAMS:
        return False
    return tuple(words[1:3]) == _MERGE_SUBWORDS[program]


def invokes_raw_merge_subcommand(command: str) -> bool:
    """Whether *command* INVOKES ``gh pr merge`` / ``glab mr merge`` as an executed program.

    Errs toward BLOCK: fires on any plausible invocation (env-prefixed,
    wrapper-prefixed, path-qualified, grouped/compound, or inside a command
    substitution). Only a heredoc body, a ``#`` comment, and a quoted-string
    operand — provably-non-invocation text — pass through.
    """
    if not command:
        return False
    if any(_segment_invokes_merge(list(seg)) for seg in _segment_word_lists(_strip_heredoc_bodies(command))):
        return True
    return any(invokes_raw_merge_subcommand(body) for body in _command_substitution_bodies(command))


def _segment_word_lists(command: str) -> list[list[str]]:
    """Lex *command* into per-segment WORD-value lists (comments and quotes resolved)."""
    return [[token.value for token in segment] for segment in split_commands(tokenize(command))]


__all__ = [
    "invokes_graphql_merge_mutation",
    "invokes_raw_merge_subcommand",
    "is_raw_merge_api_write",
    "raw_merge_deny_reason",
]
