r"""Bash command surface parsing for the quote-scanner gate (#1213).

Extracted from :mod:`teatree.hooks.quote_scanner` to keep that module
under the project's per-file LOC ceiling. The public quote-scanner API
(scan_text, format_*, log_decision, extract_publish_payload,
has_quote_ok_override) lives in ``quote_scanner.py`` and delegates the
shell-grammar work to the helpers here.

The parser walks a Bash command string in two passes:

1. :mod:`teatree.hooks._shell_lexer` produces a token stream where bash
    shell grammar is honoured (``\<NL>`` removed token-internally,
    ``;``/``|``/``&``/``&&``/``||`` emitted as standalone metachars
    regardless of whitespace, ANSI-C ``$'...'`` decoded properly per
    the bash man-page).
2. Per-command argument walkers iterate over the WORD tokens of each
    command segment and pull out body-flag values, heredoc-style content,
    and attached short-option payloads (``-d'{...}'``).

Indirect body sources we cannot inspect (``gh api --input -``, opaque
``-d @file`` references, a missing ``git commit -F`` message file) fail
closed via a sentinel string that downstream scanning treats as a HIGH
match. A missing ``gh``/``glab`` ``--body-file`` is the one exception:
an absent drafted PR/issue body is "needs-inline", not a leak, so it
contributes no payload rather than a fail-closed HIGH (#126).

Publish-surface DETECTION (which command shapes are a publish at all) lives
in :mod:`teatree.hooks._publish_detection`: the contiguous-substring catalogue
here plus the token-aware ``api`` / ``git commit`` / opaque-forge-transport
classifiers there, so an interspersed persistent flag cannot break detection
(#1672). This module owns body / title / secret-surface EXTRACTION.
"""

from pathlib import Path, PurePosixPath
from typing import Final

from teatree.hooks._body_file_resolution import (
    BodyFileContext,
    command_body_file_base,
    commit_body_file_base,
    heredoc_files_map,
    piped_stdin_writer_body,
    unredirected_heredoc_bodies,
    walk_body_file_flags,
)
from teatree.hooks._curl_payload import _json_body_fields, _walk_curl_args
from teatree.hooks._inline_body_resolution import resolve_inline_body_value
from teatree.hooks._parser_primitives import (
    FAIL_CLOSED_SENTINEL,
    UNAVAILABLE_BODY_SOURCE_SENTINEL,
    attached_value,
    canonical_forge_leader,
    is_fail_closed_sentinel,
    is_unavailable_body_source_sentinel,
    read_file_arg,
    wrapper_prefix_len,
)
from teatree.hooks._publish_detection import (
    command_has_interpreter_forge_transport,
    command_has_opaque_forge_transport,
    command_has_token_aware_publish_surface,
    extract_title_fragments,
    segment_is_substring_publish,
    segment_word_lists,
)
from teatree.hooks._python_rest_detection import (
    command_has_python_rest_publish_surface,
    is_python_leader,
    segment_is_python_rest_publish,
)
from teatree.hooks._shell_lexer import Token, TokenKind, split_commands, tokenize

# Re-exported for backward compatibility: several sibling gates import these
# primitives from ``_command_parser`` (their historical home). They now live in
# the dependency-free ``_parser_primitives`` leaf (which breaks the
# ``_command_parser`` ⇄ ``_body_file_resolution`` cycle, #F7.9); re-exporting
# keeps every ``from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL``
# (and siblings) resolving unchanged.
__all__ = [
    "FAIL_CLOSED_SENTINEL",
    "UNAVAILABLE_BODY_SOURCE_SENTINEL",
    "attached_value",
    "extract_bash_payload",
    "extract_secret_scan_text",
    "first_segment_words",
    "is_fail_closed_sentinel",
    "is_publish_command",
    "is_unavailable_body_source_sentinel",
    "read_file_arg",
]

# ── Publish-surface substring catalogues ────────────────────────────

# The ``gh``/``glab``/``git``/``curl`` contiguous-substring spellings now live
# in :data:`_publish_detection._LEADER_PUBLISH_SUBSTRINGS`, keyed by their owning
# leader so a read-only ``grep``/``cat``/``rg`` that merely QUOTES one is not a
# publish (the false positive). ``gh``/``glab api`` stays WRITE-only / token-aware
# (:func:`_publish_detection.segment_is_api_write`, effective method ≠ GET, #1530).

# t3 sub-commands that publish on the user's behalf. The overlay segment
# between ``t3`` and the verb is arbitrary (one of the registered
# overlays), so we match the verb-segment substring directly — e.g.
# ``review post-comment`` matches both ``t3 teatree review post-comment``
# and the equivalent per-overlay variant.
_T3_PUBLISH_SUBSTRINGS: Final[tuple[str, ...]] = (
    "notify send",
    "review post-comment",
    "review post-draft-note",
    "ticket create-issue",
    "t3 slack react",
)


def _segment_is_t3_publish(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``t3``-led segment carrying a publish verb.

    Keyed to the segment's own leading executable so a read-only
    ``grep "notify send"`` (leader ``grep``) is not misread as a ``t3`` post,
    and a ``cd <wt> && t3 <overlay> notify send`` (the publish verb on its own
    ``t3``-led segment) is correctly detected. The leader is canonicalised up to
    the ``t3`` executable basename so a path-form leader (``./t3``,
    ``/usr/local/bin/t3``) is recognised the same as a bare ``t3`` (env-prefixed
    leaders are already stripped by :func:`_publish_detection.segment_word_lists`).
    The overlay segment between ``t3`` and the verb is arbitrary, so the
    verb-segment substring is matched against the canonicalised joined words.
    """
    if PurePosixPath(words[0]).name != "t3":
        return False
    joined = " ".join(["t3", *words[1:]])
    return any(needle in joined for needle in _T3_PUBLISH_SUBSTRINGS)


def is_publish_command(command: str) -> bool:
    """Return True iff the Bash command would publish to an external surface.

    Detection is per-SEGMENT and keyed to each segment's own leading executable:

    - the leader-keyed substring catalogue
        (:func:`_publish_detection.segment_is_substring_publish`) catches the
        common ``gh``/``glab``/``git``/``curl`` spellings ONLY in a segment whose
        own leader is that tool -- so a read-only ``grep "glab mr create"`` /
        ``cat | grep "gh issue create"`` / ``rg "git commit -m"`` that merely
        QUOTES the spelling in an argument is NOT a publish;
    - the ``t3`` publish verbs (:func:`_segment_is_t3_publish`), likewise keyed
        to a ``t3``-led segment; and
    - the token-aware per-segment checks
        (:func:`_publish_detection.command_has_token_aware_publish_surface`) catch
        the ``git [global-flags] commit`` after a ``-C``/``--git-dir`` flag and the
        raw-REST ``gh``/``glab api`` WRITE (effective method ≠ GET) regardless of
        flag ordering. A read-only ``gh``/``glab api`` GET is NOT a publish (#1530).
    - a python REST-publish segment
        (:func:`_publish_detection.command_has_python_rest_publish_surface`) --
        a ``python3``/``python``-led segment POSTing/PATCHing to a forge REST
        API (``requests``/``httpx``/``urllib``/a raw ``http.client`` call),
        the SAME write-method + forge-target shape as ``gh``/``glab api``,
        just authored in Python instead of CLI flags (#2943 gap).
    - a forge call hidden inside a command-string INTERPRETER argument
        (:func:`_publish_detection.command_has_interpreter_forge_transport`) --
        ``sh -c "gh pr create --body X"``, ``eval "gh ..."``, ``ssh host gh ...``.
        The forge tool never reaches an argv position the substring / api /
        git-commit detectors parse, so a STANDALONE wrapper-hidden post used to
        evade detection and skip BOTH leak gates entirely; the destination-aware
        gates then fail closed on the unscannable body (#F7.1). A read-only
        inspection that merely QUOTES a forge token (``rg 'sh -c "gh"'``) has a
        non-interpreter leader and is NOT classified as a publish.
    """
    for words in segment_word_lists(command):
        if segment_is_substring_publish(words) or _segment_is_t3_publish(words):
            return True
    if command_has_token_aware_publish_surface(command):
        return True
    if command_has_python_rest_publish_surface(command):
        return True
    return command_has_interpreter_forge_transport(command)


# Per-command argument-walker dispatch tables --------------------------

# Body-bearing long options (value follows the flag as next token or
# attached via ``=``). The catalogue is shared by all publishing
# commands — gh, glab, git, curl all use the same long-option grammar.
_BODY_FLAG_NAMES: Final[frozenset[str]] = frozenset(
    {"--body", "--description", "--message", "--title"},
)

# Short body-bearing flags used by ``gh`` / ``glab`` / ``git commit``.
_BODY_SHORT_FLAGS: Final[frozenset[str]] = frozenset({"-m", "-b"})

# ``glab`` spells the MR/issue description short flag ``-d`` on ``create`` and
# ``update``; ``gh`` uses ``-d`` for the boolean ``--draft``, so this short flag
# is scoped to the ``glab`` leader only — extracting the next token as a body
# for ``gh -d`` would misread its boolean draft switch.
_GLAB_BODY_SHORT_FLAGS: Final[frozenset[str]] = frozenset({"-d"})

# Long options for ``gh api`` / ``glab api`` field assignments.
_API_FIELD_LONG_FLAGS: Final[frozenset[str]] = frozenset({"--field", "--raw-field"})
_API_FIELD_SHORT_FLAGS: Final[frozenset[str]] = frozenset({"-f", "-F"})


def _walk_python_script(words: list[str], payloads: list[str]) -> None:
    """Extract a python ``-c "<script>"`` inline script argument as a payload.

    Python owns no ``--body``/``--file`` grammar of its own, so the generic
    body-flag/file walkers never see the script text a REST-publish call
    lives in. A heredoc-fed script's body is captured separately (and
    unconditionally, regardless of leader) by :func:`extract_bash_payload`'s
    heredoc-body pass -- this walker covers only the ``-c`` inline form. The
    caller gates this on :func:`_python_rest_detection.segment_is_python_rest_publish`
    so an unrelated python ``-c`` one-liner (a local computation, a secret
    read for LOCAL use) is never dumped into the wide secret-scan surface
    :func:`extract_secret_scan_text` runs regardless of destination (#2943
    review finding: gating on the leader alone over-widened that surface).
    """
    i = 0
    n = len(words)
    while i < n:
        if words[i] == "-c" and i + 1 < n:
            payloads.append(words[i + 1])
            i += 2
            continue
        i += 1


def _walk_body_flags(words: list[str], raws: list[str], payloads: list[str], base: "Path | None", leader: str) -> None:
    """Extract ``--body``/``--description``/``--message``/``--title``/``-m``/``-b`` payloads.

    Handles both space-separated (``--body "x"``) and equals-separated
    (``--body=x``) forms. Each extracted value is passed through
    :func:`_body_file_resolution.resolve_inline_body_value`, which resolves a
    ``$(cat <path>)`` command substitution to the file content and a ``$VAR`` to
    its environment value (``base`` is the cold-hook cwd fallback for a relative
    cat path); a LIVE unresolvable indirection (one bash would expand) yields the
    fail-closed sentinel so the scan blocks rather than reads an unexpanded shell
    token. ``raws`` carries each value token's verbatim source span (parallel to
    ``words``) so the resolver can tell a single-quoted INERT ``$(...)`` (the body
    is the literal text — scanned) from a live one — an embedded substitution in a
    body the gate can read is no longer mis-flagged as unresolvable.

    ``leader`` selects the short-flag grammar: ``glab`` adds the ``-d``
    description short flag it uses on ``mr``/``issue`` ``create``/``update``,
    which ``gh`` uses for the boolean ``--draft`` and so is glab-only.
    """
    short_flags = _BODY_SHORT_FLAGS | _GLAB_BODY_SHORT_FLAGS if leader == "glab" else _BODY_SHORT_FLAGS
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _BODY_FLAG_NAMES and i + 1 < n:
            payloads.append(resolve_inline_body_value(words[i + 1], base, raws[i + 1]))
            i += 2
            continue
        attached_handled = False
        for flag in _BODY_FLAG_NAMES:
            attached = attached_value(word, flag + "=")
            if attached is not None:
                payloads.append(resolve_inline_body_value(attached, base, raws[i]))
                attached_handled = True
                break
        if attached_handled:
            i += 1
            continue
        if word in short_flags and i + 1 < n:
            payloads.append(resolve_inline_body_value(words[i + 1], base, raws[i + 1]))
            i += 2
            continue
        i += 1


def _handle_api_input(arg: str, payloads: list[str]) -> None:
    """Read a ``--input`` argument: stdin or missing file → fail closed.

    A literal ``-`` is a STDIN body the PreToolUse hook cannot read before the
    command runs, so it carries :data:`UNAVAILABLE_BODY_SOURCE_SENTINEL` and the
    gate renders the actionable "write the body to an absolute file" message
    (#2369). A NAMED file that does not exist is a missing FILE, so it keeps the
    generic :data:`FAIL_CLOSED_SENTINEL` whose "body file is missing" advice fits.
    """
    if arg == "-":
        payloads.append(UNAVAILABLE_BODY_SOURCE_SENTINEL)
        return
    content = read_file_arg(arg)
    if content is None:
        payloads.append(FAIL_CLOSED_SENTINEL)
        return
    payloads.append(content)
    payloads.extend(_json_body_fields(content))


def _walk_api_fields(words: list[str], raws: list[str], payloads: list[str], base: "Path | None") -> None:
    """Extract ``-f``/``-F``/``--field``/``--raw-field`` ``body=`` assignments.

    Also handles ``--input <file>`` / ``--input -`` (stdin → fail closed)
    and ``--input <missing>`` (fail closed). Field assignments other than
    ``body=`` are ignored. ``raws`` (parallel to ``words``) carries each token's
    verbatim source span so a single-quoted INERT ``$(...)`` in a ``body=``
    field is scanned rather than fail-closed.
    """
    field_flags = _API_FIELD_SHORT_FLAGS | _API_FIELD_LONG_FLAGS
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in field_flags and i + 1 < n:
            _handle_field_assignment(words[i + 1], payloads, base, raws[i + 1])
            i += 2
            continue
        if word == "--input" and i + 1 < n:
            _handle_api_input(words[i + 1], payloads)
            i += 2
            continue
        attached = attached_value(word, "--input=")
        if attached is not None:
            _handle_api_input(attached, payloads)
        i += 1


def _handle_field_assignment(arg: str, payloads: list[str], base: "Path | None", raw: str = "") -> None:
    """Parse a ``-F body=value`` style argument and append the resolved value.

    The ``body=`` prefix is required — other field names (``title=``,
    etc.) are not body-bearing and are ignored. The value is resolved through
    :func:`_body_file_resolution.resolve_inline_body_value` — the SAME path the
    ``--body``/``-m``/positional-NOTE handling uses — so a ``-f body=$(cat
    <path>)`` / ``-f body=$VAR`` field is scanned against the resolved file/var
    content rather than the literal ``$(cat …)`` / ``$VAR`` token (a leak inside
    the referenced file would otherwise slip onto a public repo). ``raw`` is the
    field token's verbatim source span so a single-quoted INERT ``$(...)`` is
    scanned; a LIVE unresolvable indirection yields the fail-closed sentinel.
    """
    if "=" not in arg:
        return
    name, _, value = arg.partition("=")
    if name == "body":
        payloads.append(resolve_inline_body_value(value, base, raw))


# ── Command-segment walking ─────────────────────────────────────────


def _walk_command_segment(segment: list[Token], payloads: list[str], ctx: "BodyFileContext") -> None:
    """Route a single command segment to the right argument walkers.

    Leading benign-prefix tokens (env-assignments, ``cd``/``pushd`` nav, one
    transparent argv wrapper -- ``xargs gh``, ``env GH_PAGER= gh``,
    ``/usr/bin/gh``) are stripped index-parallel from ``words`` and ``raws`` and
    the leader is canonicalised to the real forge tool's basename BEFORE the
    per-command walkers dispatch, so a wrapper/path-hidden ``gh``/``glab``/``git``
    body is extracted the same as the bare form. Previously the leader-keyed
    walkers (``gh``/``glab api`` fields, ``curl``, python ``-c``) keyed on the
    RAW first word (``xargs``, ``/usr/bin/gh``, a lowercase ``foo=1`` prefix),
    silently skipped, and the body went unscanned -- detection said "publish"
    while extraction produced nothing, failing OPEN (#F7.1/#F7.3).
    """
    from teatree.hooks._t3_review_post import append_t3_review_note_payload  # noqa: PLC0415 — deferred: import cycle

    word_tokens = [tok for tok in segment if tok.kind is TokenKind.WORD]
    all_words = [tok.value for tok in word_tokens]
    if not all_words:
        return
    # Strip the benign env/cd/wrapper prefix from BOTH parallel lists by the same
    # count so ``words`` and ``raws`` stay index-aligned. ``raws`` carries the
    # verbatim source span (quotes intact) of each WORD; the inline-body resolver
    # reads it to tell a LIVE ``$(...)`` bash would expand (double-quoted /
    # unquoted ⇒ fail closed) from an INERT one bash passes verbatim
    # (single-quoted ⇒ the body is fully present and scanned) — so a commit/note
    # body that merely MENTIONS a ``$(...)`` snippet is no longer mis-classified
    # as an unreadable source (#1415).
    all_raws = [tok.raw for tok in word_tokens]
    skip = wrapper_prefix_len(all_words)
    words = all_words[skip:]
    raws = all_raws[skip:]
    if not words:
        return
    first = canonical_forge_leader(all_words)
    # All segments get the generic body-flag walker since gh, glab, git,
    # and t3 all accept ``--body``/``--message``/``-m``/``-b``. The canonical
    # leader selects glab's extra ``-d`` description short flag.
    _walk_body_flags(words, raws, payloads, ctx.base, first)
    # ``t3 review`` posts carry the body as the positional NOTE (or the #32
    # ``--body-file``) and use ``--file`` as a diff ANCHOR, not a body-file —
    # extract the NOTE + ``--body-file`` body and SKIP the generic body-file
    # walker so the anchored source is never scanned (#2278/#2270/#32).
    if append_t3_review_note_payload(words, raws, payloads, ctx):
        return
    walk_body_file_flags(words, payloads, leader=first, ctx=ctx)
    # ``gh api`` / ``glab api`` field assignments.
    if first in {"gh", "glab"}:
        _walk_api_fields(words, raws, payloads, ctx.base)
    if first == "curl":
        _walk_curl_args(words, payloads)
    # Gated on the ACTUAL classification (write verb + forge URL), not merely
    # the python leader: ``extract_bash_payload`` also backs
    # ``extract_secret_scan_text``, which runs on EVERY Bash command
    # regardless of destination -- an unconditional append here would dump
    # any unrelated python ``-c`` one-liner's full source (a local
    # computation, a secret read for LOCAL use) into that wide surface and
    # false-block it as a "publish payload" it never was.
    if is_python_leader(first) and segment_is_python_rest_publish(words, " ".join(words)):
        _walk_python_script(words, payloads)


# ── Body extraction ─────────────────────────────────────────────────


def extract_bash_payload(command: str, *, fail_closed_body_file: bool = False, cwd: Path | None = None) -> str:
    r"""Concatenate every body-like fragment the command surface carries.

    The command is tokenized once via :mod:`teatree.hooks._shell_lexer`
    so shell-equivalent spellings (line continuations both token-
    internal and between-token, ANSI-C ``$'...'`` quoting, unspaced
    metacharacters) collapse to the same logical token stream bash
    itself would execute. Then each command segment is routed to the
    right per-command argument walker.

    Indirect body sources (``gh api --input -``, missing files, opaque
    ``-d @file`` references) fail closed via the sentinel. A
    ``--body-file``/``-F <path>`` reference whose body is written earlier in
    the same command — by a ``> path <<EOF … EOF`` heredoc (#126) or by a
    ``printf``/``echo > path`` redirect, including when the path is the same
    unexpanded ``$f`` / ``$(mktemp)`` token in both the write and the
    reference — resolves to that in-command body instead of failing closed.

    ``fail_closed_body_file`` controls an UNREADABLE ``gh``/``glab`` body
    file: ``False`` (default, the quote scanner) keeps the #126 behaviour
    (an absent draft body contributes nothing); ``True`` (the
    destination-aware banned-terms / bare-reference gates) appends the
    fail-closed sentinel so a PUBLIC file-body post whose body the gate
    cannot read hard-blocks instead of slipping through unread.

    A ``git commit -F <relpath>`` body file is resolved against the dir whose
    repo the commit LANDS in (the command's own ``cd``/``-C``/``--git-dir``,
    via :func:`_body_file_resolution.commit_body_file_base`); a ``cd <dir> && gh
    pr create --body-file <relpath>`` body file is resolved against the command's
    leading ``cd`` dir (:func:`_body_file_resolution.command_body_file_base`);
    else the harness-provided ``cwd``. This handles the cold hook's reset cwd, so
    the gate scans the real body and applies the private-repo carve-out instead
    of fail-closing on a clean post whose body it could not read.
    """
    parts: list[str] = []
    tokens = tokenize(command)
    # Heredocs fed straight to a CONSUMER (stdin / ``$(cat <<EOF)``) are part of
    # the published payload and are appended below; their presence ALSO resolves
    # a ``git commit -F -`` stdin body (the heredoc feeds git's stdin), so the
    # ``-F -`` walker emits no spurious sentinel and never double-counts (#1415).
    unredirected_heredocs = unredirected_heredoc_bodies(command)
    ctx = BodyFileContext(
        heredoc_files=heredoc_files_map(command, tokens),
        fail_closed_body_file=fail_closed_body_file,
        base=commit_body_file_base(command, cwd) or command_body_file_base(command) or cwd,
        stdin_piped_body=piped_stdin_writer_body(tokens),
        has_unredirected_heredoc=bool(unredirected_heredocs),
    )
    for segment in split_commands(tokens):
        _walk_command_segment(segment, parts, ctx)
    # Heredocs still need to be parsed against the raw command — the lexer treats
    # them as regular content since heredoc bodies live on subsequent physical
    # lines. Only heredocs fed straight to a CONSUMER (stdin / ``$(cat <<EOF)``)
    # are emitted here; a ``> path <<EOF`` heredoc writes to a file resolved by
    # path-pairing, so emitting it blanket would scan an unposted scratch body
    # and double-count a posted one.
    parts.extend(unredirected_heredocs)
    # A forge call hidden inside a command-string interpreter argument
    # (``sh -c "gh ... --body X"``, ``eval``, ``ssh host gh``) or a live
    # ``$(...)`` substitution carries its body in an opaque token the walkers
    # cannot descend into. The sentinel is appended for BOTH gate modes -- not
    # only the destination-aware banned-terms gate (``fail_closed_body_file``) but
    # ALSO the quote gate (``fail_closed_body_file=False``) -- so a wrapper-hidden
    # publish fails closed on EVERY leak gate rather than slipping through the
    # quote gate unread (#F7.1). This runs only when the command is already a
    # publish (the caller gates on :func:`is_publish_command`), so a benign
    # ``echo $(date)`` -- not a publish -- is never scanned here. A transparent
    # wrapper (``xargs gh``, ``/usr/bin/gh``) is NOT opaque: its leader
    # canonicalises to the forge tool and its body is extracted above.
    if command_has_opaque_forge_transport(command):
        parts.append(FAIL_CLOSED_SENTINEL)
    return "\n".join(parts)


# ── Secret-scan surfaces (#1672) ────────────────────────────────────


def _api_field_values(words: list[str]) -> list[str]:
    """Return EVERY ``-f``/``-F``/``--field``/``--raw-field`` field VALUE.

    The body extractor keeps only ``body=`` assignments; a secret can equally
    live in a ``-f title=`` or any other field of a ``gh api`` / ``glab api``
    call, so the secret scan reads every field value (the part after ``=``)
    regardless of field name. Bare values (no ``=``) are kept as-is.
    """
    field_flags = _API_FIELD_SHORT_FLAGS | _API_FIELD_LONG_FLAGS
    values: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in field_flags and i + 1 < n:
            values.append(words[i + 1].partition("=")[2] or words[i + 1])
            i += 2
            continue
        i += 1
    return values


def extract_secret_scan_text(command: str) -> str:
    """Concatenate EVERY surface a secret must be blocked on, regardless of destination.

    A secret leaks on ALL surfaces (a title, a short ``-t`` flag, a
    ``gh api -f title=`` field), not only the description body the carve-out
    is about. This widens the secret check beyond :func:`extract_bash_payload`
    to also cover the title / commit-subject fragments
    (:func:`extract_title_fragments`) and every ``gh``/``glab api`` field value
    (:func:`_api_field_values`), so :func:`publish_surface.contains_secret`
    sees them before the destination skip can short-circuit a scan.
    """
    parts = [extract_bash_payload(command, fail_closed_body_file=False)]
    parts.extend(extract_title_fragments(command))
    for words in segment_word_lists(command):
        # Canonicalise the leader (transparent wrapper stripped, basename taken)
        # so a secret in a ``xargs gh api -f title=…`` / ``/usr/bin/gh api …``
        # field is scanned too, not just the bare ``gh api`` form (#F7.1).
        if canonical_forge_leader(words) in {"gh", "glab"}:
            parts.extend(_api_field_values(words))
    return "\n".join(part for part in parts if part)


# ── Quote-OK override detection ─────────────────────────────────────


def first_segment_words(command: str) -> list[str]:
    """Return the WORD-value list of the FIRST command segment.

    Used by the override-detection: a ``--quote-ok`` token is only
    honoured when it appears as a CLI token in the first segment of the
    bash command. Anything after the first command-separator operator
    is a separate command and must not bypass the gate (codex round-2
    #1, round-3 #2).
    """
    tokens = tokenize(command)
    segments = split_commands(tokens)
    if not segments:
        return []
    return [tok.value for tok in segments[0] if tok.kind is TokenKind.WORD]
