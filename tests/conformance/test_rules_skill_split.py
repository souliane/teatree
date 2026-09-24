"""The rules skill is a small always-embedded core that names every reference it sheds.

``rules`` is embedded in full on every lifecycle dispatch, so its size is paid by
every phase. The core keeps each rule's trigger and verdict and names the
reference file holding the full text; the reference files hold every original
section verbatim. These tests pin that no section is lost, that every reference
is reachable from the core, that the core stays under its byte ceiling, and that
every ``§ "<title>"`` citation of the rules skill still resolves.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES = _REPO_ROOT / "skills" / "rules"
_CORE = _RULES / "SKILL.md"

_CORE_MAX_BYTES = 18_432

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^(#{2,3}) +(.+?)\s*$")
_BOLD_LEAD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_CITATION_RE = re.compile(r"(?:t3:rules|rules/SKILL\.md)[`)\]\s(.a-zA-Z/:-]{0,40}?§\s*[\"“']([^\"”'\n]+)")

#: Every H2/H3 of skills/rules/SKILL.md at 7c2955c4, before the split.
_ORIGINAL_HEADINGS: tuple[str, ...] = (
    "Index",
    "Invoke Skills Before ANY Response",
    "Verification Before Completion (Non-Negotiable)",
    "A Diagnosis Cites What Was Read (Non-Negotiable)",
    "An Acceptance Criterion That Cannot Fail Is Not a Criterion (Non-Negotiable)",
    "A Dispatch on a Closed Issue Halts and Asks (Non-Negotiable)",
    "Grep Before Claiming Cross-Reference Coverage (Non-Negotiable)",
    "User Instructions Are Priority 1",
    "On an Ambiguous Directive, Take the Non-Destructive Reading (Non-Negotiable)",
    "Classifier Denial Protocol (Non-Negotiable)",
    "Anticipate a Predictable Gate: Offer Enable-Setting or Approve-Once, Never Bypass-or-DIY (Non-Negotiable)",
    "Re-Derive the Minimal Blocker",
    "External Read Failure Must Fail Loud, Never Silent-Empty (Non-Negotiable)",
    "Read the Canonical Source Before Fixing a Conformance Bug",
    "Re-Verify Cross-Agent State Before Reporting a Dependent Request",
    "Lead a Completion Report With the Assigned-Work Status",
    "Keep Turn Output Terse and TTS-Ready",
    "Context Transparency",
    "Clickable References",
    "Render the Title Inline, Never a Bare/Link-Only Id (Non-Negotiable)",
    "ID Namespace Disambiguation (Non-Negotiable)",
    "Read Secrets From the Secret Store (Non-Negotiable)",
    "Read the Canonical Source Before a Structural Action (Non-Negotiable)",
    "Overlay Skills Are Scoped to Overlay Repos (Non-Negotiable)",
    "Token Extraction",
    "Temp File Safety",
    "Complex API Payloads: Use curl or Python",
    "Never Pipe, Redirect, or Chain a gh/glab Publish Command",
    "Preserve Existing UX Patterns",
    "No AI Signature on Posts Made on the User's Behalf (Non-Negotiable)",
    "Ask Before Posting on the User's Behalf (Non-Negotiable)",
    "Never Post PR Comments from Parallel Agents (Non-Negotiable)",
    "Evidence Comes From the Deployed Environment (Non-Negotiable)",
    "Never Modify a Remote Database Without Explicit User Approval (Non-Negotiable)",
    "Verify Repo Visibility Before Filing External Issues (Non-Negotiable)",
    "Self-Apply `needs-triage` on Agent-Filed Issues (Non-Negotiable)",
    "A Filed Issue Separates OBSERVED From INFERRED (Non-Negotiable)",
    "Leak Remediation — Silent Scrubs (Non-Negotiable)",
    "Public-Repo Commit Author Identity (Non-Negotiable)",
    "Sub-Agent Limitations",
    "Prefer Native Tool APIs Over Filesystem Heuristics",
    "Prefer the Teatree MCP Tools Over the `t3` CLI",
    "Symlink Safety",
    "Read Before Overwriting a Tracked Config/Dotfile (Non-Negotiable)",
    "Never Cron a `t3 loop` Command From a Session (Non-Negotiable)",
    "Shell Alias Safety",
    "Shell Probes Run Under zsh — a Probe Without a Control Is Unfalsifiable",
    "Skill File Writes Require a Git Repo",
    "Fix TeaTree/Skill Bugs Immediately",
    "Teatree Extension Point Changes Must Update All Registered Overlays (Non-Negotiable)",
    'Do Work Now, Don\'t Defer to "Later" Tickets (Non-Negotiable)',
    "Contribute Mode: Promote Findings to Skills, Not Personal Memory (Non-Negotiable)",
    "Autonomous Directive Adoption",
    "Ask About Auth Before External Service Integrations",
    "Never Change PR Base Branch or Dependencies (Non-Negotiable)",
    "Fewest PRs for Related Work — Splitting Requires Approval (Non-Negotiable)",
    "Always Create Tasks",
    "Mid-Task Interrupts (Non-Negotiable)",
    "Background Long Operations (Non-Negotiable)",
    "Always Use AskUserQuestion for Questions",
    "The User Asked a Question — Answer It (Non-Negotiable)",
    "Never Introduce Tech Debt; Reduce It (Non-Negotiable)",
    "Publishing Actions Are Mode-Conditional (Non-Negotiable)",
    "Always-Gated Actions (Non-Negotiable, both modes)",
    "Three Orthogonal Repo Axes — Visibility, Ownership, Collaboration (Non-Negotiable)",
    "Run Retro Before Ending Non-Trivial Sessions",
    "Verify Imports Before Applying External Code",
    "Context Longevity",
    "Commit Before Declaring Done (Non-Negotiable)",
    "Pre-Commit Hook Failures on Unrelated Tests",
    "Worktree-First Work (Non-Negotiable)",
    "Concurrent Agent Safety (Non-Negotiable)",
    "Deprecated Code",
    "GitLab Inline Comments",
    "Prefer Standard Over Clever",
    "Split Long Skills With Progressive Disclosure",
    "Session Scope Management",
    "Skill Auto-Loading Must Work",
    "Escalate Honesty-Critical Verification to the Most-Honest Model",
    "Re-Validate a Reused Guard in a New Destructive Context",
)

_CITING_GLOBS = (
    "skills/**/*.md",
    "agents/*.md",
    "hooks/**/*.py",
    "src/**/*.py",
    "scripts/**/*.py",
    "docs/**/*.md",
    "BLUEPRINT.md",
    "AGENTS.md",
    "CLAUDE.md",
)

_MIN_CITATIONS = 50


def _unfenced_lines(text: str) -> Iterator[str]:
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            yield line


def _headings(text: str) -> list[str]:
    return [match[2] for line in _unfenced_lines(text) if (match := _HEADING_RE.match(line))]


def _rule_pages() -> list[Path]:
    return [_CORE, *sorted((_RULES / "references").glob("*.md"))]


def _anchors(pages: list[Path]) -> set[str]:
    anchors: set[str] = set()
    for page in pages:
        text = page.read_text(encoding="utf-8")
        anchors.update(_headings(text))
        anchors.update(match.strip() for line in _unfenced_lines(text) for match in _BOLD_LEAD_RE.findall(line))
    return anchors


def _citations(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for pattern in _CITING_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            for match in _CITATION_RE.finditer(path.read_text(encoding="utf-8")):
                title = match[1].rstrip("\\").strip()
                if title:
                    found.append((rel, title))
    return found


def _unresolved(citations: list[tuple[str, str]], anchors: set[str]) -> list[tuple[str, str]]:
    lowered = [anchor.lower() for anchor in anchors]
    return [(path, title) for path, title in citations if not any(a.startswith(title.lower()) for a in lowered)]


def test_every_original_section_survives_as_a_heading() -> None:
    present = {heading for page in _rule_pages() for heading in _headings(page.read_text(encoding="utf-8"))}
    missing = [title for title in _ORIGINAL_HEADINGS if title not in present]
    assert not missing, f"sections lost in the split: {missing}"


def test_every_reference_file_is_named_by_path_in_the_core() -> None:
    core = _CORE.read_text(encoding="utf-8")
    unnamed = [
        page.relative_to(_REPO_ROOT).as_posix()
        for page in sorted((_RULES / "references").glob("*.md"))
        if page.relative_to(_REPO_ROOT).as_posix() not in core
    ]
    assert not unnamed, f"reference files the core never names: {unnamed}"


def test_the_core_fits_its_byte_ceiling() -> None:
    size = len(_CORE.read_text(encoding="utf-8").encode())
    assert size <= _CORE_MAX_BYTES, f"skills/rules/SKILL.md is {size} B, over the {_CORE_MAX_BYTES} B core ceiling"


def test_the_core_names_every_original_section() -> None:
    core = _CORE.read_text(encoding="utf-8")
    unnamed = [title for title in _ORIGINAL_HEADINGS if title not in core]
    assert not unnamed, f"sections a core-only reader has no cue to Read: {unnamed}"


def _broken_links(pages: list[Path]) -> list[str]:
    broken: list[str] = []
    for page in pages:
        for line in _unfenced_lines(page.read_text(encoding="utf-8")):
            for target in _LINK_RE.findall(_INLINE_CODE_RE.sub("", line)):
                path = target.split("#", 1)[0]
                if not path or "://" in target or target.startswith("mailto:"):
                    continue
                if not (page.parent / path).exists():
                    broken.append(f"{page.relative_to(_REPO_ROOT).as_posix()}: {target}")
    return broken


def test_every_relative_link_under_skills_rules_resolves() -> None:
    broken = _broken_links(sorted(_RULES.rglob("*.md")))
    assert not broken, "relative links that resolve to nothing:\n" + "\n".join(f"  {link}" for link in broken)


def test_a_planted_broken_link_is_reported(tmp_path: Path) -> None:
    (tmp_path / "references").mkdir()
    page = tmp_path / "references" / "page.md"
    page.write_text(
        "[ok](page.md) [bad](references/page.md) [web](https://x.y/z) [anchor](#top) `[code](url)`\n", encoding="utf-8"
    )
    with patch.object(sys.modules[__name__], "_REPO_ROOT", tmp_path):
        assert _broken_links([page]) == ["references/page.md: references/page.md"]


def test_every_rules_citation_resolves() -> None:
    citations = _citations(_REPO_ROOT)
    assert len(citations) >= _MIN_CITATIONS, f"only {len(citations)} citations collected — the regex went blind"
    broken = _unresolved(citations, _anchors(_rule_pages()))
    assert not broken, "rules-skill citations naming no heading or bold lead-in:\n" + "\n".join(
        f'  {path}: § "{title}"' for path, title in broken
    )


def test_a_planted_broken_citation_is_reported(tmp_path: Path) -> None:
    (tmp_path / "BLUEPRINT.md").write_text('See `/t3:rules` § "No Such Rule" for it.\n', encoding="utf-8")
    citations = _citations(tmp_path)
    assert citations == [("BLUEPRINT.md", "No Such Rule")]
    assert _unresolved(citations, _anchors(_rule_pages())) == citations


def test_a_fenced_heading_is_not_counted() -> None:
    assert _headings("## real\n```\n## fake\n```\n") == ["real"]
