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

The baseline is the set already orphaned on ``origin/main``. Shrink-only:
``test_the_baseline_has_not_gone_stale`` fails once an entry is fixed without
being removed, so the list can only drain.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = _REPO_ROOT / "skills"

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

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


def _headings(path: Path) -> list[str]:
    return [title.strip() for title in _HEADING_RE.findall(path.read_text(encoding="utf-8"))]


def _orphans() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for page in sorted(_SKILLS.glob("*/references/*.md")):
        spine = page.parents[1] / "SKILL.md"
        if not spine.is_file():
            continue
        spine_titles = set(_headings(spine))
        rel = page.relative_to(_REPO_ROOT).as_posix()
        found.update(
            (rel, title) for title in _headings(page) if "non-negotiable" in title.lower() and title not in spine_titles
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
