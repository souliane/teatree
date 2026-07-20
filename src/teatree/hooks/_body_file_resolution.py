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

The matching primitives (``FAIL_CLOSED_SENTINEL``, ``read_file_arg``,
``attached_value``) live in the dependency-free :mod:`_parser_primitives` leaf;
BOTH this module and :mod:`_command_parser` import DOWN into that leaf, so
``_command_parser`` can import this resolver at module load (no cycle, no lazy
call-time imports) instead of the reach-sideways cycle the primitives previously
forced (#F7.9).
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL, attached_value, read_file_arg
from teatree.hooks._shell_lexer import Token, TokenKind, is_command_separator, split_commands

# Long options that point at a FILE whose content we should read. If the
# file is missing or unreadable the parser appends the fail-closed sentinel.
_BODY_FILE_FLAG_NAMES: Final[frozenset[str]] = frozenset({"--body-file", "--description-file", "--file"})

# The terminator is EXACT-LINE anchored: ``\n<delim>`` followed only by optional
# trailing spaces/tabs then end-of-line or end-of-string. The old ``\n<delim>\b``
# terminated on a mere word boundary, so a body line that BEGINS with the delim
# (``EOF and rest: leak``) matched and TRUNCATED the scanned body -- the leak
# after the delim word published unscanned. A heredoc terminator must sit ALONE
# on its line, so the exact-line anchor is both correct and closes the bypass
# (#F7.5).
_HEREDOC_RE: Final[re.Pattern[str]] = re.compile(
    r"<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1[ \t]*(?=\n|$)",
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
    r">{1,2}\|?\s*(?P<path>'[^']+'|\"[^\"]+\"|\S+)\s+<<-?\s*['\"]?(?P<delim>\w+)['\"]?\s*\n"
    r"(?P<body>.*?)\n(?P=delim)[ \t]*(?=\n|$)",
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


# Stdin spellings of a body-file flag: ``git commit -F -`` and the gh/glab
# ``--body-file -`` / ``-F -`` / ``--file -`` forms (plus the ``--file=-``
# equals spelling). ``-`` means "read the body/message from STDIN", so the body
# lives in whatever feeds the command's stdin (an in-command heredoc or a piped
# ``printf``/``echo`` writer), NOT in a file named ``-`` on disk.
STDIN_DASH: Final[str] = "-"

# Leaders whose ``-F -`` / ``--file -`` / ``--body-file -`` reads its
# body/message from stdin rather than a file named ``-``: git's commit-message
# flag and gh/glab's body-file short/long forms. The stdin body is resolved from
# the in-command heredoc / piped writer instead of fail-closing on ``-`` (#1415).
_STDIN_BODY_LEADERS: Final[frozenset[str]] = frozenset({"git", "gh", "glab"})


def _segments_with_leading_separator(tokens: list[Token]) -> list[tuple[str | None, list[str]]]:
    r"""Split ``tokens`` into ``(preceding-separator, WORD-values)`` pairs.

    Like :func:`_shell_lexer.split_commands` but preserves the command-separator
    operator (``|``/``;``/``&&``/``\n`` …) that PRECEDES each segment, so a
    caller can tell a PIPE-fed consumer (its stdin is the previous segment's
    stdout) from a merely sequenced one. The first segment's separator is
    ``None``.
    """
    out: list[tuple[str | None, list[str]]] = []
    current: list[Token] = []
    pending_sep: str | None = None
    for tok in tokens:
        if is_command_separator(tok):
            if current:
                out.append((pending_sep, [t.value for t in current if t.kind is TokenKind.WORD]))
                current = []
            pending_sep = tok.value
        else:
            current.append(tok)
    if current:
        out.append((pending_sep, [t.value for t in current if t.kind is TokenKind.WORD]))
    return out


def _reads_dash_stdin(words: list[str], flags: frozenset[str]) -> bool:
    """Return True iff a body-file ``flag`` in ``words`` points at stdin (``-``).

    Covers the space-separated (``--body-file -``), equals (``--file=-``), and
    git-glued short (``-F-``) spellings.
    """
    for i, word in enumerate(words):
        if word in flags and i + 1 < len(words) and words[i + 1] == STDIN_DASH:
            return True
        if any(word == f"{flag}=-" for flag in flags):
            return True
        if attached_value(word, "-F") == STDIN_DASH:
            return True
    return False


def _segment_reads_body_from_stdin(words: list[str]) -> bool:
    """Return True iff a git-commit / gh / glab segment reads its body from stdin.

    git's commit message comes from stdin on ``-F -`` / ``--file -`` /
    ``--file=-``; a gh/glab post body on ``--body-file -`` / ``-F -`` / ``--file
    -``. A bare ``git commit`` opens an editor and ``-F <file>`` / ``-m`` read
    elsewhere, so neither pairs with a pipe.
    """
    if not words:
        return False
    leader = PurePosixPath(words[0]).name
    if leader == "git":
        return "commit" in words and _reads_dash_stdin(words, frozenset({"-F", "--file"}))
    if leader in {"gh", "glab"}:
        return _reads_dash_stdin(words, frozenset({"-F", "--file", "--body-file"}))
    return False


def piped_stdin_writer_body(tokens: list[Token]) -> str | None:
    """Return the body a ``printf``/``echo`` writer pipes into a stdin body reader.

    For ``printf '%s' 'msg' | git commit -F -`` (or ``… | gh pr create
    --body-file -``) the writer's operands ARE the body fed to the reader's stdin
    — at PreToolUse scan time that is the only place the body lives (the command
    has not run). Returns the joined operands of a ``printf``/``echo`` segment
    sitting immediately upstream (via a ``|`` pipe) of a git-commit / gh / glab
    segment reading its body from stdin, else ``None``. The operands are joined
    verbatim and scanned as a conservative SUPERSET (a banned term / user quote
    in the real body is a substring of the join), never re-executed.
    """
    segments = _segments_with_leading_separator(tokens)
    for idx in range(1, len(segments)):
        separator, words = segments[idx]
        if separator != "|" or not _segment_reads_body_from_stdin(words):
            continue
        _, prev_words = segments[idx - 1]
        if prev_words and PurePosixPath(prev_words[0]).name in _REDIRECT_WRITER_COMMANDS:
            return " ".join(prev_words[1:])
    return None


@dataclass(frozen=True)
class BodyFileContext:
    """Resolution context for ``-F``/``--file``/``--body-file`` body files.

    Groups the settings that flow together through the body-file walkers: the
    in-command ``heredoc_files`` map (a body written earlier in the SAME command
    — a ``> path <<EOF`` heredoc or a ``printf``/``echo > path`` redirect — keyed
    by the redirect-target token), the ``base`` dir a relative body file is
    retried against (the commit's repo dir), ``fail_closed_body_file`` (what an
    UNREADABLE ``gh``/``glab`` body file does), and the two STDIN-body inputs that
    feed a ``git commit -F -`` (#1415): ``stdin_piped_body`` (a ``printf``/``echo
    | git commit -F -`` writer's body) and ``has_unredirected_heredoc`` (a
    ``git commit -F - <<EOF`` heredoc, already appended globally by
    :func:`_command_parser.extract_bash_payload`). When EITHER stdin source is
    present the commit message is READABLE, so ``git commit -F -`` resolves it
    instead of fail-closing on an unreadable stdin.
    """

    heredoc_files: dict[str, str]
    fail_closed_body_file: bool
    base: Path | None = None
    stdin_piped_body: str | None = None
    has_unredirected_heredoc: bool = False


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
    from teatree.hooks import _commit_repo_dir  # noqa: PLC0415 — deferred: call-time import, kept lazy

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
    from teatree.hooks._commit_repo_dir import leading_cd_dir  # noqa: PLC0415 — deferred: call-time import, kept lazy

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
            _append_file_payload(words[i + 1], payloads, ctx, fail_closed=fail_closed, leader=leader)
            i += 2
            continue
        attached: str | None = None
        for flag in _BODY_FILE_FLAG_NAMES:
            attached = attached_value(word, flag + "=")
            if attached is not None:
                _append_file_payload(attached, payloads, ctx, fail_closed=fail_closed, leader=leader)
                break
        if attached is not None:
            i += 1
            continue
        short = _short_f_body_file(leader, words, i, fail_closed=fail_closed)
        if short is not None:
            _append_file_payload(short.path, payloads, ctx, fail_closed=short.fail_closed, leader=leader)
            i += short.consumed
            continue
        i += 1


def _append_stdin_body(payloads: list[str], ctx: BodyFileContext, *, fail_closed: bool) -> None:
    """Resolve a ``… -F -`` / ``--body-file -`` body read from STDIN (#1415).

    ``-`` is not a file named ``-`` — the body/message comes from stdin, so it
    lives in whatever feeds the command's stdin at scan time:

    - a piped ``printf``/``echo`` writer (``printf 'msg' | gh pr create
        --body-file -``) → its operands are appended (``ctx.stdin_piped_body``)
        and SCANNED, so a real banned term / user quote in the body still blocks;
    - a heredoc (``gh pr create --body-file - <<EOF … EOF``) → the body is already
        appended globally by :func:`_command_parser.extract_bash_payload`
        (``ctx.has_unredirected_heredoc``), so this contributes nothing (no
        double-count) and emits NO sentinel — the heredoc content is scanned;
    - genuinely-opaque stdin (``cat file | gh pr create --body-file -``, an
        interactive editor) → the body is unreadable at scan time, so the generic
        fail-closed sentinel is emitted when ``fail_closed``. A PUBLIC gh/glab
        post the gate cannot read hard-blocks; a LOCAL git commit's sentinel is
        later DOWNGRADED to a warning by the destination-aware carve-out (the
        pre-push gate re-scans commit messages). ``fail_closed`` False appends
        nothing (the quote scanner's drafted-but-absent posture).

    Extending this from ``git commit -F -`` to gh/glab ``--body-file -`` is the
    #1415 fix: a clean heredoc/piped gh/glab body is no longer hard-blocked as an
    unreadable file named ``-`` (previously only git resolved its stdin body).
    """
    if ctx.stdin_piped_body is not None:
        payloads.append(ctx.stdin_piped_body)
    elif ctx.has_unredirected_heredoc:
        return
    elif fail_closed:
        payloads.append(FAIL_CLOSED_SENTINEL)


def _append_file_payload(
    path: str, payloads: list[str], ctx: BodyFileContext, *, fail_closed: bool, leader: str = ""
) -> None:
    """Append the body referenced by a ``-F``/``--file``/``--body-file`` path.

    A stdin body reference (``path == "-"`` on a git-commit / gh / glab leader —
    ``git commit -F -``, ``gh pr create --body-file -``) reads its body/message
    from STDIN, not a file named ``-``; it is resolved by
    :func:`_append_stdin_body` (the in-command heredoc / piped writer, else a
    fail-closed sentinel — always for git's LOCAL commit, else per the
    destination-aware ``fail_closed`` policy for a gh/glab post).

    For a real path the resolution order is: the on-disk file (as-is, then
    relative to ``ctx.base`` -- the commit's repo dir), then an in-command body
    written to that path — a ``cat > path <<EOF … EOF`` heredoc or a
    ``printf``/``echo > path`` redirect — then the ``fail_closed`` branch. The
    in-command fallback closes the #126 false positive where a body written to a
    temp file and posted via ``--body-file``/``-F`` in the same command was
    unreadable at PreToolUse scan time (the hook runs BEFORE the file is
    created); the lookup key is the raw ``--body-file`` argument token, so a
    ``$f`` / ``$(mktemp)`` path matches the textually-identical redirect target
    even though neither is expanded. The ``ctx.base`` fallback closes the
    cold-hook false positive where the harness cwd has reset away from the
    worktree, so a ``git -C <worktree> commit -F <relpath>`` body file is
    unreadable from the cwd yet readable from the commit's own repo dir.

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
    if path == STDIN_DASH and leader in _STDIN_BODY_LEADERS:
        # git's commit-message stdin ALWAYS fails closed on opaque input (#1207);
        # gh/glab's body-file stdin follows the destination-aware fail_closed policy.
        _append_stdin_body(payloads, ctx, fail_closed=leader == "git" or fail_closed)
        return
    # Consult the in-command heredoc/redirect body FIRST and the on-disk file
    # SECOND, appending BOTH when both resolve (a conservative SUPERSET scan). A
    # pre-existing REUSED temp path could otherwise let a STALE clean on-disk file
    # shadow the real body the command is about to write there via ``> path <<EOF``
    # -- the gate scanned the stale content and the live body published unscanned
    # (#F7.6). Scanning both closes that: a banned term / user quote in EITHER the
    # heredoc body or the on-disk file is a substring of the joined payload.
    heredoc_body = ctx.heredoc_files.get(path)
    disk_content = read_file_arg(path, ctx.base)
    appended = False
    if heredoc_body is not None:
        payloads.append(heredoc_body)
        appended = True
    if disk_content is not None:
        payloads.append(disk_content)
        appended = True
    if not appended and fail_closed:
        payloads.append(FAIL_CLOSED_SENTINEL)
