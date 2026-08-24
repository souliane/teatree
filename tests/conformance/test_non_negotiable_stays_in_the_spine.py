"""A ``Non-Negotiable`` HEADING in a reference must also head a section of the spine.

The sibling sweep (``test_reference_is_never_a_doctrines_only_home``) catches a
lost COMMAND. This catches a lost RULE: a whole section carrying the repo's
strongest marker, moved out of a SKILL.md into ``references/``, where skill
injection never sends it. That is how the whole-file ``--ours`` prohibition — a
data-loss guard — came to bind no sub-agent at all, and how the squash-merge
cross-check followed it out on the same branch.

Deliberately NOT a ban on the string. Reference pages mention "Non-Negotiable" in
prose while cross-referencing the spine, which is exactly what progressive
disclosure should look like, and every one of those must stay green. Only a
HEADING is a claim to own the rule, so only an orphaned heading fails.

A matching TITLE is not enough. ``_orphans`` compares heading titles, so gutting
the section under a spine heading while leaving the heading in place restores
nothing and stays green — the same loss, re-inflicted in heading-preserving form.
A spine heading therefore only counts as OWNING the rule when the section under
it (down to the next heading of the same or higher level) is NON-TRIVIAL: enough
prose to carry a rule, and at least one mandate word. A heading followed only by
a pointer link no longer satisfies the check.

The baseline is the set already orphaned on ``origin/main``. Shrink-only:
``test_the_baseline_has_not_gone_stale`` fails once an entry is fixed without
being removed, and ``test_the_baseline_can_only_drain`` fails when a row is
ADDED, so the list can only drain.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _REPO_ROOT / "skills"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_MANDATE_RE = re.compile(r"\b(?:must|never|only|non-negotiable|do NOT|always|forbidden|refuse)\b", re.IGNORECASE)

#: A restored rule needs a body, not just a heading. Below this many non-blank
#: characters the section is a stub or a bare pointer link, which binds nobody.
_MIN_SECTION_CHARS = 120

#: Already orphaned on ``origin/main`` — pre-existing, out of scope for the branch
#: that added this check. SHRINK ONLY: never add a row to silence a new failure.
_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        ("skills/code/references/multi-tenant-development.md", "Decision Gate (Non-Negotiable)"),
        ("skills/contribute/references/upstream-issue.md", "5. User Confirmation (Non-Negotiable)"),
        ("skills/platforms/references/gitlab.md", "Pre-Flight Checks (Non-Negotiable)"),
        ("skills/retro/references/commit-to-fork.md", "Never Work on Main (Non-Negotiable)"),
        ("skills/retro/references/commit-to-fork.md", "Worktree for Retro Commits (Non-Negotiable)"),
    }
)

#: The ratchet's real invariant: the list may DRAIN, never GROW. Lower this number
#: when rows are fixed; raising it is the change reviewers must refuse.
_CEILING = 5


def _headings(path: Path) -> list[str]:
    return [title.strip() for _, title in _HEADING_RE.findall(path.read_text(encoding="utf-8"))]


def _substantive_headings(body: str) -> set[str]:
    """Titles whose own section carries a rule, not just a heading and a pointer."""
    matches = list(_HEADING_RE.finditer(body))
    owning: set[str] = set()
    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = next(
            (later.start() for later in matches[index + 1 :] if len(later.group(1)) <= level),
            len(body),
        )
        section = body[match.end() : end]
        if len(section.replace("\n", "").strip()) >= _MIN_SECTION_CHARS and _MANDATE_RE.search(section):
            owning.add(match.group(2).strip())
    return owning


def _orphans() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for page in sorted(_SKILLS.glob("*/references/*.md")):
        spine = page.parents[1] / "SKILL.md"
        if not spine.is_file():
            continue
        owning = _substantive_headings(spine.read_text(encoding="utf-8"))
        rel = page.relative_to(_REPO_ROOT).as_posix()
        found.update(
            (rel, title) for title in _headings(page) if "non-negotiable" in title.lower() and title not in owning
        )
    return found


def test_prose_mentions_are_not_swept() -> None:
    # The check must tell a HEADING from a cross-reference in prose, or it would
    # red every page that correctly points back at the spine.
    mentioning = {
        page.relative_to(_REPO_ROOT).as_posix()
        for page in _SKILLS.glob("*/references/*.md")
        if "Non-Negotiable" in page.read_text(encoding="utf-8")
    }
    assert len(mentioning) > len({page for page, _ in _orphans()}), (
        "every mention is being read as a heading — the heading regex is too loose"
    )


def test_no_new_non_negotiable_rule_lives_only_in_a_reference() -> None:
    new = sorted(_orphans() - _BASELINE)
    assert not new, (
        "these Non-Negotiable sections head a reference page but no section of the sibling "
        "SKILL.md, so skill injection binds no sub-agent with them:\n"
        + "\n".join(f'  "{title}" in {page}' for page, title in new)
        + "\nReturn the trigger and the verdict to the spine; the reference keeps the mechanics. "
        "Do NOT add it to _BASELINE."
    )


def test_the_baseline_has_not_gone_stale() -> None:
    fixed = sorted(_BASELINE - _orphans())
    assert not fixed, (
        "these baseline rows are no longer orphaned — delete them from _BASELINE so the ratchet "
        "keeps its teeth:\n" + "\n".join(f'  "{title}" in {page}' for page, title in fixed)
    )


def test_the_baseline_can_only_drain() -> None:
    assert len(_BASELINE) <= _CEILING, (
        "a row was ADDED to the orphan baseline. Return the rule's trigger and verdict to the "
        "spine instead; if the addition is genuinely pre-existing on origin/main, say so in "
        "review and lower nothing."
    )
