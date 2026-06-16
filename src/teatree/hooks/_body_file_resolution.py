r"""Body-file flag resolution for the pre-publish gates.

Split out of :mod:`teatree.hooks._command_parser` to keep that module under
the module-health LOC cap. This module owns one concern: resolving the body
a ``-F``/``--file``/``--body-file`` flag points at, including the cold-hook
fallback where the harness cwd has reset away from the worktree.

A ``git commit -F <relpath>`` body file is read with the cold PreToolUse hook
subprocess's cwd, which has often reset away from the worktree, so a relative
``-F`` path (or an absolute one the cold cwd cannot reach) is unreadable as
given. :func:`commit_body_file_base` resolves the dir whose repo the commit
LANDS in (the command's own ``cd``/``-C``/``--git-dir``), and
:func:`append_file_payload` retries the path against it — so the gate scans
the real body and the private-repo carve-out can downgrade it instead of
fail-closing on an unread body. A body file that exists nowhere still fails
closed, and a PUBLIC-repo body file still hard-blocks.

The matching primitives (``FAIL_CLOSED_SENTINEL``, ``_read_file_arg``,
``_attached_value``) stay in :mod:`_command_parser`; this module imports them
one-directionally. ``_command_parser`` calls back into here via a lazy import
(at call time, not module load) so no cycle forms.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import (
    FAIL_CLOSED_SENTINEL,
    UNAVAILABLE_BODY_SOURCE_SENTINEL,
    attached_value,
    read_file_arg,
)
from teatree.hooks._shell_lexer import Token, TokenKind, split_commands

# Long options that point at a FILE whose content we should read. If the
# file is missing or unreadable the parser appends the fail-closed sentinel.
_BODY_FILE_FLAG_NAMES: Final[frozenset[str]] = frozenset({"--body-file", "--description-file", "--file"})

# A body value that IS exactly a ``$(cat <path>)`` command substitution. Agents
# pass a body inline as ``--description "$(cat <path>)"`` / ``--body "$(cat
# <path>)"``; the lexer keeps the whole quoted value as ONE token with the
# substitution UNEXPANDED, so the gate would scan the literal ``$(cat ...)``
# string -- rejecting a clean file and missing a banned term inside it. The
# path is read so the scan runs against the ACTUAL body. Backticks (``$(cat …)``
# only -- the modern form) and a single optional ``-- `` are tolerated; the path
# may be quoted.
_CAT_SUBST_RE: Final[re.Pattern[str]] = re.compile(
    r"^\$\(\s*cat\s+(?:--\s+)?(?P<path>'[^']+'|\"[^\"]+\"|\S+)\s*\)$",
)

# A body value that IS exactly a single shell-variable reference (``$VAR`` or
# ``${VAR}``). Resolved best-effort from the hook subprocess's environment (it
# inherits the agent's env, the same channel the ``ALLOW_BANNED_TERM`` override
# reaches the gate through). An absent variable is genuinely unresolvable and
# fails closed.
_VAR_REF_RE: Final[re.Pattern[str]] = re.compile(r"^\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}?$")

# A whole-value ``$VAR`` / ``${VAR}`` reference anchored INSIDE a double-quote
# span (``"$VAR"``) -- the live form the env resolver reads. A single-quoted
# ``'$VAR'`` is inert literal text bash never expands, so it must NOT be env
# resolved (the ``$VAR`` is the published body itself, e.g. documenting a flag).
_DOUBLE_QUOTED_VAR_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^\"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\"$",
)


def _raw_substitution_is_live(raw: str) -> bool:
    """Return True iff a ``$(...)`` in ``raw`` sits OUTSIDE a single-quoted span.

    A command substitution is expanded by bash only when it is unquoted or
    inside DOUBLE quotes; inside SINGLE quotes (``'...$(x)...'``) it is inert
    literal text bash passes verbatim, so the gate already holds the real body
    in the decoded value and can scan it. This walks the verbatim source span
    tracking single-quote state (a single-quoted region runs to the next ``'``;
    bash has no single-quote escape) and reports True the moment a ``$(`` opens
    while NOT inside a single-quoted region -- the live case the gate cannot
    read before the command runs, so it must fail closed.

    ``raw`` defaults to empty for in-process callers that do not carry a source
    span; an empty/absent ``raw`` is treated as live (conservative -- the gate
    keeps failing closed on an embedded ``$(...)`` it cannot prove inert).
    """
    if not raw:
        return True
    in_single = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "'":
            in_single = not in_single
            i += 1
            continue
        if not in_single and ch == "$" and i + 1 < n and raw[i + 1] == "(":
            return True
        i += 1
    return False


def resolve_inline_body_value(value: str, base: Path | None, raw: str = "") -> str:
    """Resolve a ``--description``/``--body`` value's indirection to the real body.

    Three forms are resolved so the banned-terms / quote scan runs against the
    ACTUAL published body rather than an unexpanded shell token:

    - ``$(cat <path>)`` -- the file content (read via :func:`read_file_arg`,
        ``base``-relative fallback for the cold-hook reset cwd). An unreadable
        path yields the fail-closed sentinel.
    - ``$VAR`` / ``${VAR}`` -- the environment variable's value when present in
        the hook subprocess env; absent yields the UNAVAILABLE-body-source
        sentinel (the value does not exist before the command runs, so the gate
        renders the actionable "write the body to an absolute file" message,
        #2369). Only the DOUBLE-quoted (``"$VAR"``) live form env-resolves -- a
        single-quoted ``'$VAR'`` is inert literal text bash never expands, so it
        is the published body and is scanned verbatim.
    - anything else -- returned verbatim (a normal inline body).

    A value that STILL carries an embedded ``$(...)`` command-substitution
    marker the single-form matchers above did not fully resolve is fail-closed
    ONLY when that substitution is LIVE -- i.e. its source span (``raw``) shows
    the ``$(`` sitting outside any single-quoted region, so bash WOULD expand it
    and the gate cannot see the real content (a mixed ``"prefix $(cat x)"``). A
    ``$(...)`` that sits INSIDE a single-quoted span (``'... $(date) ...'``,
    ``git commit -m 'ran $(date)'``) is inert literal text bash passes verbatim:
    the body is fully present in ``value`` and is SCANNED, not blocked. Without a
    source span (``raw`` empty) an embedded ``$(`` stays fail-closed --
    conservative, since the gate cannot prove it inert. Resolution is never a
    bypass: a live ``$(...)`` source the gate cannot read always fails closed.

    A backtick is NOT a fail-closed trigger. The extracted value is a literal
    argv element the gate only SCANS (never re-feeds to a shell), so a markdown
    inline-code span (a function name / flag / path in backticks, the common
    case in real PR/issue bodies) is inert data fully present in the value and
    fully scanned -- blocking on it was a pure false positive that forced
    ``--body-file``/heredoc workarounds.
    """
    cat_match = _CAT_SUBST_RE.match(value)
    if cat_match is not None and _raw_substitution_is_live(raw):
        path = cat_match.group("path").strip("'\"")
        content = read_file_arg(path, base)
        return content if content is not None else FAIL_CLOSED_SENTINEL
    var_match = _VAR_REF_RE.match(value)
    if var_match is not None and (not raw or _DOUBLE_QUOTED_VAR_REF_RE.match(raw)):
        resolved = os.environ.get(var_match.group("name"))
        return resolved if resolved is not None else UNAVAILABLE_BODY_SOURCE_SENTINEL
    if "$(" in value and _raw_substitution_is_live(raw):
        return FAIL_CLOSED_SENTINEL
    return value


_HEREDOC_RE: Final[re.Pattern[str]] = re.compile(
    r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b",
    re.DOTALL,
)

# A redirect (``> path`` / ``>| path`` / ``>> path``) that writes a heredoc
# body to a file, e.g. ``cat > /tmp/msg.txt <<'EOF' … EOF``. The common agent
# idiom is to write a body to a temp file then ``git commit -F /tmp/msg.txt`` /
# ``gh ... --body-file /tmp/msg.txt`` -- at PreToolUse scan time that file does
# NOT exist yet (the hook runs BEFORE the command), so the only place the body
# lives is the in-command heredoc. This regex pairs the redirect target path
# with the heredoc delimiter so a later ``-F``/``--body-file <path>`` reference
# resolves to the body the command is about to write there (#126).
_HEREDOC_TO_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r">{1,2}\|?\s*(?P<path>'[^']+'|\"[^\"]+\"|\S+)\s+<<\s*['\"]?(?P<delim>\w+)['\"]?\s*\n(?P<body>.*?)\n(?P=delim)\b",
    re.DOTALL,
)

# Commands whose operands ARE the body content when redirected to a file
# (an agent's two idioms for materialising a body temp file before a post).
_REDIRECT_WRITER_COMMANDS: Final[frozenset[str]] = frozenset({"printf", "echo"})
# Output-redirect operator prefixes, longest-first so ``>>`` precedes ``>``;
# matched as a prefix to also catch the unspaced glued ``>$f`` lexer token.
_REDIRECT_OPERATOR_PREFIXES: Final[tuple[str, ...]] = (">>", ">|", ">")


def _heredoc_file_bodies(command: str) -> dict[str, str]:
    """Map each ``> path <<EOF … EOF`` redirect target to its heredoc body.

    Resolves the agent idiom of writing a body to a temp file and then
    referencing it via ``git commit -F <path>`` / ``--body-file <path>``. The
    path is normalised (surrounding quotes stripped) so a quoted redirect target
    matches the later bare reference (#126).
    """
    bodies: dict[str, str] = {}
    for match in _HEREDOC_TO_FILE_RE.finditer(command):
        bodies[match.group("path").strip("'\"")] = match.group("body")
    return bodies


def _split_redirect_token(word: str) -> str | None:
    """Return the redirect target if ``word`` is/begins a write redirect, else None.

    A bare operator (``>``/``>>``/``>|``) returns ``""`` — the target is the
    NEXT word. An unspaced glued form (``>$f``, ``>/tmp/x``) returns the target
    suffix. A word that does not start with a redirect operator returns ``None``.
    """
    for prefix in _REDIRECT_OPERATOR_PREFIXES:
        if word.startswith(prefix):
            return word[len(prefix) :]
    return None


def _redirect_target_and_operands(words: list[str]) -> tuple[str, list[str]]:
    """Return the write-redirect target token and the writer operands preceding it.

    Returns ``("", [])`` when the segment carries no output redirect. The target
    is the glued suffix of an unspaced ``>$f`` token, or the next word after a
    bare ``>`` operator. Operands are every WORD between the command name and the
    redirect (the body content the writer emits).
    """
    for i in range(1, len(words)):
        suffix = _split_redirect_token(words[i])
        if suffix is None:
            continue
        operands = words[1:i]
        if suffix:
            return suffix, operands
        if i + 1 < len(words):
            return words[i + 1], operands
        return "", operands
    return "", []


def _redirect_written_bodies(tokens: list[Token]) -> dict[str, str]:
    r"""Map each ``printf``/``echo`` ``> path`` redirect target token to its body.

    Resolves the body-via-indirection idiom the heredoc map misses: an agent
    materialises a body into a temp file with ``printf``/``echo`` and then posts
    it with ``--body-file <path>``/``-F <path>`` in the SAME command
    (``f=$(mktemp); printf '%s' 'text' > "$f"; gh ... --body-file "$f"``). At
    PreToolUse scan time the file does NOT exist yet, so the only place the body
    lives is the writer's own operands.

    The map is keyed by the **lexer token value** of the redirect target so a
    shell-variable / command-substitution path (``$f``, ``$(mktemp)``) pairs
    with the textually-identical ``--body-file`` argument token even though
    neither is expanded at scan time. A literal path keys by its own value.
    Both the spaced (``> "$f"``) and unspaced (``>"$f"``) spellings are handled.

    Conservative by construction: a redirect with no preceding writer operands
    contributes no entry, so a genuinely-unresolvable target still fails closed.
    """
    bodies: dict[str, str] = {}
    for segment in split_commands(tokens):
        words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
        if not words or words[0] not in _REDIRECT_WRITER_COMMANDS:
            continue
        target, operands = _redirect_target_and_operands(words)
        if target and operands:
            bodies[target] = " ".join(operands)
    return bodies


def heredoc_files_map(command: str, tokens: list[Token]) -> dict[str, str]:
    """Map every in-command body-file target (heredoc or redirect) to its body."""
    return {**_heredoc_file_bodies(command), **_redirect_written_bodies(tokens)}


def unredirected_heredoc_bodies(command: str) -> list[str]:
    """Return heredoc bodies fed to a CONSUMER, not redirected to a file.

    A bare ``<<EOF`` / ``--body-file - <<EOF`` heredoc pipes its body straight
    into the command (stdin, or a ``$(cat <<EOF)`` substitution), so it is part
    of the published payload and must be scanned. A ``> path <<EOF`` heredoc
    writes to a file resolved by path-pairing through the body-file flag
    (:func:`_heredoc_file_bodies`), so emitting it blanket would scan an unposted
    scratch body as if published and double-count a posted one; it is excluded
    here when its body span sits inside a file-redirect match.
    """
    file_spans = [m.span("body") for m in _HEREDOC_TO_FILE_RE.finditer(command)]
    return [
        m.group(2)
        for m in _HEREDOC_RE.finditer(command)
        if not any(fs <= m.start(2) and m.end(2) <= fe for fs, fe in file_spans)
    ]


@dataclass(frozen=True)
class BodyFileContext:
    """Resolution context for ``-F``/``--file``/``--body-file`` body files.

    Groups the three settings that flow together through the body-file
    walkers: the in-command ``heredoc_files`` map (a body written earlier in
    the SAME command — a ``> path <<EOF`` heredoc or a ``printf``/``echo >
    path`` redirect — keyed by the redirect-target token), the ``base`` dir a
    relative body file is retried against (the commit's repo dir), and
    ``fail_closed_body_file`` (what an UNREADABLE ``gh``/``glab`` body file
    does — the git ``-F`` commit-message path always fails closed regardless).
    """

    heredoc_files: dict[str, str]
    fail_closed_body_file: bool
    base: Path | None = None


def commit_body_file_base(command: str, cwd: Path | None = None) -> Path | None:
    """Return the dir to resolve a ``git commit -F <relpath>`` body against.

    The base is the dir whose repo the commit LANDS in — the command's own
    leading ``cd``/``pushd`` plus ``-C``/``--git-dir`` directives, resolved
    by :func:`_commit_repo_dir.resolve_commit_dir` (a RELATIVE ``-C``/``cd``
    target anchored on the ambient ``cwd``, mirroring the carve-out, so a
    sub-agent's ``git -C ../worktree`` body file resolves against the dir the
    agent ran in, not the cold hook's process cwd) and walked up to the
    enclosing repo root by :func:`_commit_repo_dir.git_root_for_dir`. ``None``
    when the command names no commit dir (a plain ``git commit`` with no
    ``cwd`` whose body file is then resolved against the process cwd only) or
    when the dir is the fail-closed sentinel (a ``-C`` value the gate cannot
    pin down statically).
    """
    from teatree.hooks import _commit_repo_dir  # noqa: PLC0415

    # A plain ``git commit`` names no dir; keep the historical ``None`` so the
    # caller's own ``cwd`` fallback governs (anchoring only changes a command
    # that DOES name a relative ``cd``/``-C``/``--git-dir`` target).
    if _commit_repo_dir.effective_repo_dir(command) is None:
        return None
    commit_dir = _commit_repo_dir.resolve_commit_dir(command, cwd)
    if commit_dir is None or commit_dir == _commit_repo_dir.UNRESOLVABLE_REPO_DIR:
        return None
    return _commit_repo_dir.git_root_for_dir(Path(commit_dir))


def command_body_file_base(command: str) -> Path | None:
    """Return the working dir a non-git command's ``--body-file`` resolves against.

    A ``cd <dir> && gh pr create --body-file <relpath>`` body file is resolved
    with the cold PreToolUse hook's cwd, which has reset away from the worktree,
    so the relative path is unreadable as given — the gate would fail closed and
    block a clean post. The command's own leading ``cd``/``pushd`` dir
    (:func:`_commit_repo_dir.leading_cd_dir`) is the dir the forge command would
    actually run in, so resolving the body file against it lets the gate scan the
    real body. ``None`` when the command has no leading ``cd``.
    """
    from teatree.hooks._commit_repo_dir import leading_cd_dir  # noqa: PLC0415

    cd_dir = leading_cd_dir(command)
    return Path(cd_dir) if cd_dir is not None else None


@dataclass(frozen=True)
class _ShortFileFlag:
    """Resolved value of a short ``-F`` body-file reference, with its token span.

    ``path`` is the file the ``-F`` points at; ``consumed`` is how many tokens
    the flag occupied (``2`` for the space-separated ``-F <path>`` form, ``1``
    for the attached ``-F<path>`` form). ``fail_closed`` is the policy for an
    unreadable path — ``git``'s commit-message ``-F`` always fails closed, while
    ``gh``/``glab``'s body-file ``-F`` follows the destination-aware policy.
    ``None`` is returned when the ``-F`` at this position is not a body-file
    reference (so the caller advances by one and lets another walker handle it).
    """

    path: str
    consumed: int
    fail_closed: bool


def _short_f_body_file(leader: str, words: list[str], i: int, *, fail_closed: bool) -> _ShortFileFlag | None:
    """Resolve the file a short ``-F`` at ``words[i]`` references, or ``None``.

    The short ``-F`` is overloaded across leaders:

    - ``git`` -- ALWAYS a file (the ``git commit -F`` message file), regardless
        of the value, and always fails closed when unreadable (#1207).
    - ``gh`` / ``glab`` -- the documented short form of ``--body-file`` on
        ``issue/pr create|comment`` etc., but ``-F name=value`` on ``api`` is a
        field assignment. The two are disambiguated by VALUE: a ``=``-free token
        is a body-file path; a ``name=value`` token is left to
        :func:`_command_parser._walk_api_fields` (returns ``None`` here).
    - any other leader -- never a body-file ``-F`` (returns ``None``).

    Both the space-separated (``-F <path>``) and attached (``-F<path>``) spellings
    are recognised.
    """
    word = words[i]
    is_git = leader == "git"
    is_gh_glab = leader in {"gh", "glab"}
    if not (is_git or is_gh_glab):
        return None
    flag_fail_closed = True if is_git else fail_closed
    if word == "-F" and i + 1 < len(words):
        nxt = words[i + 1]
        if is_git or "=" not in nxt:
            return _ShortFileFlag(path=nxt, consumed=2, fail_closed=flag_fail_closed)
        return None
    attached = attached_value(word, "-F")
    if attached is not None and (is_git or "=" not in attached):
        return _ShortFileFlag(path=attached, consumed=1, fail_closed=flag_fail_closed)
    return None


def walk_body_file_flags(words: list[str], payloads: list[str], *, leader: str, ctx: BodyFileContext) -> None:
    """Extract ``--body-file``/``--file``/``-F`` style file payloads.

    The long ``--body-file`` / ``--description-file`` / ``--file`` forms apply to
    every leader. The short ``-F`` form is leader-scoped by
    :func:`_short_f_body_file`: ``git``'s ``-F`` is always the commit-message
    file; ``gh``/``glab``'s ``-F`` is the documented ``--body-file`` short form
    when its value is ``=``-free, otherwise it is an ``api`` field assignment
    that :func:`_command_parser._walk_api_fields` handles (#1812). The
    resolution context (heredoc map, repo-dir base, fail-closed policy) is
    carried by :class:`BodyFileContext`.
    """
    fail_closed = ctx.fail_closed_body_file
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _BODY_FILE_FLAG_NAMES and i + 1 < n:
            _append_file_payload(words[i + 1], payloads, ctx, fail_closed=fail_closed)
            i += 2
            continue
        attached: str | None = None
        for flag in _BODY_FILE_FLAG_NAMES:
            attached = attached_value(word, flag + "=")
            if attached is not None:
                _append_file_payload(attached, payloads, ctx, fail_closed=fail_closed)
                break
        if attached is not None:
            i += 1
            continue
        short = _short_f_body_file(leader, words, i, fail_closed=fail_closed)
        if short is not None:
            _append_file_payload(short.path, payloads, ctx, fail_closed=short.fail_closed)
            i += short.consumed
            continue
        i += 1


def _append_file_payload(path: str, payloads: list[str], ctx: BodyFileContext, *, fail_closed: bool) -> None:
    """Append the body referenced by a ``-F``/``--file``/``--body-file`` path.

    Resolution order: the on-disk file (as-is, then relative to ``ctx.base`` --
    the commit's repo dir), then an in-command body written to that path — a
    ``cat > path <<EOF … EOF`` heredoc or a ``printf``/``echo > path`` redirect
    — then the ``fail_closed`` branch. The in-command fallback closes the #126
    false positive where a body written to a temp file and posted via
    ``--body-file``/``-F`` in the same command was unreadable at PreToolUse
    scan time (the hook runs BEFORE the file is created); the lookup key is the
    raw ``--body-file`` argument token, so a ``$f`` / ``$(mktemp)`` path matches
    the textually-identical redirect target even though neither is expanded.
    The ``ctx.base`` fallback closes the cold-hook false positive where the
    harness cwd has reset away from the worktree, so a ``git -C <worktree>
    commit -F <relpath>`` body file is unreadable from the cwd yet readable from
    the commit's own repo dir.

    ``fail_closed`` selects what an unresolvable path does. ``True`` appends
    the fail-closed sentinel: the ``git commit -F <path>`` commit-message path
    always uses it (#1207), as does a ``gh``/``glab`` body file for the
    destination-aware banned-terms / bare-reference scanners, so a PUBLIC post
    whose body the gate cannot read hard-blocks rather than slip through unread
    (a destination-internal post is skipped before the payload is scanned, so
    the sentinel never over-blocks it). ``False`` appends NOTHING — the quote
    scanner keeps a drafted-but-absent ``gh``/``glab`` body file as
    "needs-inline", not a fail-closed HIGH (#126).
    """
    content = read_file_arg(path, ctx.base)
    if content is None:
        content = ctx.heredoc_files.get(path)
    if content is not None:
        payloads.append(content)
    elif fail_closed:
        payloads.append(FAIL_CLOSED_SENTINEL)
