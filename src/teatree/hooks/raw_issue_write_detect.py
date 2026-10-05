"""Detect a raw ``gh issue comment`` / ``glab issue note`` — the #162 issue-note bypass.

:mod:`teatree.core.issue_hygiene` is the single application-level issue-write
facade, and the whole point of #162 is that an issue note answers a question no
transport can: WHERE does this text belong? A requirement goes in the description
where a lane reads it; only ``status`` and ``evidence`` stay comments. The facade
also refuses a ticket the owner or factory bot did not file, and scrubs every
outbound through the public-repo leak gate.

``gh issue comment`` typed into Bash reaches the same forge endpoint and answers
none of that — which is how the requirement that started #162 ended up in a
comment nothing would ever read. The sibling :mod:`raw_review_post_detect` already
denies the REST spelling (``gh api .../issues/<n>/notes -X POST``); this leaf is
the CLI half, and it is scoped to the SAME surface: the note.

Deliberately NOT the create / body-edit / close verbs. Those have no complete
sanctioned CLI replacement yet — ``create_or_extend`` needs a judged backlog a bare
CLI cannot supply, the sweep's own fold path edits a host body with
``gh issue edit --body-file``, and a label-only ``--add-label`` edit is not a body
write at all. Denying them would wedge documented workflows with nowhere to go,
which is a worse failure than the bypass it would close.

An invocation, never a mention: the traversal is the shared
:mod:`teatree.hooks.forge_subcommand` leaf, so a ``grep "gh issue comment"`` whose
executed program is ``grep``, a heredoc body, and a ``#`` comment are all not
writes — exactly the over-block class #1415 records.

Both words are found the way gh and glab (cobra CLIs) find them: past every option
word — ``-R o/r``, ``--repo o/r``, ``-R"$R"``, ``--repo="$R"``, ``--`` — where an
option with no glued value takes the next word as its value, and past a redirection
the shell strips (``2>/dev/null``, ``> out``). An option's value is one word; an
unquoted value the shell splits into several is not modelled.

A forge invocation whose subcommand, or whose verb after ``issue``, is a shell
expansion (``gh is$X comment``, ``gh $(printf …) comment``, ``gh "$@"``), or that
reaches them past an option whose NAME is one (``gh -$X o/r …``), is one the shell,
not the text, decides — it cannot be ruled out as a note, so it is refused with a
reason naming that word and the escape: spell the subcommand and verb literally.

Not read (the shared traversal's limits, the same for every forge gate and for the
crash fallback): ``bash -c`` / ``sh -c`` / ``eval`` strings, ``sudo`` and wrappers
outside the traversal's list (``ionice``), a redirection BEFORE the program word, a
program word the shell expands (``"$GH" issue comment``), and user-defined ``gh alias``
/ ``glab alias`` names.
"""

import re

from teatree.hooks.forge_subcommand import basename, forge_program_argvs

_FORGES: frozenset[str] = frozenset({"gh", "glab"})
_ISSUE_SUBCOMMAND = "issue"

#: A parameter, arithmetic or command substitution: the word the shell runs is not the word written.
_EXPANSION_RE = re.compile(r"[$`]")

#: A redirection word (``2>/dev/null``, ``>&2``, ``>``, ``<<<``): the shell removes it — and, when the
#: operator stands alone, the target word after it — before the program sees its argv.
_REDIRECT_RE = re.compile(r"(?:\d+|&)?(?:<<<|<<-?|<>|>>|>&|<&|>\||>|<)")

#: The note verb on each forge (GitHub calls it ``comment``, GitLab ``note``),
#: unioned because a verb the other forge does not own is inert.
_NOTE_VERBS: frozenset[str] = frozenset({"comment", "note"})

DENY_REASON = (
    "BLOCKED: a raw `gh issue comment` / `glab issue note` bypasses the issue-hygiene "
    "facade (#162). The facade decides whether the text belongs in the DESCRIPTION (where "
    "a lane reads it) or a comment, refuses a ticket the owner/factory bot did not file, "
    "and routes the body through the public-repo leak gate and the send-proxy audit — a "
    "direct CLI note skips all three. Use `t3 <overlay> ticket comment <issue-url> "
    "--purpose <purpose> --body '<text>'` (or `--body-file <path>`): a "
    "requirement/change_request/scope_change/decision lands in the description, and only "
    "status/evidence stays a comment. To REMOVE a note use `t3 review delete-issue-note "
    "<repo> <issue-iid> <note-id>`. Read-only `issue view` / `issue list` are unaffected."
)


def undeterminable_reason(program: str, word: str) -> str:
    """The refusal for a forge invocation whose subcommand or verb only the shell decides."""
    name = basename(program)
    return (
        f"BLOCKED: `{name}` reaches its subcommand or verb through `{word}`, a word the shell expands "
        "at run time, so the raw issue-note guard (#162) cannot rule out `gh issue comment` / "
        "`glab issue note`. Not a note? Spell the subcommand and verb literally and put any expanded "
        f'option after them (`{name} pr view 12 --repo "$R"`): that form always passes. A note goes '
        "through `t3 <overlay> ticket comment <issue-url> --purpose <purpose> --body '<text>'`."
    )


def is_raw_issue_write(command: str) -> bool:
    """Whether *command* EXECUTES a raw ``gh``/``glab`` issue-note write, or one the shell may expand into one."""
    return raw_issue_write_deny_reason(command) is not None


def raw_issue_write_deny_reason(command: str) -> str | None:
    """The deny reason for a raw issue note, or ``None`` when allowed."""
    for words in forge_program_argvs(command, _FORGES):
        if reason := _note_deny_reason(words):
            return reason
    return None


def _note_deny_reason(words: list[str]) -> str | None:
    index, subcommand = _positional(words, 1)
    if _EXPANSION_RE.search(subcommand):
        return undeterminable_reason(words[0], subcommand)
    if subcommand != _ISSUE_SUBCOMMAND:
        return None
    _, verb = _positional(words, index + 1)
    if _EXPANSION_RE.search(verb):
        return undeterminable_reason(words[0], verb)
    return DENY_REASON if verb in _NOTE_VERBS else None


def _positional(words: list[str], index: int) -> tuple[int, str]:
    """``(index, word)`` of the first positional word from *index* on, ``(len(words), "")`` when none.

    cobra's own rule: an option with no glued value (``-R``, ``--repo``) takes the next word as its
    value, a glued one (``-Ro/r``, ``--repo=o/r``) does not, and ``--`` ends the options. An option
    whose NAME the shell expands is returned as the positional: nothing says whether it takes a value.
    """
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if redirect := _REDIRECT_RE.match(word):
            index += 2 if redirect.end() == len(word) else 1
            continue
        if not word.startswith("-") or _EXPANSION_RE.search(_option_name(word)):
            return index, word
        index += 1 if word == "-" or len(word) > len(_option_name(word)) else 2
    return (index, words[index]) if index < len(words) else (len(words), "")


def _option_name(word: str) -> str:
    """``--repo`` of ``--repo=o/r``, ``-R`` of ``-Ro/r``; anything after the name is a glued value."""
    return word.partition("=")[0] if word.startswith("--") else word[:2]


__all__ = [
    "DENY_REASON",
    "is_raw_issue_write",
    "raw_issue_write_deny_reason",
    "undeterminable_reason",
]
