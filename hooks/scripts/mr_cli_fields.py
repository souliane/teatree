"""``glab mr create``/``update`` title & description extraction for the gate.

Split out of ``hook_router.py`` by concern (module health): every surface an MR
title/description can be SET from — the ``glab mr`` CLI inline / file /
dynamic-value parsing (:func:`extract_cli_mr_fields`), the out-of-band
``glab api``/``gh api`` REST field surface (:func:`extract_api_mr_fields`) —
plus the MR TARGET-repo slug parsing (:func:`extract_mr_target_repo`). Only the
gate handler (``handle_validate_mr_metadata``) stays in the router, which
delegates each surface here.

A bare sibling module (like ``unknown_repo_push_gate``): the router puts its own
dir on ``sys.path`` so ``from mr_cli_fields import …`` resolves both as the live
hook and when imported as ``hooks.scripts.hook_router`` in tests.
"""

import re
import shlex
import stat
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from hooks.scripts.forge_api_detect import API_FIELD_SEPARATOR, _is_api_create_endpoint_write
from hooks.scripts.gate_result import GateSkipped

# The MR-mutation verb itself — ``glab mr create``/``update``. Matched against
# the command with quoted spans and heredoc bodies stripped (see
# :func:`strip_quoted_and_heredoc`) so it fires only on a REAL invocation, not
# on the phrase merely embedded in a ``git commit -m '… glab mr create …'``
# message, a doc string, or a heredoc body.
_MR_OP_RE = re.compile(r"\bglab\s+mr\s+(create|update)\b")
# A heredoc body — ``<<['"]?DELIM['"]?`` up to a line that is just ``DELIM``.
# Stripped FIRST (before quotes) because a quoted delimiter (``<<'PY'``) would
# otherwise be eaten by the quote-stripper and orphan the body.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(?P<delim>\w+)\1.*?^\s*(?P=delim)\b", re.DOTALL | re.MULTILINE)
# Single- and double-quoted argument spans — a ``-m``/``-F`` message, a quoted
# title/description value, any quoted text. Removed for DETECTION only; value
# extraction still runs on the original command.
_SQUOTE_SPAN_RE = re.compile(r"'[^']*'")
_DQUOTE_SPAN_RE = re.compile(r'"[^"]*"')

# Inline ``--title``/``--description`` (single OR double quoted, multi-line via
# DOTALL). The `glab mr` CLI uses ``--title``/``--description``; the long-flag
# value is captured verbatim from the matching opening quote.
_MR_TITLE_FLAG_RE = re.compile(r"""--title[ =]+(['"])(?P<val>.*?)\1""", re.DOTALL)
_MR_DESC_FLAG_RE = re.compile(r"""--description[ =]+(['"])(?P<val>.*?)\1""", re.DOTALL)
# A file-based description flag (``--description-file``/``-F``) is PRESENT even
# when the inline quote-capture fails: used to decide whether to fall back to
# :func:`_read_message_file` rather than pass a falsely-empty description
# through the validator. ``--description`` is included because the inline
# capture failing on it (``--description "$(cat f)"`` / heredoc) still means a
# description WAS intended — re-read it from the resolvable file arg if any.
_MR_DESC_FLAG_PRESENT_RE = re.compile(r"(?:--description-file|--description\b|\s-F\b)")

# Every shape an unreadable file path raises. Only the first is an OSError: a latin-1 file read
# as utf-8 raises UnicodeDecodeError, an unknown `~user` raises RuntimeError, and a NUL byte in
# the path raises ValueError. A raise here is a gate BYPASS rather than a crash — the router
# treats a handler's exception as cannot-evaluate and skips the whole gate.
_UNREADABLE_FILE: Final[tuple[type[Exception], ...]] = (OSError, ValueError, RuntimeError)
# GitLab's own MR-description limit, so the cap can never refuse a body the forge would
# have accepted (GitHub's PR-body cap is 64 KiB).
_MAX_MESSAGE_FILE_BYTES: Final[int] = 1_048_576

# An unexpanded shell construct inside a DOUBLE-quoted value — command
# substitution ``$(…)``, parameter expansion ``${…}``/``$VAR``, or a backtick.
_DYNAMIC_VALUE_RE = re.compile(r"\$[({A-Za-z_]|`")

# The named reasons a recognised MR mutation is nonetheless not evaluated,
# carried on the :class:`GateSkipped` the caller prints.
_DYNAMIC_FIELD_REASON = (
    "the {field} value is an unexpanded shell construct (`$(…)`/`${{…}}`/`$VAR`/backtick) "
    "that only the shell resolves at runtime, so the hook never sees the real text"
)
_UPDATE_SETS_NO_FIELD_REASON = "this `glab mr update` sets neither a title nor a description"
# The file-arg twin of ``_DYNAMIC_FIELD_REASON`` — see ``_dynamic_message_file_arg``.
_DYNAMIC_FILE_ARG_REASON = (
    "the description-file path is an unexpanded shell construct ({field}) that only the "
    "shell resolves at runtime, so the hook cannot read the description it names — this is "
    "NOT an empty description. Write the body to a literal path first, then pass that path "
    "(`--description-file <path>`), if you want the gate to validate it locally"
)

# A help invocation prints usage and mutates nothing, so it carries no metadata to
# govern — and it is the FIRST thing an operator reaches for once this gate has refused
# them, which is exactly when refusing it again is worst. Matched as a whole argument so
# a `--title '--help me'` is untouched.
_HELP_FLAG_RE = re.compile(r"(?:^|\s)(?:--help|-h)(?=\s|$)")

# File-based message arg — the standard multi-line path (#831's shape):
# ``glab mr create --description-file FILE`` / ``-F FILE``. The captured token
# is a path (optionally quoted); a missing/binary file fails open in
# :func:`_read_message_file`. Long flags require a space or ``=`` separator;
# the short ``-F``/``-C`` branch also accepts git's glued form (``-F<path>``).
_MSG_FILE_FLAG_RE = re.compile(
    r"(?:(?:--description-file|--body-file|--file|--description)[ =]+|-[FC][ =]*)"
    r"(?P<quote>['\"]?)(?P<arg>[^'\"\s]+)['\"]?",
)


def strip_quoted_and_heredoc(command: str) -> str:
    """Command with heredoc bodies and quoted spans removed — for verb DETECTION.

    Heredoc bodies first (line-structured, and a quoted delimiter would confuse
    the quote pass), then single- and double-quoted argument spans. What remains
    is the bare command skeleton: a ``glab mr create/update`` here is a real
    invocation, while the same text inside a ``-m`` message, a quoted title, or a
    heredoc body is gone. The residual false-negative — a real create/update fed
    *through* a stripped span (``bash -c "glab mr create …"`` or a heredoc piped
    to a shell) — is rare and backstopped by the remote MR-title CI gate; the
    common case (a commit message / doc / verification script that merely quotes
    the phrase) no longer false-blocks.
    """
    without_squote = _SQUOTE_SPAN_RE.sub(" ", strip_heredoc(command))
    return _DQUOTE_SPAN_RE.sub(" ", without_squote)


def strip_heredoc(command: str) -> str:
    """Command with heredoc bodies removed, quoted spans KEPT.

    The half of the skeleton a caller wants when it must still read INSIDE a
    quoted span — ``foreign_branch_push_gate`` tokenizes ``bash -c '<payload>'``
    and would lose the payload to the quote pass.
    """
    return _HEREDOC_RE.sub(" ", command)


def _looks_dynamic_value(match: "re.Match[str] | None") -> bool:
    """True when a captured ``--title``/``--description`` value is unresolvable.

    An unexpanded shell construct the PreToolUse hook cannot resolve statically.
    The hook sees the raw command BEFORE the shell expands it, so the value
    captured from ``--description "$(cat "$F")"`` is the truncated fragment
    ``$(cat `` (a nested quote ends the non-greedy capture early), not the real
    description. Validating that fragment false-blocks a legitimate dynamic
    description, so it must be skipped — the remote CI gate is the backstop. A
    SINGLE-quoted (or absent) value is literal as captured: a literal ``$`` /
    backtick there is real text and is still validated. ``match.group(1)`` is the
    opening quote char (the shared shape of ``_MR_TITLE_FLAG_RE`` /
    ``_MR_DESC_FLAG_RE``).
    """
    if match is None:
        return False
    if match.group(1) != '"':
        return False
    return bool(_DYNAMIC_VALUE_RE.search(match.group("val")))


def shlex_flag_value(command: str, flag: str) -> tuple[bool, str | None]:
    """Return ``(parsed, value)`` for *flag*'s value via a shlex split of *command*.

    ``parsed`` is ``False`` when the command cannot be shlex-split (unbalanced
    quotes, an unterminated construct) — the caller then keeps the
    truncation-prone regex value, preserving today's behaviour for those
    commands. ``value`` is ``None`` when the flag is absent; otherwise it is the
    flag's value with all internal quoting resolved correctly — a body mixing an
    apostrophe and a double-quoted phrase, single-quote escaping, backslash-
    escaped inner quotes — for both the ``--flag value`` and ``--flag=value``
    spellings. shlex reads the raw command exactly as bash would tokenize it, so
    the value no longer truncates at the first inner quote char (#3300).
    """
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False, None
    prefix = f"{flag}="
    for i, token in enumerate(tokens):
        if token == flag:
            return True, tokens[i + 1] if i + 1 < len(tokens) else ""
        if token.startswith(prefix):
            return True, token[len(prefix) :]
    return True, None


def _resolved_literal_value(command: str, flag: str, regex_match: "re.Match[str]") -> str:
    """Return the full literal value of *flag*, un-truncated via shlex (#3300).

    The caller has already cleared the value as non-dynamic (a literal, not an
    unexpanded ``$(…)``/``$VAR``), so the shlex-resolved argument is the real
    body. Falls back to the regex capture when the command cannot be shlex-split
    or shlex does not see the flag (the regex matched a looser span), so no
    command that parses today changes verdict beyond gaining the full body.
    """
    parsed, value = shlex_flag_value(command, flag)
    if parsed and value is not None:
        return value
    return regex_match.group("val")


def _message_file_arg(command: str) -> str | None:
    """The raw token captured as the file-based message arg, or ``None`` if absent."""
    match = _MSG_FILE_FLAG_RE.search(command)
    return match.group("arg") if match else None


def _dynamic_message_file_arg(command: str) -> str | None:
    """The file-arg token when it is an unexpanded shell construct, else ``None``.

    ``--description-file "$(cat body.md)"`` captures ``$(cat`` as the "filename":
    the hook sees the command BEFORE the shell expands it, and the capture stops
    at the first whitespace inside the substitution. Reading that fails, and the
    old fall-through returned ``""`` — so the gate refused the MR with "MR
    description is empty", a reason that is simply untrue. Detected with the same
    :data:`_DYNAMIC_VALUE_RE` the inline ``--description "$(…)"`` branch uses, so
    both spellings of the same unresolvable body reach the same skip verdict.
    """
    match = _MSG_FILE_FLAG_RE.search(command)
    if match is None:
        return None
    arg = match.group("arg")
    # Shell expansion is inert inside single quotes. Retaining the opening quote
    # from the raw command keeps a literal ``'$BODY.md'`` distinct from the
    # unresolvable ``"$BODY.md"`` and ``$BODY.md`` spellings.
    if match.group("quote") == "'" or not _DYNAMIC_VALUE_RE.search(arg):
        return None
    return arg


def _read_capped_regular_file(path: Path) -> str | None:
    """Text of a REGULAR file no larger than the forge itself accepts, else ``None``.

    ``stat`` comes FIRST because it answers without opening: ``open`` on a FIFO with no
    writer blocks forever and a character device never ends, and a read that outlives the
    router's budget gets the whole process killed — skipping every OTHER gate in the
    chain, not just this one. Reading cap+1 rather than trusting ``st_size`` closes the
    stat-then-open race on a file still growing. Never raises: a raise is the same
    gate BYPASS the caller's ``_UNREADABLE_FILE`` arm exists to prevent.
    """
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return None
        with path.open("rb") as handle:
            raw = handle.read(_MAX_MESSAGE_FILE_BYTES + 1)
        return None if len(raw) > _MAX_MESSAGE_FILE_BYTES else raw.decode("utf-8")
    except _UNREADABLE_FILE:
        return None


def _read_message_file(command: str) -> str | None:
    """Read a file-based message arg (``-F``/``--description-file``/etc.).

    The standard multi-line path is exactly #831's shape. A
    missing/unreadable/binary/non-regular/over-cap file fails open (returns ``None``: no
    scan, no crash) — matching the other t3-shelling hooks' posture of never blocking the
    agent on a broken environment.
    """
    arg = _message_file_arg(command)
    if arg is None:
        return None
    return _read_capped_regular_file(Path(arg))


def _extract_inline_or_file_desc(command: str) -> "str | GateSkipped":
    """Description text from a Bash MR command — inline quote, then file/heredoc.

    Inline ``--description 'x'`` wins. When the flag is present but the inline
    capture is empty (file-based ``-F``/``--description-file``, a heredoc), fall
    back to :func:`_read_message_file` so a multi-line description is actually
    read and validated rather than passed through as a falsely-empty (and
    trivially "valid"-looking) string.

    Returns a :class:`GateSkipped` when the inline value is an unexpanded
    ``$(…)``/``$VAR`` the hook cannot resolve (:func:`_looks_dynamic_value`), and
    likewise when the FILE ARG itself is one (:func:`_dynamic_message_file_arg`)
    — the caller then skips validation entirely and SAYS SO (never-lockout; the
    remote CI gate validates the real, runtime-expanded body). Returns ``""``
    only when a file-based source names a real path that is unreadable — the
    validator then rejects the empty first line, the correct verdict for a
    genuinely empty description.
    """
    inline = _MR_DESC_FLAG_RE.search(command)
    if inline is not None and inline.group("val"):
        if _looks_dynamic_value(inline):
            return GateSkipped(_DYNAMIC_FIELD_REASON.format(field="--description"))
        return _resolved_literal_value(command, "--description", inline)
    if _MR_DESC_FLAG_PRESENT_RE.search(command):
        dynamic_arg = _dynamic_message_file_arg(command)
        if dynamic_arg is not None:
            return GateSkipped(_DYNAMIC_FILE_ARG_REASON.format(field=dynamic_arg))
        from_file = _read_message_file(command)
        if from_file is not None:
            return from_file
    return ""


def extract_cli_mr_fields(command: str) -> "tuple[str, str] | GateSkipped | None":
    """Title/description for a ``glab mr create``/``update`` CLI command.

    Three outcomes, deliberately distinct (the mute-skip class, #1528's sibling):

    ``None`` — NOT an MR mutation at all. Either the command does not actually invoke
    ``glab mr create/update`` (the verb only appears inside a quoted arg or a heredoc
    body, see :func:`strip_quoted_and_heredoc`), or it is a ``--help`` that prints usage
    and changes nothing. The one outcome that is legitimately silent — this gate has no
    opinion on the call.

    :class:`GateSkipped` — it IS an MR mutation, but the gate cannot evaluate it,
    carrying the human-readable reason the caller must print. Either a field is an
    unexpanded ``$(…)``/``${…}``/``$VAR``/backtick the shell only expands at
    runtime (the captured fragment, e.g. ``$(cat ``, is not the real value), or an
    ``update`` sets neither governed field.

    A ``(title, description)`` tuple — validate it.

    For ``update`` only the field(s) the command actually sets are validated: an
    unset field is back-filled with a known-good placeholder so the validator's
    verdict reflects only the field under edit. ``create`` keeps the stricter
    both-fields contract — an empty title/description on a create is exactly the
    bad metadata the gate must catch (#119).
    """
    stripped = strip_quoted_and_heredoc(command)
    op_match = _MR_OP_RE.search(stripped)
    if op_match is None or _HELP_FLAG_RE.search(stripped):
        return None
    operation = op_match.group(1)
    title_match = _MR_TITLE_FLAG_RE.search(command)
    if _looks_dynamic_value(title_match):
        return GateSkipped(_DYNAMIC_FIELD_REASON.format(field="--title"))
    description = _extract_inline_or_file_desc(command)
    if isinstance(description, GateSkipped):
        return description
    title = _resolved_literal_value(command, "--title", title_match) if title_match else ""
    if operation == "update":
        desc_present = bool(_MR_DESC_FLAG_PRESENT_RE.search(command))
        if title_match is None and not desc_present:
            return GateSkipped(_UPDATE_SETS_NO_FIELD_REASON)
        if title_match is None:
            title = description.split("\n", 1)[0]
        elif not desc_present:
            description = f"{title}\n\n## What\n-"
    return title, description


def cli_update_is_title_only(command: str) -> bool:
    """True for a ``glab mr update`` that sets a title but touches NO description (#3254).

    A pure retitle carries no new description content, so the overlay's
    required-section completeness check (``## Configuration`` / ``## Security &
    privacy impact`` …) must not fire on the hook's back-filled placeholder body —
    the existing MR description is not being changed. ``create`` is never
    title-only (both fields are required), and an update that DOES set a
    description is validated in full.
    """
    op = _MR_OP_RE.search(strip_quoted_and_heredoc(command))
    if op is None or op.group(1) != "update":
        return False
    if _MR_DESC_FLAG_PRESENT_RE.search(command):
        return False
    return _MR_TITLE_FLAG_RE.search(command) is not None


# The MR TARGET repo flag — ``-R <slug>`` / ``--repo <slug>`` on ``glab mr``
# (owner/repo, optionally host-qualified). The slug runs to the next whitespace;
# an optional surrounding quote is tolerated.
_MR_TARGET_REPO_FLAG_RE = re.compile(r"""(?:-R|--repo)[ =]+['"]?(?P<slug>[^\s'"]+)['"]?""")
# ``glab api .../projects/<url-encoded-namespace>/merge_requests…`` — the
# namespace is URL-encoded (``acme-group%2Fwidget``); decoded below. A leading
# slash is optional (``glab api projects/…`` vs ``/api/v4/projects/…``).
_GLAB_API_PROJECT_RE = re.compile(r"\bprojects/(?P<ns>[^/\s'\"]+)/merge_requests")
# ``gh api repos/<owner>/<repo>/pulls…`` — the slug is the two path segments
# after ``repos/``.
_GH_API_REPO_RE = re.compile(r"\brepos/(?P<slug>[^/\s'\"]+/[^/\s'\"]+)/pulls")
# A GitHub PR WEB URL operand to ``gh pr merge`` —
# ``https://<host>/<owner>/<repo>/pull/<n>`` yields ``owner/repo``. The ``/pull/``
# path segment matches case-insensitively; the captured slug stays case-preserved.
_GH_WEB_PR_URL_RE = re.compile(r"https?://[^/\s]+/(?P<slug>[^/\s]+/[^/\s]+)/pull/\d+", re.IGNORECASE)
# A GitLab MR WEB URL operand to ``glab mr merge`` —
# ``https://<host>/<namespace…>/-/merge_requests/<n>`` yields the namespace.
# The namespace may span subgroups (multiple path segments) up to the ``/-/``
# separator, so it is captured non-greedily and URL-decoded like the api form.
_GL_WEB_MR_URL_RE = re.compile(r"https?://[^/\s]+/(?P<ns>[^\s]+?)/-/merge_requests/\d+", re.IGNORECASE)


def extract_mr_target_repo(command: str) -> str | None:
    """Return the MR's TARGET repo slug (``owner/repo``), or ``None`` if absent.

    Parses the target from whichever surface the gate watches so the validator
    can be keyed to the MR's target overlay instead of the agent's cwd. The
    ``-R``/``--repo`` flag on ``glab mr`` gives the slug directly; the
    ``glab api .../projects/<ns>/merge_requests…`` namespace is URL-decoded
    (``acme-group%2Fwidget`` → ``acme-group/widget``); a ``gh api
    repos/<owner>/<repo>/pulls…`` path yields the two segments after ``repos/``;
    and a forge WEB URL operand to ``gh pr merge`` / ``glab mr merge``
    (``…/<owner>/<repo>/pull/<n>`` or ``…/<namespace>/-/merge_requests/<n>``)
    yields the owner/repo or namespace.

    ``None`` when no target is parseable — the validator then keeps its
    cwd-keyed resolution (the established never-lockout fallback).
    """
    flag_match = _MR_TARGET_REPO_FLAG_RE.search(command)
    if flag_match:
        return flag_match.group("slug")

    project_match = _GLAB_API_PROJECT_RE.search(command)
    if project_match:
        return unquote(project_match.group("ns"))

    gh_api_match = _GH_API_REPO_RE.search(command)
    if gh_api_match:
        return gh_api_match.group("slug")

    gh_web_match = _GH_WEB_PR_URL_RE.search(command)
    if gh_web_match:
        return gh_web_match.group("slug")

    gl_web_match = _GL_WEB_MR_URL_RE.search(command)
    if gl_web_match:
        return unquote(gl_web_match.group("ns"))

    return None


def merge_target_managed_state(command: str, managed_slugs: list[str]) -> bool | None:
    """Tri-state classification of the command's MR-TARGET repo (#3343).

    Classifies the merge TARGET (parsed by :func:`extract_mr_target_repo`), NOT
    the agent's cwd, so a raw merge form aimed at a managed repo is caught
    regardless of where it runs. ``True`` — a target slug PARSES and matches a
    ``managed_slugs`` signal (managed → the caller BLOCKS). ``False`` — a target
    resolves to a real ``owner/repo`` namespace (contains a ``/``) matching no
    managed signal (a CONFIDENT unmanaged verdict → the caller ALLOWS on the
    target's own evidence, never consulting cwd). ``None`` — NO RESOLVABLE target:
    none parses at all (a bare ``gh pr merge <n>``), OR the parsed target is an
    OPAQUE id the offline slug set cannot classify (a numeric GitLab ``projects/5``
    id, which could well BE a managed repo whose slug is unresolvable offline) →
    the caller falls back to its cwd-keyed classification (never-lockout fail-safe).

    The plain ``bool`` this replaces conflated the last two states: a
    confidently-unmanaged TARGET was indistinguishable from NO target, so it always
    fell through to the cwd rule and a non-git cwd denied a merge the gate never
    meant to block. Mirroring the tri-state ``cwd_teatree_managed_state`` shape
    makes ``None`` expressible (#3343).
    """
    target = extract_mr_target_repo(command)
    if target is None:
        return None
    target_lower = target.strip().lower()
    # Only a namespaced ``owner/repo`` slug is classifiable offline; an opaque id
    # (no ``/``) cannot be confirmed unmanaged, so defer to the cwd fail-safe.
    if "/" not in target_lower:
        return None
    return any(entry in target_lower for entry in managed_slugs)


# REST-API field args set on a ``glab api``/``gh api`` MR/PR write
# (``--field title=…`` / ``-f description=…`` / ``--raw-field …``). The value is matched
# as ONE SHELL WORD — a run of quoted spans and bare characters ending at the next
# whitespace — rather than as an enumeration of the shapes an author might write. The
# enumeration kept missing spellings the shell treats identically, each miss silent: a
# quoted KEY (``-F "description"=JUNK``) matched nothing at all, and a quoted span the
# shell CONCATENATES with what follows (``-F 'title=fix(x): ok'"JUNK"``) matched only the
# compliant prefix — so the gate reported a PASS on text the forge would never store.
# ``body`` is GitHub's PR-description field (``gh api … -f body=…``); it is normalised to
# ``description`` so the overlay validator sees one key.
# The long/short prefix split mirrors ``_MSG_FILE_FLAG_RE``: only the SHORT flag may be
# glued to its value (``-Fkey=value``), because ``--fieldkey=value`` is a spelling neither
# pflag nor cobra accepts. The separator class is ``[\s=]`` — a TAB or a newline separates
# a flag from its value exactly as a space does, and both CLIs tokenise it that way.
#: One piece of a shell word — a quoted span (the capture is its CONTENTS) or a bare run.
#: The same pattern both matches a whole value (repeated below) and splits it back into
#: pieces, so the two readings of one value cannot drift apart.
_SHELL_WORD_PIECE = r"""'([^']*)'|"([^"]*)"|([^\s'"]+)"""
_SHELL_WORD_PIECE_RE = re.compile(_SHELL_WORD_PIECE)
_API_FIELD_RE = re.compile(
    rf"""(?:(?P<longflag>--field|--raw-field){API_FIELD_SEPARATOR}+"""
    rf"""|(?P<shortflag>-[fF]){API_FIELD_SEPARATOR}*)"""
    rf"""(?P<word>(?:{_SHELL_WORD_PIECE})+)""",
)
#: The MR/PR metadata keys this gate governs. ``state_event``, labels and the rest set no
#: text the validator grades, so a command touching only those has nothing to validate.
_API_FIELD_KEYS: Final[frozenset[str]] = frozenset({"title", "description", "body"})


def _unquote_shell_word(word: str) -> str:
    """The value the forge receives — every quoted span joined to its neighbours, unquoted."""
    return "".join(m.group(1) or m.group(2) or m.group(3) or "" for m in _SHELL_WORD_PIECE_RE.finditer(word))


_API_VERB_RE = re.compile(r"\b(?:gh|glab)\s+api\b")
_VALIDATION_FAILED = "MR title/description failed overlay validation."
# Named in full because every condition is one an operator would otherwise discover by trial.
_AT_FILE_LITERAL_NOTE = (
    "NOTE: `--field`/`-F` reads a value opening with `@` as a FILENAME, so what was validated "
    "above is the literal {literals}, not any file's text. `@path` is dereferenced only when the "
    "path is ABSOLUTE, names a REGULAR file, and is at most {cap} bytes; `@-` (stdin) is never "
    "read. Pass an absolute path, or inline the text."
)
# The two flags that dereference `@filename`, per both CLIs' help text: `-F`/`--field` applies the
# magic conversion, `-f`/`--raw-field` adds a static string the forge stores verbatim. Matching is
# case-SENSITIVE — folding `-f` in with `-F` would validate a file the forge never sees.
_AT_FILE_FLAGS: Final[frozenset[str]] = frozenset({"--field", "-F"})


def _at_file_field_text(value: str) -> str:
    """Resolve ``--field``/``-F``'s documented ``@filename`` indirection to the file's text.

    Under those two flags — and ONLY those two, per both CLIs' help text — a value
    opening with ``@`` names a file to read, so the literal ``@path`` is a string the
    forge never receives. Validating it judges the wrong thing in both directions, and
    the dangerous direction is silent: a NON-compliant body sails through, because
    ``@/tmp/body.md`` never looks like a malformed title.

    Two values are returned untouched. A RELATIVE path resolves against this process's
    directory, which is not the one ``glab`` runs in once the command opens with a ``cd``
    — and that one guard also keeps the CLIs' ``@-`` stdin sentinel and a bare ``@``
    literal, since neither is ever absolute. And a path that is unreadable, not a regular
    file, or larger than the forge accepts keeps the literal, which fails loudly rather
    than opening the gate — the read must never RAISE, because the router treats a
    handler's exception as cannot-evaluate and skips the whole gate.
    """
    if not value.startswith("@"):
        return value
    try:
        path = Path(value[1:]).expanduser()
    except _UNREADABLE_FILE:
        return value
    if not path.is_absolute():
        return value
    text = _read_capped_regular_file(path)
    return value if text is None else text


def _api_field_args(command: str) -> list[tuple[str, str, str]]:
    """Every ``(flag, key, raw value)`` the api-field matcher finds, in command order."""
    found: list[tuple[str, str, str]] = []
    for match in _API_FIELD_RE.finditer(command):
        key, assigned, value = _unquote_shell_word(match.group("word")).partition("=")
        if assigned and key in _API_FIELD_KEYS:
            found.append((match.group("longflag") or match.group("shortflag"), key, value))
    return found


def mr_deny_reason(data: dict, validator_text: str) -> str:
    """The gate's refusal text, plus a note for each ``@value`` that was judged as a literal.

    An ``@`` value the indirection did NOT resolve is validated as text, so the refusal
    otherwise quotes a path the operator never meant as a description and names no rule —
    leaving the ``@`` contract to be rediscovered by trial against a gate that refuses
    every attempt.
    """
    reason = (validator_text or "").strip() or _VALIDATION_FAILED
    command = data.get("tool_input", {}).get("command", "") if data.get("tool_name") == "Bash" else ""
    literals = [
        value
        for flag, _key, value in _api_field_args(command)
        if flag in _AT_FILE_FLAGS and value.startswith("@") and _at_file_field_text(value) == value
    ]
    if not literals:
        return reason
    named = ", ".join(f"`{value}`" for value in dict.fromkeys(literals))
    return f"{reason}\n\n{_AT_FILE_LITERAL_NOTE.format(literals=named, cap=_MAX_MESSAGE_FILE_BYTES)}"


def extract_api_mr_fields(command: str) -> tuple[str, str] | None:
    """Title/description for an out-of-band ``glab api``/``gh api`` MR write.

    Closes the gap where a non-compliant title/description reaches GitLab via
    ``glab api --method PUT .../merge_requests/N --field description=…`` (or a
    ``gh api`` POST), entirely outside the ``glab mr create`` surface the gate
    historically watched. Validates ONLY the fields the command actually sets.

    Neither field set (e.g. ``--field state_event=close``): returns ``None`` —
    nothing to validate (never-lockout: a partial state edit must not be
    force-validated against an empty description). Exactly one field set: the
    untouched field is back-filled with the set field's value as a known-good
    placeholder so the verdict reflects ONLY the field under edit. A valid
    ``type(scope): … (ticket_url)`` line is, by the canonical grammar,
    simultaneously a valid title and a valid description first line — so
    mirroring the set field can never inject a spurious failure for the
    untouched field, while a non-compliant edited field is still rejected
    (without this, editing only the description would false-block on
    ``Title is empty.``). Both fields set: validated as a pair, like a create.

    Reuses :func:`_is_api_create_endpoint_write` so a bare ``GET`` read is
    never treated as a write.
    """
    if not _API_VERB_RE.search(command) or not _is_api_create_endpoint_write(command):
        return None
    fields: dict[str, str] = {}
    for flag, key, value in _api_field_args(command):
        text = _at_file_field_text(value) if flag in _AT_FILE_FLAGS else value
        fields["description" if key == "body" else key] = text
    if not fields:
        return None
    title = fields.get("title")
    description = fields.get("description")
    if title is None:
        title = description or ""
    if description is None:
        description = title
    return title, description
