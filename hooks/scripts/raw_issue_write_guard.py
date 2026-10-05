"""Deny a raw ``gh issue comment`` / ``glab issue note`` at the Bash boundary (#162).

The CLI half of the issue-note bypass the REST gate (``raw_review_post_guard``)
already closes. ``gh issue comment`` reaches the same forge endpoint as the
sanctioned ``t3 <overlay> ticket comment`` and answers none of the questions that
command's facade answers — description or comment, our ticket or a colleague's,
scrubbed or not — which is precisely how a requirement lands in a comment no lane
will ever read.

Narrow by construction: it denies an INVOCATION of the NOTE verb and nothing else.
A read (``issue view`` / ``list``), a create, a body edit (the sweep's own fold
path runs ``gh issue edit --body-file``), a label-only relabel, a different surface
(``pr comment``, ``mr note``), and a read-only tool that merely QUOTES the spelling
all pass — so the gate cannot wedge a session doing unrelated work, which is why it
sits on the never-lockout allowlist as a narrow targeted-command gate.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
the leaf is back-imported lazily.

A detector crash still fails closed, through a deliberately small stdlib reading of
the same rule: it refuses a ``gh``/``glab`` run whose subcommand and verb, found past
the global options, are ``issue`` and a note verb, or which the shell decides
(``gh is$X``, ANSI-C ``gh $'…'``, ``gh $(…)``, ``gh "$@"``). Everything else — a read,
another surface, a mention in a quote, a comment or a heredoc body — is let through
rather than locking the agent out of every Bash call.
"""

import logging
import re
import sys

from hooks.scripts.managed_repo import teatree_src_on_path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("raw_issue_write_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.raw_issue_write_guard", sys.modules[__name__])
logger = logging.getLogger(__name__)
_FORGES = frozenset({"gh", "glab"})
_NOTE_VERBS = frozenset({"comment", "note"})
#: Past one of these, a forge word is the program: the argv wrappers the detector's traversal reads
#: (``env gh …``, ``timeout 30 gh …``), a function head (``function f { gh …; }``) and ``coproc``.
_WRAPPERS = frozenset({"command", "env", "exec", "nohup", "time", "xargs", "timeout", "nice", "stdbuf", "setsid"})
_HEADS = frozenset({"function", "coproc"})
_COMMENT_AFTER = frozenset(" \t\n;&|(")
_KEYWORDS = frozenset({"{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!"})
_ASSIGNMENT = re.compile(r"\w+=")
_EXPANDED = re.compile(r"[$`]")
_REDIRECT = re.compile(r"(?:\d+|&)?(?:<<<|<<-?|<>|>>|>&|<&|>\||>|<)")
_SEGMENT_BREAKS = frozenset(";&|()\n")
_DELIMITER_END = frozenset(" \t\n;&|()<>")


def issue_write_deny_reason(command: str) -> str | None:
    """Return the deny reason for a raw issue note, or ``None`` when allowed."""
    if not command:
        return None
    try:
        with teatree_src_on_path():
            from teatree.hooks import raw_issue_write_detect  # noqa: PLC0415 — lazy src-bootstrap import

            return raw_issue_write_detect.raw_issue_write_deny_reason(command)
    except ImportError:
        return None
    except Exception as exc:
        logger.exception("raw issue-write detection failed")
        word = next(filter(None, map(_deciding_word, _fallback_forge_argvs(command))), None)
        if word is None:
            return None
        return (
            f"Raw issue-write guard failed ({type(exc).__name__}); issue write denied: `{word}` may make this "
            "a raw `gh issue comment` / `glab issue note`. Not a note? Spell the subcommand and verb literally, "
            "any expanded option after them. Escalate the guard failure to the owner."
        )


def _deciding_word(words: list[str]) -> str | None:
    """The word that may make *words* an issue note — a note verb after ``issue``, or an expansion — else ``None``."""
    index = _past_options(words, 1)
    subcommand = words[index] if index < len(words) else ""
    if _EXPANDED.search(subcommand):
        return subcommand
    if subcommand not in {"issue", "issues"}:
        return None
    index = _past_options(words, index + 1)
    verb = words[index] if index < len(words) else ""
    return verb if _EXPANDED.search(verb) or verb in _NOTE_VERBS else None


def _past_options(words: list[str], index: int) -> int:
    """The index past the options and redirections from *index*: cobra's rule, an unglued option takes the next word."""
    while index < len(words):
        word = words[index]
        if word == "--":
            return index + 1
        redirect = _REDIRECT.match(word)
        name = word.partition("=")[0] if word.startswith("--") else word[:2]
        if redirect is None and (not word.startswith("-") or _EXPANDED.search(name)):
            return index
        glued = word == "-" or len(word) > (redirect.end() if redirect else len(name))
        index += 1 if glued else 2
    return index


def _fallback_forge_argvs(command: str) -> list[list[str]]:
    """The argv of every ``gh``/``glab`` the command runs at a command position, a substitution's included."""
    argvs: list[list[str]] = []
    for words in _segments(command.replace("\\\n", "")):
        start = next((i for i, word in enumerate(words) if not (_ASSIGNMENT.match(word) or word in _KEYWORDS)), None)
        if start is None:
            continue
        programs = range(start + 1, len(words)) if _basename(words[start]) in _WRAPPERS | _HEADS else (start,)
        argvs.extend(words[i:] for i in programs if _basename(words[i]) in _FORGES)
    return argvs


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _segments(text: str) -> list[list[str]]:
    """Command segments as the shell splits them: quotes and backslashes removed, an expansion kept as written.

    A heredoc body is text fed to a program: dropped, except that an unquoted delimiter's body still runs its
    substitutions (its quotes are plain characters there).
    """
    segments: list[list[str]] = [[]]
    bodies: list[str] = []
    heredocs: list[tuple[str, bool, bool]] = []
    word: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if (head := _heredoc_head(text, index)) is not None:
            heredocs.append(head[0])
            index = head[1]
        elif char in " \t" or (char in _SEGMENT_BREAKS and not _in_redirection(text, index, word or "")):
            if word is not None:
                segments[-1].append(word)
                word = None
            if char in _SEGMENT_BREAKS:
                segments.append([])
            index += 1
            if char == "\n" and heredocs:
                index = _heredoc_bodies(text, index, heredocs, bodies)
                heredocs = []
        elif char == "#" and word is None and (index == 0 or text[index - 1] in _COMMENT_AFTER):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        else:
            part, index = _word_part(text, index, bodies)
            word = (word or "") + part
    if word is not None:
        segments[-1].append(word)
    for body in bodies:
        segments.extend(_segments(body))
    return [segment for segment in segments if segment]


def _heredoc_head(text: str, index: int) -> tuple[tuple[str, bool, bool], int] | None:
    """``((delimiter, strip_tabs, quoted), index after it)`` for a ``<<`` (not ``<<<``) at *index*, else ``None``."""
    if not text.startswith("<<", index) or text.startswith("<<<", index) or text[index - 1 : index] == "<":
        return None
    cursor = index + 2 + text.startswith("-", index + 2)
    while text[cursor : cursor + 1] in {" ", "\t"}:
        cursor += 1
    start = cursor
    while cursor < len(text) and text[cursor] not in _DELIMITER_END:
        if text[cursor] in "'\"":
            close = text.find(text[cursor], cursor + 1)
            cursor = len(text) if close < 0 else close + 1
        else:
            cursor += 2 if text[cursor] == "\\" else 1
    spelled = text[start:cursor]
    delimiter = spelled.translate(str.maketrans("", "", "'\"\\"))
    return ((delimiter, text.startswith("-", index + 2), spelled != delimiter), cursor) if delimiter else None


def _heredoc_bodies(text: str, index: int, heredocs: list[tuple[str, bool, bool]], bodies: list[str]) -> int:
    """Read each pending heredoc body from *index*; an unquoted one's substitutions run. The index after them."""
    for delimiter, strip_tabs, quoted in heredocs:
        start = index
        while index < len(text):
            newline = text.find("\n", index)
            line = text[index : len(text) if newline < 0 else newline]
            index = len(text) if newline < 0 else newline + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
        cursor = start
        while not quoted and cursor < index:
            if text.startswith(("$(", "`"), cursor):
                end = _span_end(text, cursor)
                bodies.append(text[cursor + (1 if text[cursor] == "`" else 2) : end - 1])
                cursor = end
            else:
                cursor += 2 if text[cursor] == "\\" else 1
    return index


def _in_redirection(text: str, index: int, word: str) -> bool:
    """Whether the ``&`` or ``|`` at *index* is part of a redirection (``2>&1``, ``&>f``, ``>|f``), not a break."""
    return (text[index] in "&|" and word.endswith((">", "<"))) or text.startswith("&>", index)


def _word_part(text: str, index: int, bodies: list[str]) -> tuple[str, int]:
    """The text one word part contributes and the index after it; a substitution's body is added to *bodies*."""
    char = text[index]
    if text.startswith(("$(", "`"), index):
        end = _span_end(text, index)
        bodies.append(text[index + (1 if char == "`" else 2) : end - 1])
        return text[index:end], end
    if text.startswith("$'", index):
        end = _span_end(text, index)
        return text[index:end], end
    if char == "'":
        close = text.find("'", index + 1)
        end = len(text) if close < 0 else close
        return text[index + 1 : end], end + 1
    if char == '"':
        return _double_quoted(text, index + 1, bodies)
    if char == "\\":
        return text[index + 1 : index + 2], index + 2
    return char, index + 1


def _double_quoted(text: str, index: int, bodies: list[str]) -> tuple[str, int]:
    """The content of a double-quoted span opening before *index*, and the index past its closing quote."""
    content: list[str] = []
    while index < len(text) and text[index] != '"':
        if text[index] == "\\" and text[index + 1 : index + 2] in {"\\", '"', "$", "`"}:
            content.append(text[index + 1])
            index += 2
        elif text.startswith(("$(", "`"), index):
            part, index = _word_part(text, index, bodies)
            content.append(part)
        else:
            content.append(text[index])
            index += 1
    return "".join(content), index + 1


def _span_end(text: str, index: int) -> int:
    """The index past a ``$(…)``, backtick or ANSI-C ``$'…'`` span opening at *index* (quotes and escapes respected)."""
    if text[index] == "`" or text.startswith("$'", index):
        closer = text[index] if text[index] == "`" else "'"
        cursor = index + (1 if closer == "`" else 2)
        while cursor < len(text) and text[cursor] != closer:
            cursor += 2 if text[cursor] == "\\" else 1
        return min(cursor + 1, len(text))
    depth, cursor = 1, index + 2
    while cursor < len(text) and depth:
        char = text[cursor]
        if char in "'\"":
            close = text.find(char, cursor + 1)
            cursor = len(text) if close < 0 else close
        elif char == "\\":
            cursor += 1
        elif char in "()":
            depth += 1 if char == "(" else -1
        cursor += 1
    return cursor


def handle_block_raw_issue_write(data: dict) -> bool:
    """Deny a Bash ``gh issue comment`` / ``glab issue note``; True when a deny was emitted."""
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 — deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    reason = issue_write_deny_reason(data.get("tool_input", {}).get("command", ""))
    if reason is None:
        return False
    return emit_pretooluse_deny(reason, gate_id="raw_issue_write")
