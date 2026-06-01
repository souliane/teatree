r"""Hidden-``gh``/``glab``-invocation detection for the publish-surface carve-out.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the module-health LOC cap. This module owns one concern: given a Bash command,
decide whether a ``gh``/``glab`` invocation is hidden from the carve-out's
top-level segment scan -- wrapped in a subshell/procsub/wrapper-word, or buried
inside a STATIC inline-string execution introducer (shell ``-c``, ``env -S`` /
``env --split-string``, here-string ``<shell> <<<``, ``eval``). Any hidden
invocation makes the carve-out fail closed (the gate cannot resolve the hidden
command's target repo, so it must hard-block rather than downgrade).

``command_segments`` (the WORD-value splitter shared with publish_surface) and
the inline-env-assignment regex live here because the hide detection is their
heaviest consumer; publish_surface re-imports both.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize

# A leading ``KEY=value`` token is an inline env assignment, not the
# command name -- bash applies it to the command's environment. Skipped
# so ``FOO=1 git commit`` is still classified as a ``git commit``.
ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# Two-char group/subshell/process-substitution openers a ``gh``/``glab``
# command word can hide behind. The lexer treats ``(``/``)``/``{`` as ordinary
# word chars, so ``$(gh``/``<(gh``/``>(gh``/``=(gh`` arrive as one WORD token
# with the opener attached. Stripped LONGEST-MATCH-FIRST so ``$(`` is consumed
# as a unit, not as a stray ``$`` leaving ``(gh``.
_MULTICHAR_OPENERS: Final[tuple[str, ...]] = ("$(", "<(", ">(", "=(")

# Single-char group/subshell/backtick openers. A bare ``(gh``, ``{gh``, or
# ``\`gh`` token strips down to ``gh``.
_SINGLECHAR_OPENERS: Final[frozenset[str]] = frozenset({"(", "{", "`"})

# Command words counted as a top-level ``gh``/``glab`` invocation.
_GH_GLAB_WORDS: Final[frozenset[str]] = frozenset({"gh", "glab"})

# Substitution markers that introduce a ``gh``/``glab`` command word inside a
# command substitution ``$(gh ...)`` / ``echo $(glab ...)`` or a backtick
# ``\`gh ...\``. These can appear anywhere in a token (incl. wholly inside one
# quoted token, ``--body "$(gh ...)"``) where the count invariant alone misses
# them (the whole substitution is one token), so the whole token value is
# scanned for both ``gh`` and ``glab`` and both the ``$(`` and backtick forms.
_SUBST_MARKERS: Final[tuple[str, ...]] = ("$(gh", "$(glab", "`gh", "`glab")

# Shell interpreters whose ``-c`` argument is a command STRING bash hands to a
# child shell. A ``gh``/``glab`` invocation inside that quoted string is one
# WORD token that does not strip to ``gh``/``glab`` (the count invariant misses
# it) and the segment's own ``words[0]`` is the shell, not gh/glab. The same set
# decides whether a here-string ``<<< "str"`` is fed to a SHELL (which runs it)
# or to a non-shell command (which does not), so a here-string is an execution
# introducer only when its consuming command basename is in this set.
_SHELL_WORDS: Final[frozenset[str]] = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

# The COMPLETE, closed enumeration of STATIC inline-string execution introducers
# this gate recognises -- every shell construct that takes a LITERAL string
# operand and hands it to execution without a runtime variable/command
# resolution step. Each name maps to the function that, given a segment's WORD
# list, yields the literal operand sub-strings that get recursively re-analysed
# (:func:`_child_shell_string_runs_gh_glab`). This single registry is the
# anti-whack-a-mole contract: the recognised set is pinned by a meta-test
# (``test_publish_surface.py::TestExecIntroducerRegistry``), so adding or
# removing an introducer trips the test rather than silently widening or
# narrowing the gate. See :func:`_segment_exec_introducer_operands` for the per-
# introducer extraction and the documented runtime-only residuals.
_EXEC_INTRODUCERS: Final[tuple[str, ...]] = ("shell_c", "env_split_string", "here_string", "eval")

# ``env`` word-splits a single literal string and execs the result. ``-S`` and
# ``--split-string`` are the two spellings of the flag; both the
# space-separated (``-S "str"``) and attached/equals (``-Sstr`` / ``-S"str"`` /
# ``--split-string=str``) operand forms are handled.
_ENV_SPLIT_FLAGS: Final[frozenset[str]] = frozenset({"-S", "--split-string"})

# A here-string operator. ``cmd <<< "str"`` feeds ``str`` on STDIN; a SHELL
# consumer runs it as a script, so the operand is an execution-introducer string
# only when the consuming command basename is in ``_SHELL_WORDS``.
_HERE_STRING_OP: Final[str] = "<<<"


def command_segments(command: str) -> list[list[str]]:
    """Return the WORD-value lists of every ``&&``/``;``/``|``/newline segment.

    Each segment's leading inline env assignments (``FOO=1 gh ...``) are
    stripped, mirroring :func:`publish_surface.is_git_commit_command`, so a
    posting verb behind an env prefix is still seen. Empty segments are dropped.

    The banned-terms SCANNER inspects the WHOLE payload (it finds a term in
    any segment), so the carve-out must inspect every segment too -- a
    posting verb behind a leading ``cd ... &&`` / env-assignment prefix is
    a true command, not noise, and ignoring it over-blocks a legitimate
    private-repo post.
    """
    segments: list[list[str]] = []
    for segment in split_commands(tokenize(command)):
        words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
        while words and ENV_ASSIGNMENT_RE.fullmatch(words[0]):
            words = words[1:]
        if words:
            segments.append(words)
    return segments


def _strip_leading_openers(value: str) -> str:
    r"""Strip a leading run of group/subshell/procsub openers from a WORD token.

    The two-char openers (``$(``, ``<(``, ``>(``, ``=(``) are matched before the
    single-char ones (``(``, ``{``, backtick) so ``$(`` is consumed as a unit,
    not as a stray ``$`` that would leave ``(gh`` un-stripped. The run is
    stripped repeatedly so a doubly-wrapped ``$((gh`` still reduces to ``gh``.

    A bare ``gh`` strips nothing and is returned unchanged; ``(gh``, ``$(gh``,
    ``<(gh``, ``{gh``, ``\`gh`` all reduce to ``gh``.
    """
    while value:
        for opener in _MULTICHAR_OPENERS:
            if value.startswith(opener):
                value = value[len(opener) :]
                break
        else:
            if value[0] in _SINGLECHAR_OPENERS:
                value = value[1:]
                continue
            break
    return value


def _count_top_level_gh_glab_segments(command: str) -> int:
    """Count segments whose ``words[0]`` is EXACTLY ``gh``/``glab`` (no stripping).

    These are the invocations the segment parser recognises and the existing
    ``all(target_private)`` check already evaluates. Leading inline env
    assignments are stripped (mirroring :func:`command_segments`), but NO
    wrapper/env-command word is: ``env FOO=x gh ...``, ``eval gh ...``, and a
    ``( gh ...)`` segment whose ``words[0]`` is the opener do NOT count -- that
    asymmetry is what makes the count invariant fire on a hidden invocation.
    """
    return sum(1 for words in command_segments(command) if words[0] in _GH_GLAB_WORDS)


def _shell_c_operand(words: list[str]) -> str | None:
    r"""Return a shell ``-c`` command-string operand in ``words``, or ``None``.

    A shell token -- one whose ``Path(word).name`` basename is in ``_SHELL_WORDS``
    (``sh``/``bash``/``zsh``/``dash``/``ksh``/``ash``, so path-forms ``/bin/sh``
    and ``/usr/bin/env bash`` match) -- found ANYWHERE in the segment and followed
    by a ``-c``-style flag (any flag token containing ``c`` -- ``-c``, ``-lc``,
    ``-ic``, ``-cx``, ...) hands the NEXT argument as a STATIC command-string to a
    child shell. That argument is the operand returned here.

    Scanning for the shell token ANYWHERE in the segment (not only at ``words[0]``)
    subsumes wrapper words (``timeout``/``nice``/``xargs``/``env``/``command``) and
    the ``find . -exec sh -c "..." \\;`` form (the ``\\;`` terminator lexes as a
    literal argument, keeping the inner ``sh -c`` in the same segment) without
    enumerating them.

    Scoped STRICTLY to the argument immediately following a ``-c`` flag -- a prose
    ``--body "... gh ..."`` token elsewhere in a posting segment is NOT returned,
    so an ordinary private post mentioning the word ``gh`` is not over-blocked.
    """
    for i, word in enumerate(words[:-1]):
        if Path(word).name not in _SHELL_WORDS:
            continue
        for j in range(i + 1, len(words) - 1):
            flag = words[j]
            if flag.startswith("-") and "c" in flag:
                return words[j + 1]
            if not flag.startswith("-"):
                break
    return None


def _env_split_string_operand(words: list[str]) -> str | None:
    r"""Return an ``env -S`` / ``env --split-string`` literal operand, or ``None``.

    ``env`` with ``-S``/``--split-string`` word-splits a SINGLE literal string
    argument and execs the result -- a static inline-string execution introducer,
    NOT a runtime resolution. Recognised forms, the flag found ANYWHERE in the
    segment (so a wrapper word before ``env`` does not hide it):

    - ``env -S "str"`` / ``env --split-string "str"`` -- operand is the next word.
    - ``env -S"str"`` / ``env -Sstr`` -- attached short form; operand is the
        suffix after ``-S``.
    - ``env --split-string=str`` -- equals form; operand is the suffix.

    The ``env`` token need not be ``words[0]`` and need not have its own basename
    checked beyond equality -- ``env`` is the only command that interprets ``-S``
    this way, so a literal ``-S``/``--split-string`` flag token after an ``env``
    word is the trigger.
    """
    for i, word in enumerate(words):
        if Path(word).name != "env":
            continue
        for j in range(i + 1, len(words)):
            flag = words[j]
            if flag in _ENV_SPLIT_FLAGS:
                if j + 1 < len(words):
                    return words[j + 1]
                return None
            if flag.startswith("--split-string="):
                return flag[len("--split-string=") :]
            if flag.startswith("-S") and len(flag) > len("-S"):
                return flag[len("-S") :]
            if not flag.startswith("-"):
                break
    return None


def _here_string_operand(words: list[str]) -> str | None:
    r"""Return a here-string operand fed to a SHELL consumer, or ``None``.

    ``cmd <<< "str"`` feeds ``str`` on STDIN; a SHELL consumer (basename in
    ``_SHELL_WORDS``) runs it as a script, so the string is an execution
    introducer. A non-shell consumer (``cat <<< "str"``, ``grep x <<< "str"``)
    does not execute the string, so this returns ``None`` for it (no over-block).

    The lexer emits ``<<<`` as its own WORD token (space-separated:
    ``bash <<< "str"`` -> operand is the next word) or attached
    (``bash <<<"str"`` -> ``<<<str`` is one token, operand is the suffix). The
    consuming shell must precede the ``<<<`` in the same segment.
    """
    saw_shell = False
    for i, word in enumerate(words):
        if Path(word).name in _SHELL_WORDS:
            saw_shell = True
        if not saw_shell:
            continue
        if word == _HERE_STRING_OP and i + 1 < len(words):
            return words[i + 1]
        if word.startswith(_HERE_STRING_OP) and len(word) > len(_HERE_STRING_OP):
            return word[len(_HERE_STRING_OP) :]
    return None


def _eval_operand(words: list[str]) -> str | None:
    r"""Return the joined ``eval`` operand string, or ``None``.

    ``eval`` concatenates its arguments with single spaces and runs the result as
    a command, so a ``gh``/``glab`` verb hidden inside a single QUOTED ``eval``
    argument (``eval "gh ..."``, ``eval "sh -c 'gh ...'"``) is an execution
    introducer the top-level token scan misses (the quoted argument is one WORD
    token that does not strip to ``gh``/``glab``). Joining every word after the
    ``eval`` token reconstructs the command-string for recursive re-analysis;
    the bare-word form (``eval gh issue create``) is ALSO covered (it rejoins to
    ``gh issue create``), subsuming the count-invariant path for that case.

    The ``eval`` token need not be ``words[0]`` -- a wrapper word can precede it.
    """
    for i, word in enumerate(words):
        if Path(word).name == "eval" and i + 1 < len(words):
            return " ".join(words[i + 1 :])
    return None


# Maps each enumerated introducer name (the pinned ``_EXEC_INTRODUCERS`` set) to
# its per-segment operand extractor. Kept as a dict so the meta-test can pin the
# recognised names to ``_EXEC_INTRODUCERS`` and any drift (a new extractor not
# added to the registry, or a registry name with no extractor) trips the test.
_EXEC_INTRODUCER_EXTRACTORS: Final[dict[str, Callable[[list[str]], str | None]]] = {
    "shell_c": _shell_c_operand,
    "env_split_string": _env_split_string_operand,
    "here_string": _here_string_operand,
    "eval": _eval_operand,
}


def _segment_exec_introducer_operands(words: list[str]) -> list[str]:
    r"""Return every static inline-string execution-introducer operand in ``words``.

    This is the SINGLE auditable place that defines, for one command segment,
    which literal sub-strings get recursively re-analysed via
    :func:`_child_shell_string_runs_gh_glab`. It iterates the closed
    ``_EXEC_INTRODUCERS`` registry; each operand is a literal string the
    construct hands to execution WITHOUT a runtime variable/command resolution
    step, so re-tokenising it is sound.

    Re-tokenisation is scoped STRICTLY to these introducer operands -- never an
    arbitrary token -- so the literal-``gh``-inside-a-``--body`` carve-out holds:
    a public ``gh ... --repo PUBLIC`` appearing literally inside a ``--body``
    string of a real private ``gh`` post is NOT an introducer operand, so it is
    not re-analysed and the private post still DOWNGRADES.

    The COMPLETE closed set of recognised static introducers:

    - **shell ``-c``** (:func:`_shell_c_operand`) -- ``sh``/``bash``/... ``-c
        "str"``; the next arg is a child-shell command-string.
    - **``env -S`` / ``env --split-string``** (:func:`_env_split_string_operand`)
        -- ``env`` statically word-splits ONE literal string and execs it.
    - **here-string** (:func:`_here_string_operand`) -- ``<shell> <<< "str"``;
        the string is fed to a shell on STDIN and run as a script.
    - **``eval``** (:func:`_eval_operand`) -- ``eval`` concatenates its literal
        args and runs them as a command.

    GENUINELY-RUNTIME residuals deliberately NOT entry points (a static gate
    cannot execute the shell to see the verb, and fragile heuristics for them add
    bypass surface without closing them):

    - **Variable indirection:** ``G=gh; "$G" issue create ...`` -- the command
        word is a parameter expansion resolved when the shell runs.
    - **Verb substitution:** ``$(echo gh) issue create ...`` / backtick
        ``\`echo gh\` issue create ...`` -- the verb is the OUTPUT of an inner
        command, not a static token.

    Note: command substitution producing a verb is runtime, but a command
    substitution whose BODY is a literal ``gh``/``glab`` invocation
    (``--body "$(gh ... PUBLIC ...)"``) is caught by the ``$(gh``/backtick
    ``_SUBST_MARKERS`` path in :func:`command_hides_gh_glab`, not here.
    """
    operands: list[str] = []
    for name in _EXEC_INTRODUCERS:
        operand = _EXEC_INTRODUCER_EXTRACTORS[name](words)
        if operand is not None:
            operands.append(operand)
    return operands


def _command_runs_gh_glab_via_introducer(command: str) -> bool:
    """Return True iff any segment of ``command`` hides ``gh``/``glab`` in an introducer.

    For every command segment, each static inline-string execution-introducer
    operand (:func:`_segment_exec_introducer_operands`) is recursively analysed by
    :func:`_child_shell_string_runs_gh_glab`. ANY operand that runs ``gh``/``glab``
    fails closed.
    """
    for words in command_segments(command):
        for operand in _segment_exec_introducer_operands(words):
            if _child_shell_string_runs_gh_glab(operand):
                return True
    return False


def _child_shell_string_runs_gh_glab(command_string: str) -> bool:
    r"""Return True iff an inline-string ``command_string`` runs ``gh``/``glab``.

    The string is a full command an introducer (shell ``-c``, ``env -S``,
    here-string, ``eval``) hands to execution, so EVERY ``gh``/``glab``
    command-word inside it targets an unverifiable surface (the parent gate
    cannot resolve the child's ``--repo``) -- ANY such word is a hidden
    invocation, not just a surplus one. So this fails closed when:

    - any WORD token strips (:func:`_strip_leading_openers`) to ``gh``/``glab``;
    - a substitution marker (``$(gh``/backtick ``gh``) appears in any token; or
    - a nested introducer operand itself runs ``gh``/``glab`` -- via
        :func:`_segment_exec_introducer_operands` per re-tokenized segment -- so
        ``sh -c "sh -c 'gh ...'"`` and ``eval "sh -c 'gh ...'"`` fail closed by
        recursion.

    Unlike :func:`command_hides_gh_glab`'s top-level ``T > R`` count invariant --
    which subtracts the recognised top-level segments whose target the gate DID
    verify -- inside an opaque introducer string no target is verifiable, so the
    threshold is ``T >= 1``, not ``T > R``.
    """
    tokens = [token for token in tokenize(command_string) if token.kind is TokenKind.WORD]
    if any(marker in token.value for token in tokens for marker in _SUBST_MARKERS):
        return True
    if any(_strip_leading_openers(token.value) in _GH_GLAB_WORDS for token in tokens):
        return True
    return _command_runs_gh_glab_via_introducer(command_string)


def command_hides_gh_glab(command: str) -> bool:
    r"""Return True iff a ``gh``/``glab`` invocation is hidden from the segment scan.

    The carve-out's ``all(target_private)`` check only evaluates ``gh``/``glab``
    invocations the segment parser recognises as top-level segments. An
    invocation reached through a subshell ``( gh ...)`` / ``$( gh ...)``, a brace
    group ``{ gh ...}``, a process substitution ``<(gh ...)`` / ``>(gh ...)`` /
    ``=(gh ...)``, a command substitution ``$(gh ...)`` / ``\`gh ...\```, or a
    wrapper word (``eval gh ...``, ``xargs gh ...``, ``env FOO=x gh ...``,
    ``command gh ...``) is NOT a recognised segment, so a PUBLIC-targeting post
    can hide behind a private one. This detects that hiding so the carve-out
    fails closed (caller returns False => hard-block, no downgrade).

    Three structurally-complete checks; any firing means a hidden invocation:

    - **Substring marker:** any WORD token containing ``$(gh``/``$(glab`` or a
        backtick immediately followed by ``gh``/``glab``. This catches the
        in-ONE-token quoted substitution ``--body "$(gh ... PUB ...)"`` that the
        count check alone misses (the whole substitution is one token).
    - **Static inline-string execution introducer:** any segment carrying an
        introducer operand from the closed ``_EXEC_INTRODUCERS`` set -- shell
        ``-c``, ``env -S`` / ``env --split-string``, here-string ``<shell> <<<``,
        or ``eval`` -- whose operand, when re-tokenized, runs ``gh``/``glab``
        (:func:`_command_runs_gh_glab_via_introducer`). The inner verb lives wholly
        inside one literal operand token that does NOT strip to ``gh``/``glab`` (T
        not raised) and the segment's ``words[0]`` is the introducer command, not
        gh/glab (R not raised), so the count invariant alone misses
        ``... && sh -c "gh ... --repo PUBLIC ..."``,
        ``... && env -S "gh ... PUBLIC ..."``,
        ``... && bash <<< "gh ... PUBLIC ..."`` and
        ``... && eval "sh -c 'gh ... PUBLIC ...'"``. Re-tokenization is scoped
        STRICTLY to the introducer operand (:func:`_segment_exec_introducer_operands`),
        never an arbitrary token, so an ordinary private post whose ``--body``
        prose contains the word ``gh`` is not over-blocked.
    - **Count invariant:** ``T > R`` where ``T`` is the number of WORD tokens
        that, after stripping a leading run of opener prefixes
        (:func:`_strip_leading_openers`), equal exactly ``gh``/``glab`` -- every
        ``gh``/``glab`` command-word however it is wrapped -- and ``R`` is the
        number of recognised top-level ``gh``/``glab`` segments
        (:func:`_count_top_level_gh_glab_segments`). More command-words than
        recognised segments means at least one is hidden in a
        wrapper/procsub/quoted-subst the segment parser cannot resolve.

    A single bare private post (``gh issue create --repo PRIV --body "see (gh
    issue 5) and glab notes, cost $5"``) has ``T==R==1`` and no ``$(gh``/backtick
    marker, so it is NOT over-blocked: the quoted prose is one token that strips
    to neither ``gh`` nor ``glab``. A chained READ ``gh`` (``... && gh issue view
    5``) is a recognised segment, so it counts toward both ``T`` and ``R`` and
    does not trip the invariant.

    Accepts a rare exotic over-block where ``gh`` is an option VALUE
    (``--assignee gh`` => ``T==2, R==1`` => fail-closed). Over-block is the SAFE
    failure for a privacy gate; fragile option-value parsing to avoid it is not
    worth the bypass surface it would add.

    Accepted static-analysis limitations -- these resolve the gh/glab verb at
    RUNTIME, so a static gate that cannot execute the shell cannot see them, and
    fragile heuristics for them are deliberately NOT attempted:

    - **Variable indirection:** ``G=gh; "$G" issue create --repo PUBLIC ...`` --
        the command word is a parameter expansion resolved when the shell runs.
    - **Substitution producing the verb:** ``$(echo gh) issue create ...`` or the
        backtick ``\`echo gh\` issue create ...`` -- the verb is the OUTPUT of an
        inner command, not a static token.
    """
    tokens = [token for token in tokenize(command) if token.kind is TokenKind.WORD]
    if any(marker in token.value for token in tokens for marker in _SUBST_MARKERS):
        return True
    if _command_runs_gh_glab_via_introducer(command):
        return True
    total = sum(1 for token in tokens if _strip_leading_openers(token.value) in _GH_GLAB_WORDS)
    return total > _count_top_level_gh_glab_segments(command)
