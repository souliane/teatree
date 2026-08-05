"""A command a reference MANDATES must also be named in its sibling SKILL.md.

``teatree.agents.skill_injection`` resolves ``<dir>/<name>/SKILL.md`` and
concatenates those bodies. ``grep -rn "references/" src/teatree/agents/`` returns
nothing — the module has no concept of a reference file at all. So every
dispatched sub-agent receives the SKILL.md body and nothing else, and a command
moved out to ``references/`` silently stops binding them. ``rules`` is in
``_ALWAYS_FULL_SKILLS``, so a loss there reaches EVERY sub-agent. Three such
losses shipped green on one branch: ``t3 slack react``, ``gh issue view``, and
the whole-file ``--ours`` prohibition.

Scoped two ways, because a reference page is SUPPOSED to be full of commands:

*   only a command inside a MANDATE sentence counts (``must`` / ``never`` /
    ``only`` / ``do NOT`` / ``Non-Negotiable`` / ``forbidden`` / ``always``). A
    troubleshooting recipe listing ``git reset`` as step 2 is mechanics, which is
    exactly what progressive disclosure puts in a reference. A sentence saying
    the agent must never run it is doctrine, and doctrine needs a spine;
*   the token is the runner plus up to two command words, plus the long flag
    IMMEDIATELY following them. Two command words is the doctrine-bearing unit
    for a subcommand (``t3 ticket clear``, ``gh issue view``); a flag is what
    makes an otherwise-generic verb specific, which is the whole difference
    between ``git checkout`` (mentioned everywhere) and ``git checkout --ours``
    (a data-loss prohibition).

The house style writes commands as ``t3 <overlay> <group> <sub>``, so a
whole-word ``<...>``/ellipsis placeholder is SKIPPED rather than collected — the
alternative dropped 27% of the corpus (24 of 89 spans), including the §17.4
keystone itself. Skipping placeholders means the reference's token
(``t3 ticket clear``) no longer spells the spine's text (``t3 <overlay> ticket
clear``), so spine matching is SYMMETRIC: the spine is parsed with the same
parser, over its inline backticks AND its fenced blocks, and a token counts as
present on any of four one-directional clauses. Every clause can only mark a
token PRESENT, never absent, so the widening strictly reduces false positives.

``_ORPHANED_ON_MAIN`` is the set that PREDATES this guard, re-derived
mechanically by running this module's own sweep over the skills tree as it stood
BEFORE the vendor-sync branch landed. The five orphans that branch introduced are
deliberately NOT in it — they are returned to the spines by this change, which is
why ``origin/main`` reports 29 orphans today and this branch reports 24. It is a
shrink-only ratchet, not a suppression: a NEW orphan fails
immediately, ``test_the_baseline_has_not_gone_stale`` fails once an entry is
fixed without being removed, and ``test_the_baseline_can_only_drain`` fails when
a row is ADDED. The list can only drain.
"""

import re
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _REPO_ROOT / "skills"

_RUNNERS = ("t3", "gh", "glab", "git", "prek")
_BACKTICKED_RE = re.compile(r"`([^`\n]{2,120})`")
_MANDATE_RE = re.compile(r"\b(?:must|never|only|non-negotiable|do NOT|always|forbidden|refuse)\b", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*```")
_PLACEHOLDER_RE = re.compile(r"<[^>]*>\S*|\.\.\.|…")
_COMMAND_WORD_RE = re.compile(r"[a-z][a-z0-9_-]*")
_LONG_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]{2,}")

type CommandSignature = tuple[str, tuple[str, ...], str | None]

#: Sub-commands too generic to carry doctrine on their own — the token would be
#: ``git show``, which nearly every skill mentions, so the check would grade noise.
_TOO_GENERIC = frozenset(
    {
        "git add",
        "git log",
        "git show",
        "git diff",
        "git status",
        "git commit",
        "git push",
        "git fetch",
        "git branch",
        "git checkout",
        "git rev-parse",
        "gh api",
        "glab api",
        "t3 doctor",
    }
)

#: Orphaned BEFORE the vendor-sync branch — pre-existing, and out of scope for the
#: change that added this check. The five that branch introduced are absent by
#: design: they are fixed in the spines, not recorded here. SHRINK ONLY: never add
#: a row here to make a new failure go away; return the command to the spine instead.
_ORPHANED_ON_MAIN: frozenset[tuple[str, str]] = frozenset(
    {
        ("skills/platforms/references/gitlab.md", "glab auth token"),
        ("skills/platforms/references/gitlab.md", "glab ci status --branch"),
        ("skills/platforms/references/gitlab.md", "glab mr list"),
        ("skills/platforms/references/gitlab.md", "glab mr note"),
        ("skills/platforms/references/gitlab.md", "glab mr note --help"),
        ("skills/platforms/references/gitlab.md", "glab mr view"),
        ("skills/platforms/references/gitlab.md", "t3 review"),
        ("skills/platforms/references/slack.md", "t3 review-request check"),
        ("skills/platforms/references/slack.md", "t3 review-request check --mr-url"),
        ("skills/platforms/references/slack.md", "t3 slack react"),
        ("skills/retro/references/commit-to-fork.md", "t3 workspace ticket"),
        ("skills/rules/references/publishing-mode-doctrine.md", "glab mr merge"),
        ("skills/setup/references/recommended-automode-authorizations.md", "gh pr merge"),
        ("skills/workspace/references/troubleshooting.md", "gh auth git-credential"),
        ("skills/workspace/references/troubleshooting.md", "gh pr create"),
        ("skills/workspace/references/troubleshooting.md", "git clean"),
        ("skills/workspace/references/troubleshooting.md", "git diff --cached"),
        ("skills/workspace/references/troubleshooting.md", "git merge-base --is-ancestor"),
        ("skills/workspace/references/troubleshooting.md", "git remote set-url"),
        ("skills/workspace/references/troubleshooting.md", "git reset --hard"),
        ("skills/workspace/references/troubleshooting.md", "git restore --staged"),
        ("skills/workspace/references/troubleshooting.md", "git update-index --no-skip-worktree"),
        ("skills/workspace/references/troubleshooting.md", "t3 loops tick --loop"),
        ("skills/workspace/references/troubleshooting.md", "t3 push"),
    }
)

#: The ratchet's real invariant: the list may DRAIN, never GROW. Lower this number
#: when rows are fixed; raising it is the change reviewers must refuse.
_CEILING = 24

#: Anti-vacuity floor on the corpus the parser recognises, not on the baseline.
#: Fixing an orphan moves the command INTO the spine and leaves the reference's
#: mandate in place, so this count does not shrink as the ratchet drains. Measured
#: 56 here and 57 on ``origin/main`` — the difference is the one incidental mention
#: this change de-backticks. A broken glob or a dead mandate regex produces 0.
_MANDATED_SPAN_FLOOR = 40


def _parse_command(span: str) -> CommandSignature | None:
    words = span.strip().strip("$ ").split()
    if len(words) < 2 or words[0] not in _RUNNERS:
        return None

    rest = words[1:]
    command_words: list[str] = []
    cursor = 0
    while cursor < len(rest) and len(command_words) < 2:
        word = rest[cursor]
        if _PLACEHOLDER_RE.fullmatch(word):
            cursor += 1
        elif _COMMAND_WORD_RE.fullmatch(word):
            command_words.append(word)
            cursor += 1
        else:
            break
    if not command_words:
        return None

    trailing = rest[cursor] if cursor < len(rest) else ""
    flag = trailing if _LONG_FLAG_RE.fullmatch(trailing) else None
    if flag is None and len(command_words) == 1 and f"{words[0]} {command_words[0]}" in _TOO_GENERIC:
        return None
    return words[0], tuple(command_words), flag


def _render(signature: CommandSignature) -> str:
    runner, command_words, flag = signature
    base = " ".join((runner, *command_words))
    return f"{base} {flag}" if flag else base


def _spine_spans(body: str) -> Iterator[str]:
    """Inline backticked spans plus whole lines of fenced blocks.

    Fences are load-bearing and measured: ``skills/rules/SKILL.md`` and
    ``skills/ship/SKILL.md`` name commands only inside them, and reading the
    spine without them invents false positives.
    """
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif in_fence:
            yield line
        else:
            yield from _BACKTICKED_RE.findall(line)


def _spine_index(body: str) -> set[CommandSignature]:
    return {signature for span in _spine_spans(body) if (signature := _parse_command(span)) is not None}


def _satisfied_by_spine(signature: CommandSignature, index: set[CommandSignature], body: str) -> bool:
    runner, command_words, _ = signature
    if signature in index:
        return True
    base = " ".join((runner, *command_words))
    if base not in _TOO_GENERIC and any(runner == other and command_words == words for other, words, _ in index):
        return True
    joined = " ".join(command_words)
    if len(command_words) >= 2 and re.search(rf"(?<![\w-]){re.escape(joined)}(?![\w-])", body):
        return True
    return _render(signature) in body


def _mandated_signatures(page: Path) -> set[CommandSignature]:
    signatures: set[CommandSignature] = set()
    for line in page.read_text(encoding="utf-8").splitlines():
        if not _MANDATE_RE.search(line):
            continue
        signatures.update(
            signature for span in _BACKTICKED_RE.findall(line) if (signature := _parse_command(span)) is not None
        )
    return signatures


def _pages(skills_root: Path) -> list[Path]:
    return sorted(skills_root.glob("*/references/*.md"))


def _orphans(skills_root: Path = _SKILLS) -> set[tuple[str, str]]:
    """Every ``(reference page, mandated command)`` whose sibling spine omits it."""
    found: set[tuple[str, str]] = set()
    for page in _pages(skills_root):
        spine = page.parents[1] / "SKILL.md"
        if not spine.is_file():
            continue
        body = spine.read_text(encoding="utf-8")
        index = _spine_index(body)
        rel = page.relative_to(skills_root.parent).as_posix()
        found.update(
            (rel, _render(signature))
            for signature in _mandated_signatures(page)
            if not _satisfied_by_spine(signature, index, body)
        )
    return found


def _mandated_span_count(skills_root: Path = _SKILLS) -> int:
    return sum(len(_mandated_signatures(page)) for page in _pages(skills_root))


def _plant(skills_root: Path, spine_body: str) -> None:
    references = skills_root / "planted" / "references"
    references.mkdir(parents=True)
    (skills_root / "planted" / "SKILL.md").write_text(spine_body, encoding="utf-8")
    (references / "page.md").write_text(
        "You must never run `t3 <overlay> planted-group planted-sub --force`.\n", encoding="utf-8"
    )


def test_the_sweep_actually_reads_reference_pages() -> None:
    # Anti-vacuity: a glob that matched nothing, or a mandate regex that matched
    # nothing, would make every assertion below pass having checked no file.
    assert len(_pages(_SKILLS)) >= 5
    assert _mandated_span_count() >= _MANDATED_SPAN_FLOOR


def test_the_sweep_finds_a_planted_orphan(tmp_path: Path) -> None:
    # The control for the clearing test below: if the parser stops producing
    # tokens at all, this reds before that one's green can mean anything.
    _plant(skills_root := tmp_path / "skills", "# Planted\n\nNothing here names the command.\n")
    assert ("skills/planted/references/page.md", "t3 planted-group planted-sub --force") in _orphans(skills_root)


def test_the_sweep_clears_a_planted_command_named_in_the_spine(tmp_path: Path) -> None:
    _plant(
        skills_root := tmp_path / "skills",
        "# Planted\n\nRun `t3 <overlay> planted-group planted-sub` to do it.\n",
    )
    assert not _orphans(skills_root)


def test_no_new_command_is_mandated_only_in_a_reference() -> None:
    new = sorted(_orphans() - _ORPHANED_ON_MAIN)
    assert not new, (
        "these commands are MANDATED in a reference but appear nowhere in the sibling SKILL.md, so "
        "skill injection reaches no sub-agent with them:\n"
        + "\n".join(f"  {token!r} in {page}" for page, token in new)
        + "\nName the command in the spine; the reference keeps the recipe. Do NOT add it to _ORPHANED_ON_MAIN."
    )


def test_the_baseline_has_not_gone_stale() -> None:
    fixed = sorted(_ORPHANED_ON_MAIN - _orphans())
    assert not fixed, (
        "these baseline rows are no longer orphaned — delete them from _ORPHANED_ON_MAIN so the ratchet "
        "keeps its teeth:\n" + "\n".join(f"  {token!r} in {page}" for page, token in fixed)
    )


def test_the_baseline_can_only_drain() -> None:
    assert len(_ORPHANED_ON_MAIN) <= _CEILING, (
        "a row was ADDED to the orphan baseline. Return the command to the spine instead; "
        "if the addition is genuinely pre-existing on origin/main, say so in review and "
        "lower nothing."
    )
