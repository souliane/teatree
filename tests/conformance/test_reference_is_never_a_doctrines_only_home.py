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
*   the token is the first two words, plus the first long-form flag when the span
    carries one. Two words is the doctrine-bearing unit for a subcommand
    (``t3 slack``, ``gh issue``); a flag is what makes an otherwise-generic verb
    specific, which is the whole difference between ``git checkout`` (mentioned
    everywhere) and ``git checkout --ours`` (a data-loss prohibition).

The baseline below is the set already orphaned on ``origin/main``. It is a
shrink-only ratchet, not a suppression: a NEW orphan fails immediately, and
``test_the_baseline_has_not_gone_stale`` fails once an entry is fixed without
being removed, so the list can only drain.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _REPO_ROOT / "skills"

_RUNNERS = ("t3", "gh", "glab", "git", "prek")
_BACKTICKED_RE = re.compile(r"`([^`\n]{2,120})`")
_MANDATE_RE = re.compile(r"\b(?:must|never|only|non-negotiable|do NOT|always|forbidden|refuse)\b", re.IGNORECASE)
_LONG_FLAG_RE = re.compile(r"(--[a-z][a-z0-9-]{2,})")

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

#: Already orphaned on ``origin/main`` — pre-existing, and out of scope for the
#: branch that added this check. SHRINK ONLY: never add a row here to make a new
#: failure go away; return the command to the spine instead.
_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        ("skills/platforms/references/gitlab.md", "glab auth"),
        ("skills/platforms/references/gitlab.md", "glab mr"),
        ("skills/platforms/references/gitlab.md", "glab mr --help"),
        ("skills/platforms/references/gitlab.md", "glab ci --branch"),
        ("skills/platforms/references/gitlab.md", "t3 review"),
        ("skills/platforms/references/slack.md", "t3 slack"),
        ("skills/platforms/references/slack.md", "t3 review-request"),
        ("skills/platforms/references/slack.md", "t3 review-request --mr-url"),
        ("skills/retro/references/privacy-scan.md", "git diff --cached"),
        ("skills/rules/references/on-behalf-posting.md", "t3 review --approver"),
        ("skills/setup/references/recommended-automode-authorizations.md", "gh pr"),
        # Incidental prose, not a mandate: the sentence prohibits `git rebase -i`
        # (which the spine does carry) and only names this while explaining why.
        ("skills/ship/references/merge-and-history-mechanics.md", "git log --oneline"),
        ("skills/workspace/references/troubleshooting.md", "t3 teatree"),
        ("skills/workspace/references/troubleshooting.md", "t3 push"),
        ("skills/workspace/references/troubleshooting.md", "t3 loops --loop"),
        ("skills/workspace/references/troubleshooting.md", "gh auth"),
        ("skills/workspace/references/troubleshooting.md", "git update-index --no-skip-worktree"),
        ("skills/workspace/references/troubleshooting.md", "git remote"),
        ("skills/workspace/references/troubleshooting.md", "git clean"),
        ("skills/workspace/references/troubleshooting.md", "git reset --hard"),
        ("skills/workspace/references/troubleshooting.md", "git restore --staged"),
        ("skills/workspace/references/troubleshooting.md", "git diff --cached"),
        ("skills/workspace/references/troubleshooting.md", "git merge-base --is-ancestor"),
    }
)


def _command_token(span: str) -> str | None:
    words = span.strip().strip("$ ").split()
    if len(words) < 2 or words[0] not in _RUNNERS:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9-]*", words[1]):
        return None
    base = f"{words[0]} {words[1]}"
    if (flag := _LONG_FLAG_RE.search(span)) is not None:
        # A flag makes a generic verb specific, so it is never dropped as noise.
        return f"{base} {flag.group(1)}"
    return None if base in _TOO_GENERIC else base


def _mandated_tokens(page: Path) -> set[str]:
    tokens: set[str] = set()
    for line in page.read_text(encoding="utf-8").splitlines():
        if not _MANDATE_RE.search(line):
            continue
        tokens.update(token for span in _BACKTICKED_RE.findall(line) if (token := _command_token(span)) is not None)
    return tokens


def _orphans() -> set[tuple[str, str]]:
    """Every ``(reference page, mandated command)`` whose sibling spine omits it."""
    found: set[tuple[str, str]] = set()
    for page in sorted(_SKILLS.glob("*/references/*.md")):
        spine = page.parents[1] / "SKILL.md"
        if not spine.is_file():
            continue
        body = spine.read_text(encoding="utf-8")
        rel = page.relative_to(_REPO_ROOT).as_posix()
        found.update((rel, token) for token in _mandated_tokens(page) if token not in body)
    return found


def test_the_sweep_actually_reads_reference_pages() -> None:
    # Anti-vacuity: a glob that matched nothing, or a mandate regex that matched
    # nothing, would make every assertion below pass having checked no file.
    assert len(list(_SKILLS.glob("*/references/*.md"))) >= 5
    assert len(_BASELINE) >= 10


def test_no_new_command_is_mandated_only_in_a_reference() -> None:
    new = sorted(_orphans() - _BASELINE)
    assert not new, (
        "these commands are MANDATED in a reference but appear nowhere in the sibling SKILL.md, so "
        "skill injection reaches no sub-agent with them:\n"
        + "\n".join(f"  {token!r} in {page}" for page, token in new)
        + "\nName the command in the spine; the reference keeps the recipe. Do NOT add it to _BASELINE."
    )


def test_the_baseline_has_not_gone_stale() -> None:
    fixed = sorted(_BASELINE - _orphans())
    assert not fixed, (
        "these baseline rows are no longer orphaned — delete them from _BASELINE so the ratchet "
        "keeps its teeth:\n" + "\n".join(f"  {token!r} in {page}" for page, token in fixed)
    )
