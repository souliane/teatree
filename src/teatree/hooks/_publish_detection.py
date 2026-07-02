r"""Token-aware publish/commit/api detection for the pre-publish gates (#1672).

Split out of :mod:`teatree.hooks._command_parser` to keep that module under the
project's per-file LOC ceiling. This module owns publish-surface DETECTION:
whether a Bash command segment is a publish the gates must scan. Two layers:

- the leader-keyed contiguous-substring catalogue
    (:data:`_LEADER_PUBLISH_SUBSTRINGS`, :func:`segment_is_substring_publish`) --
    each spelling (``gh pr create``, ``git commit -m``, ``chat.postMessage``)
    matches ONLY in a segment whose own leading executable is that spelling's
    owning tool, so a read-only ``grep "glab mr create"`` that merely QUOTES the
    spelling in an argument is not a publish; and
- the token-aware per-WORD-position checks below, robust to interspersed
    persistent flags.

The contiguous-substring detection matches spellings like ``gh api ``,
``git commit -m``. An interspersed persistent flag breaks contiguity, so a real
publish would slip the substring detection unseen -- the token-aware checks
close that:

- ``gh --hostname H api ...`` / ``gh -X POST api ...`` -- a persistent flag
    before the ``api`` sub-command (:func:`segment_is_api_call`);
- ``git -C <dir> commit -m ...`` / ``git --git-dir=x commit --message ...`` --
    a value-taking global flag before the ``commit`` verb
    (:func:`_segment_is_git_commit_publish`); and
- ``sh -c "gh ... --body X"`` / ``eval`` / ``ssh host gh`` / ``xargs gh`` -- a
    forge call HIDDEN inside an interpreter argument the body walkers cannot
    descend into (:func:`segment_is_opaque_forge_transport`), which the gates
    fail closed on rather than scan an unreachable body.

Position-aware matching is robust to flag ordering WITHOUT enumerating every
persistent flag -- the closed inversion the anti-whack-a-mole doctrine requires.
"""

import re
from itertools import starmap
from typing import Final

from teatree.hooks._gh_glab_hiding import token_has_substitution_marker
from teatree.hooks._shell_lexer import TokenKind, raw_substitution_sees_live, split_commands, tokenize

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# Value-taking global ``git`` flags that sit BEFORE the ``commit`` verb
# (``git -C <dir> commit``, ``--git-dir``, ``--work-tree``). The token-aware walk
# skips them (flag + value, plus their ``=`` forms) so the ``commit`` verb is
# reached. Mirrors ``publish_surface._GIT_GLOBAL_DIR_FLAGS``.
_GIT_GLOBAL_DIR_FLAGS: Final[frozenset[str]] = frozenset({"-C", "--git-dir", "--work-tree"})

# Message-bearing flags that make a ``git commit`` a publish surface (its body
# lands in public history): ``-m`` / ``--message`` / ``-F`` / ``--file`` -- the
# same set the substring catalogue covers, now reached token-aware.
_GIT_COMMIT_BODY_FLAGS: Final[frozenset[str]] = frozenset({"-m", "--message", "-F", "--file"})
_GIT_COMMIT_BODY_ATTACHED: Final[tuple[str, ...]] = ("-m", "-F", "--message=", "--file=")

# Forge-tool command words the body-extracting walkers can parse a body out of.
# A segment whose LEADING executable (after cd/env) is one of these is parseable;
# a forge token appearing only NESTED (a ``sh -c "gh ... --body X"`` interpreter
# arg, an ``eval``/``ssh``/``xargs`` wrapper) is an OPAQUE forge transport the
# walkers cannot reach -- so the body the post carries is unscannable.
_PARSEABLE_FORGE_LEADERS: Final[frozenset[str]] = frozenset({"gh", "glab", "git", "curl"})

# Contiguous-substring publish spellings, keyed by the LEADING executable that
# owns each (the first word of the substring -- ``chat.postMessage`` is a Slack
# REST endpoint reachable only via ``curl``). A substring is a publish ONLY when
# it appears in a SEGMENT whose own leading executable (after a benign cd/env
# prefix) is that leader: a read-only ``grep "glab mr create"`` / ``rg "git
# commit -m"`` / ``cat | grep "gh issue create"`` merely QUOTES the substring in
# an argument, so its leader is ``grep``/``rg``/``cat`` -- not a publish. Keying
# detection to the segment leader closes that recurring false positive without
# enumerating read-only tools (the inversion: prove the segment IS a forge call,
# rather than denylist the inspection tools that can quote a forge string).
_LEADER_PUBLISH_SUBSTRINGS: Final[tuple[tuple[str, str], ...]] = (
    ("gh", "gh issue create"),
    ("gh", "gh issue edit"),
    ("gh", "gh issue comment"),
    ("gh", "gh pr create"),
    ("gh", "gh pr edit"),
    ("gh", "gh pr comment"),
    ("gh", "gh pr review"),
    ("glab", "glab issue create"),
    ("glab", "glab issue update"),
    ("glab", "glab issue note create"),
    # ``glab issue note <id>`` (no ``create`` segment) is the real comment
    # subcommand -- trailing space pins it to the subcommand boundary so
    # ``glab issue notebook`` would not match.
    ("glab", "glab issue note "),
    ("glab", "glab mr create"),
    ("glab", "glab mr update"),
    ("glab", "glab mr note create"),
    ("glab", "glab mr note "),
    ("git", "git commit -m"),
    ("git", "git commit --message"),
    ("git", "git commit -F"),
    ("git", "git commit --file"),
    ("git", "git tag --message"),
    ("curl", "chat.postMessage"),
)

# Forge-tool markers detected as a WORD within any token, so a forge call
# hidden inside a quoted interpreter argument (``sh -c "gh pr create ..."``,
# one token after tokenization) is recognised as a transport.
_FORGE_TOOL_MARKERS: Final[tuple[str, ...]] = ("gh", "glab", "curl")

# Matches a marker only at a WORD boundary within the token, never inside a
# longer run of word characters. A raw substring check (``marker in token``)
# false-positived on ordinary English words carrying ``gh`` mid-word --
# "though", "night", "light", "right", "weight", "eight" -- which a
# ``t3 review post-comment`` NOTE (or any other publish body) legitimately
# contains, wrongly classifying the whole segment as an opaque forge
# transport and injecting the fail-closed sentinel into its own clean payload
# (#1415). ``\b`` still matches a marker glued to punctuation/path separators
# (`` "gh issue create ..." `` starts the token, ``/usr/bin/gh`` ends it), so
# the opaque-wrapper detection (``sh -c "gh ..."``) is unaffected.
_FORGE_TOOL_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(rf"\b{re.escape(marker)}\b" for marker in _FORGE_TOOL_MARKERS)
)


def _token_carries_forge_marker(token: str) -> bool:
    """Return True iff ``token`` contains a forge-tool marker as a whole word."""
    return bool(_FORGE_TOOL_MARKER_RE.search(token))


# Title / commit-subject flags (#1544). A title (``gh``/``glab`` ``--title``)
# or git-commit subject is a forge surface distinct from the description body.
_TITLE_LONG_FLAG: Final[str] = "--title"
_TITLE_SHORT_FLAG: Final[str] = "-t"
_GIT_COMMIT_MESSAGE_FLAGS: Final[frozenset[str]] = frozenset({"-m", "--message"})

# ``gh api`` / ``glab api`` request-body flags. Their presence makes a
# method-less call default to POST (a write); absent them it defaults to GET (a
# read). Mirrors ``hook_router._REVIEW_POST_BODY_FLAG_RE``.
_API_BODY_FLAGS: Final[frozenset[str]] = frozenset(
    {"-f", "--field", "-F", "--raw-field", "--input", "-d", "--data"},
)
# Read-only effective HTTP methods. Every other method mutates and is a write.
_API_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})


def _attached_value(token: str, prefix: str) -> str | None:
    """Return the attached value of ``-X<value>`` / ``-X=<value>``, if any."""
    if token.startswith(prefix) and len(token) > len(prefix):
        return token[len(prefix) :].removeprefix("=")
    return None


def segment_word_lists(command: str) -> list[list[str]]:
    """Return the WORD-value list of every top-level command segment.

    Leading inline ``KEY=value`` env assignments are stripped so a publish verb
    behind an env prefix is still found. Mirrors
    :func:`_gh_glab_hiding.command_segments`.
    """
    segments: list[list[str]] = []
    for segment in split_commands(tokenize(command)):
        words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
        while words and _ENV_ASSIGNMENT_RE.fullmatch(words[0]):
            words = words[1:]
        if words:
            segments.append(words)
    return segments


def segment_word_lists_raw(command: str) -> list[list[str]]:
    """Return every top-level segment's WORD values WITHOUT stripping env prefixes.

    The sibling :func:`segment_word_lists` strips leading ``KEY=value`` env
    assignments; this keeps them so an override detector can inspect the
    assignment bash applies to that segment's command. A leading inline
    env-assignment (``ENV=1 git commit``) leads ONLY the command of its own
    segment, so checking each segment's own leading run is what honours a
    ``cd <dir> && ENV=1 git commit`` override without letting a chained second
    command that lacks the override bypass the gate.
    """
    return [
        [tok.value for tok in segment if tok.kind is TokenKind.WORD] for segment in split_commands(tokenize(command))
    ]


def segment_is_api_call(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``gh``/``glab`` raw-REST call.

    A ``gh``/``glab`` segment is raw REST iff the ``api`` sub-command WORD
    appears anywhere after the tool word, regardless of any interspersed
    persistent flag (``--hostname H``, ``-R repo``, ``-X POST``) or stray token.
    This catches ``gh --hostname github.com api ...`` and ``gh -X POST api ...``
    the contiguous ``gh api `` substring missed. Matching the bare ``api`` WORD
    is robust to flag ordering without enumerating every persistent flag; a
    quoted flag VALUE that merely contains the text ``api`` is a single distinct
    token, so it does not match.
    """
    return bool(words) and words[0] in {"gh", "glab"} and "api" in words[1:]


def _api_effective_method(words: list[str]) -> str:
    """Return the EFFECTIVE HTTP method gh/glab would send for a ``... api`` call.

    Models the gh (2.87.x) / glab (1.80.x) resolution the merge / review-post
    gates already encode (``hook_router._is_raw_review_write``): a repeated
    ``-X``/``--method`` flag resolves LAST-WINS, so ``-X GET -X POST`` POSTs and
    ``-X POST -X GET`` reads. With no method flag the forge defaults to POST when
    a request-body flag is present (``-f``/``--field``/``--input``/``-d``/…),
    else GET. The returned method is upper-cased; ``GET``/``HEAD`` are reads,
    every other method is a write.

    Both spaced/``=`` (``-X PUT``, ``--method=POST``) and attached
    (``-XPUT``/``-X=POST``) forms are honoured; a quoted value merely containing
    the text ``-X`` stays a single distinct token and cannot spoof the method.
    """
    method: str | None = None
    has_body_flag = False
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in {"-X", "--method"} and i + 1 < n:
            method = words[i + 1]
            i += 2
            continue
        attached = _attached_value(word, "-X") or _attached_value(word, "--method=")
        if attached is not None:
            method = attached
        if word in _API_BODY_FLAGS:
            has_body_flag = True
        i += 1
    if method is not None:
        return method.strip("'\"").upper()
    return "POST" if has_body_flag else "GET"


def segment_is_api_write(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``gh``/``glab api`` call whose method WRITES.

    A read (effective ``GET``/``HEAD``) is NOT a publish surface: ``gh api
    user``, ``gh api repos/o/r/commits/main``, ``gh api … --method GET`` only
    READ and must not be force-classified as a publish (#1530). A call whose
    effective method mutates (``POST``/``PATCH``/``PUT``/``DELETE``/…) hits the
    REST endpoints that publish issue/PR/MR comments, so it stays a publish
    surface the body walkers must scan.
    """
    return segment_is_api_call(words) and _api_effective_method(words) not in _API_READ_METHODS


def segment_is_api_read(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``gh``/``glab api`` call whose method only READS.

    The complement of :func:`segment_is_api_write` over the ``api`` surface: a
    call whose effective method is ``GET``/``HEAD`` (``gh api user``, ``gh api
    repos/o/r/issues --method GET``, ``glab api projects/42/issues``) posts NO
    request body, so it cannot leak content onto a public surface and is not a
    publish the gates must scan or fail-closed on. A non-``api`` segment is
    neither a read nor a write.
    """
    return segment_is_api_call(words) and _api_effective_method(words) in _API_READ_METHODS


def _segment_is_git_commit_publish(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``git [global-flags] commit`` with a body flag.

    A leading ``cd``/``pushd`` navigation prefix and the value-taking ``git``
    global flags (``-C <dir>``, ``--git-dir``, ``--work-tree``, plus ``=`` forms)
    are skipped so ``git -C <dir> commit -m ...`` and
    ``git --git-dir=x commit --message ...`` reach the ``commit`` verb -- the
    contiguous ``git commit -m`` substring broke on the interspersed flag. A
    commit publishes (to public history) only when it carries an inline message /
    file flag; a flagless ``git commit`` is interactive and out of scope here.
    """
    rest = _strip_cd_prefix(words)
    if not rest or rest[0] != "git":
        return False
    i = 1
    while i < len(rest):
        word = rest[i]
        if word in _GIT_GLOBAL_DIR_FLAGS:
            i += 2
            continue
        if any(word.startswith(flag + "=") for flag in _GIT_GLOBAL_DIR_FLAGS):
            i += 1
            continue
        break
    if i >= len(rest) or rest[i] != "commit":
        return False
    return any(_token_is_commit_body_flag(tok) for tok in rest[i + 1 :])


def _token_is_commit_body_flag(token: str) -> bool:
    return token in _GIT_COMMIT_BODY_FLAGS or any(
        token.startswith(prefix) and len(token) > len(prefix) for prefix in _GIT_COMMIT_BODY_ATTACHED
    )


def _strip_cd_prefix(words: list[str]) -> list[str]:
    rest = words
    while rest and rest[0] in {"cd", "pushd"} and len(rest) >= 2:  # noqa: PLR2004
        rest = rest[2:]
    return rest


def _strip_cd_env_prefix(words: list[str]) -> list[str]:
    rest = words
    while rest:
        if _ENV_ASSIGNMENT_RE.fullmatch(rest[0]):
            rest = rest[1:]
            continue
        if rest[0] in {"cd", "pushd"} and len(rest) >= 2:  # noqa: PLR2004
            rest = rest[2:]
            continue
        break
    return rest


def segment_is_opaque_forge_transport(words: list[str]) -> bool:
    """Return True iff ``words`` carries a forge call the body walkers cannot parse.

    A segment is an OPAQUE forge transport when a ``gh``/``glab``/``curl`` token
    (or a command/process-substitution marker) is present but the segment's
    LEADING executable is NOT one of the parseable forge tools -- i.e. the forge
    invocation hides inside an interpreter / wrapper argument (``sh -c "gh ...
    --body X"``, ``eval "..."``, ``ssh host gh ...``, ``xargs gh ...``). The body
    the post carries then sits inside an opaque argument the per-command walkers
    never descend into, so its content (a banned term, a bare ref) cannot be
    scanned. The destination-aware gates inject the fail-closed sentinel for such
    a segment so the unscannable post HARD-BLOCKS rather than slips through
    unread -- mirroring the prove-pure-or-fail-closed inversion.

    A plain ``gh``/``glab``/``git``/``curl`` invocation at ``words[0]`` is NOT
    opaque (the walkers parse its body); a forge-free segment (``git push``,
    ``echo done``) is NOT a transport.
    """
    rest = _strip_cd_env_prefix(words)
    if not rest or rest[0] in _PARSEABLE_FORGE_LEADERS:
        return False
    carries_forge = any(_token_carries_forge_marker(token) for token in rest)
    carries_substitution = any(token_has_substitution_marker(token) for token in rest)
    return carries_forge or carries_substitution


def segment_is_substring_publish(words: list[str]) -> bool:
    """Return True iff ``words`` is a publish by the leader-keyed substring catalogue.

    The segment's own leading executable (after a benign ``cd``/``ENV=`` prefix)
    must equal the leader that owns the matched substring -- so a read-only
    ``grep "glab mr create"`` / ``cat | grep "gh issue create"`` / ``rg "git
    commit -m"`` whose leader is ``grep``/``cat``/``rg`` is NOT a publish even
    though it QUOTES the spelling in an argument. This is the per-segment,
    leader-keyed replacement for the whole-command flattened substring match that
    re-emitted quoted argument contents and produced that false positive. The
    composed per-command form lives in :func:`_command_parser.is_publish_command`,
    which already iterates segments (mirroring :func:`segment_is_api_write` and
    the other per-segment predicates).
    """
    rest = _strip_cd_env_prefix(words)
    if not rest:
        return False
    leader = rest[0]
    joined = " ".join(rest)
    return any(needle in joined for own_leader, needle in _LEADER_PUBLISH_SUBSTRINGS if own_leader == leader)


def command_has_token_aware_publish_surface(command: str) -> bool:
    """Return True iff any segment is a token-aware ``api`` WRITE / ``git commit`` publish.

    The position-aware complement of the contiguous-substring catalogue, used by
    :func:`_command_parser.is_publish_command` to catch the interspersed-flag
    spellings the substring matcher misses. A ``gh``/``glab api`` segment is a
    publish surface only when its EFFECTIVE method writes
    (:func:`segment_is_api_write`); a read-only GET ``api`` call is not a publish
    and must not be force-classified as one (#1530).
    """
    return any(
        segment_is_api_write(words) or _segment_is_git_commit_publish(words) for words in segment_word_lists(command)
    )


def _segment_raws(command: str) -> list[list[str]]:
    """Return each top-level segment's WORD ``raw`` spans, env-prefix stripped.

    Parallel to :func:`segment_word_lists` (same env-prefix stripping), but
    carrying each WORD token's verbatim source span instead of its decoded
    value — so a caller can tell whether a ``$(...)`` marker in a token sits
    inside a single-quoted (inert) span or a live one.
    """
    segments: list[list[str]] = []
    for segment in split_commands(tokenize(command)):
        word_tokens = [tok for tok in segment if tok.kind is TokenKind.WORD]
        while word_tokens and _ENV_ASSIGNMENT_RE.fullmatch(word_tokens[0].value):
            word_tokens = word_tokens[1:]
        if word_tokens:
            segments.append([tok.raw for tok in word_tokens])
    return segments


# Substitution openers bash expands outside single quotes: command
# substitution ``$(...)``, process substitution ``<(...)``/``>(...)``, and the
# legacy backtick command substitution. The two-char ``$(`` family and the
# one-char backtick compose as literal-prefix markers.
_SUBSTITUTION_OPENERS: Final[tuple[str, ...]] = ("$(", "<(", ">(", "`")


def _raw_has_live_substitution(raw: str) -> bool:
    """Return True iff a ``$(`` / ``<(`` / ``>(`` / backtick in ``raw`` is LIVE.

    A substitution is live (bash WOULD run it) only OUTSIDE a single-quoted
    span; inside single quotes it is inert literal text. Delegates to the
    shared quote-aware walker (:func:`raw_substitution_sees_live`), which tracks
    BOTH single- and double-quote context — so an apostrophe inside a
    double-quoted span (``"it's $(gh ...)"``) is a literal character and the
    genuinely LIVE substitution after it is reported live, not mis-classified as
    inert. An empty ``raw`` is treated as live (conservative).
    """
    if not raw:
        return True
    return raw_substitution_sees_live(raw, _SUBSTITUTION_OPENERS)


def _segment_is_opaque_forge_transport_raw(words: list[str], raws: list[str]) -> bool:
    """Raw-aware :func:`segment_is_opaque_forge_transport`.

    A segment is an opaque forge transport when its leader is not a parseable
    forge tool AND it carries either a forge-tool marker OR a LIVE substitution
    (one bash would expand). A substitution that sits entirely inside a
    single-quoted span is inert literal text the gate already holds in the
    decoded value, so it does NOT make the segment opaque — this stops a
    ``t3 review post-comment`` NOTE (or any non-forge segment) that merely
    MENTIONS a ``$(...)`` snippet in its single-quoted body from being treated
    as an unscannable hidden forge call (#1415). The decoded ``words`` drive the
    leader/forge-marker checks; the parallel ``raws`` drive the inert-vs-live
    substitution test.
    """
    rest_words = _strip_cd_env_prefix(words)
    if not rest_words or rest_words[0] in _PARSEABLE_FORGE_LEADERS:
        return False
    skipped = len(words) - len(rest_words)
    rest_raws = raws[skipped:]
    carries_forge = any(_token_carries_forge_marker(token) for token in rest_words)
    carries_live_substitution = any(_raw_has_live_substitution(raw) for raw in rest_raws)
    return carries_forge or carries_live_substitution


def command_has_opaque_forge_transport(command: str) -> bool:
    """Return True iff any segment hides a forge call in an opaque interpreter arg.

    Raw-aware: a substitution marker only makes a non-forge segment opaque when
    it is LIVE (bash would expand it). A single-quoted ``$(...)`` in a body the
    gate already decodes and scans is inert and does not fail the segment closed
    (#1415).
    """
    word_segments = segment_word_lists(command)
    raw_segments = _segment_raws(command)
    return any(starmap(_segment_is_opaque_forge_transport_raw, zip(word_segments, raw_segments, strict=True)))


def _forge_title_value(words: list[str]) -> str | None:
    """Return the ``--title``/``-t`` value of a ``gh``/``glab`` segment.

    Handles space-separated (``--title "x"``), equals (``--title=x``), and
    attached short (``-tx``) forms. ``None`` when the segment carries no title
    flag.
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in {_TITLE_LONG_FLAG, _TITLE_SHORT_FLAG} and i + 1 < n:
            return words[i + 1]
        attached = _attached_value(word, _TITLE_LONG_FLAG + "=")
        if attached is not None:
            return attached
        if word != _TITLE_SHORT_FLAG:
            attached = _attached_value(word, _TITLE_SHORT_FLAG)
            if attached is not None:
                return attached
        i += 1
    return None


def _git_commit_subject(words: list[str]) -> str | None:
    """Return the SUBJECT line of a ``git commit`` segment.

    The subject is the first physical line of the first ``-m``/``--message``
    value (later ``-m`` values are body paragraphs). ``None`` when the segment
    carries no inline message.
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _GIT_COMMIT_MESSAGE_FLAGS and i + 1 < n:
            return words[i + 1].split("\n", 1)[0]
        attached = _attached_value(word, "--message=")
        if attached is not None:
            return attached.split("\n", 1)[0]
        attached = _attached_value(word, "-m")
        if attached is not None:
            return attached.split("\n", 1)[0]
        i += 1
    return None


def extract_title_fragments(command: str) -> list[str]:
    """Return the TITLE / commit-SUBJECT fragments the command publishes.

    A title (``gh``/``glab`` ``--title``) or git-commit subject is a forge
    surface distinct from a description body: the forge auto-links a trailing
    ``(#NNNN)``/``(!NNNN)`` reference there. A gate that wants to treat that
    conventional suffix differently from a body reads these fragments instead of
    the flattened body blob (#1544).
    """
    fragments: list[str] = []
    for words in segment_word_lists(command):
        if words[0] in {"gh", "glab"}:
            title = _forge_title_value(words)
            if title is not None:
                fragments.append(title)
        elif words[0] == "git":
            subject = _git_commit_subject(words)
            if subject is not None:
                fragments.append(subject)
    return fragments
