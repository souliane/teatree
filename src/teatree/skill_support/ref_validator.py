"""Dangling skill-reference validator.

Enumerates every place a skill *name* is referenced and expected to resolve,
then asserts each resolves to a real skill in the **canonical skill set** —
the actual installed/remote skills, not a hardcoded allowlist that would
itself drift. Any reference that does not resolve is a :class:`DanglingReference`
naming the file:line, the bad name, and the nearest valid matches.

The canonical set is enumerated exactly as the skill-loading hook does
(:func:`teatree.skill_support.loading.SkillLoadingPolicy` reads the same search dirs):
every ``<search-dir>/<name>/SKILL.md`` is a skill. The default search dirs
resolve the ``~/.claude/skills/*`` symlinks (which point into the user's remote
skill repos and any external skill packages) plus this plugin's own ``skills/``
tree. A reference name is canonical when its bare ``:``-stripped segment is one
of those directory names.

Reference sites enumerated:

* the ``.teatree-skills.yml`` keyword->skill routing config in the home dir (and
    any ``T3_SUPPLEMENTARY_SKILLS`` override location) — the file that carried the
    real ``ac-reviewing-skills`` dangling name the owner caught;
* ``agents/*.md`` frontmatter ``skills:`` and ``companion_skills`` lists.

Runnable via ``t3 tool validate-skill-refs``.
"""

import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SUGGESTION_CUTOFF = 0.6
_MAX_SUGGESTIONS = 3
_CONFIG_LINE_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]+):\s+(.*)")
_FRONTMATTER_LIST_KEYS = ("skills", "companion_skills", "requires")


@dataclass(frozen=True, slots=True)
class DanglingReference:
    """A skill reference that does not resolve to a canonical skill."""

    path: Path
    line: int
    name: str
    site: str
    suggestions: list[str] = field(default_factory=list)

    def render(self) -> str:
        hint = f" (did you mean: {', '.join(self.suggestions)}?)" if self.suggestions else ""
        return f"{self.path}:{self.line}: [{self.site}] dangling skill reference '{self.name}'{hint}"


def default_search_dirs() -> list[Path]:
    """Return the canonical skill search dirs — same source the hook uses.

    ``T3_SKILL_SEARCH_DIRS`` (os.pathsep-separated) overrides the defaults
    (the test seam). Otherwise: this plugin's own ``skills/`` directory plus
    the agent skill install locations (``~/.agents/skills``, ``~/.claude/skills``),
    whose symlinks resolve into the user's remote skill repos.
    """
    override = os.environ.get("T3_SKILL_SEARCH_DIRS", "")
    if override:
        return [Path(d) for d in override.split(os.pathsep) if d]

    home = os.environ.get("HOME", str(Path.home()))
    plugin_skills = Path(__file__).resolve().parents[3] / "skills"
    candidates = [
        plugin_skills,
        Path(home) / ".agents" / "skills",
        Path(home) / ".claude" / "skills",
    ]
    return [d for d in candidates if d.is_dir()]


def canonical_skill_names(search_dirs: list[Path]) -> set[str]:
    """Enumerate the canonical skill names from *search_dirs*.

    A directory is a skill iff it contains a ``SKILL.md`` (symlinked skill
    dirs resolve through ``is_file``). This is the authoritative enumeration —
    no hardcoded list to drift.
    """
    names: set[str] = set()
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        try:
            entries = sorted(search_dir.iterdir())
        except OSError:
            continue
        for skill_dir in entries:
            if (skill_dir / "SKILL.md").is_file():
                names.add(skill_dir.name)
    return names


def _bare_segment(name: str) -> str:
    """Return the bare skill name — last ``/`` segment, last ``:`` segment.

    A namespaced ``t3:rules`` resolves on its bare ``rules``; an overlay
    ``skills/<skill>/SKILL.md`` path resolves on ``<skill>``.
    """
    segment = name.rstrip("/").removesuffix("/SKILL.md").rsplit("/", 1)[-1]
    if ":" in segment:
        segment = segment.rsplit(":", 1)[-1]
    return segment


def _resolves(name: str, canonical: set[str]) -> bool:
    return _bare_segment(name) in canonical


def _suggest(name: str, canonical: set[str]) -> list[str]:
    return difflib.get_close_matches(
        _bare_segment(name),
        sorted(canonical),
        n=_MAX_SUGGESTIONS,
        cutoff=_SUGGESTION_CUTOFF,
    )


def validate_supplementary_config(config_path: Path, canonical: set[str]) -> list[DanglingReference]:
    """Flag dangling skill names in the home-dir ``.teatree-skills.yml`` routing config.

    A missing config file is *not* a failure (fail-open) — the file is
    optional. Comments and blank lines are skipped, matching the hook's own
    parser (:func:`scripts.lib.skill_loader.read_supplementary_skills`).
    """
    if not config_path.is_file():
        return []
    findings: list[DanglingReference] = []
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _CONFIG_LINE_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if not _resolves(name, canonical):
            findings.append(
                DanglingReference(
                    path=config_path,
                    line=number,
                    name=name,
                    site="supplementary-config",
                    suggestions=_suggest(name, canonical),
                )
            )
    return findings


def validate_agent_frontmatter(agent_path: Path, canonical: set[str]) -> list[DanglingReference]:
    """Flag dangling skill names in an ``agents/*.md`` frontmatter list field.

    Scans the ``skills:`` / ``companion_skills:`` / ``requires:`` list items
    inside the leading ``---`` frontmatter block.
    """
    if not agent_path.is_file():
        return []
    try:
        text = agent_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not text.startswith("---"):
        return []
    findings: list[DanglingReference] = []
    in_list = False
    for number, raw_line in enumerate(text.splitlines(), start=1):
        if number > 1 and raw_line.strip() == "---":
            break
        stripped = raw_line.strip()
        if not raw_line.startswith((" ", "\t")) and ":" in stripped:
            key = stripped.split(":", 1)[0].strip()
            in_list = key in _FRONTMATTER_LIST_KEYS
            continue
        if in_list and stripped.startswith("- "):
            name = stripped.removeprefix("- ").strip().strip("'\"")
            if name and not _resolves(name, canonical):
                findings.append(
                    DanglingReference(
                        path=agent_path,
                        line=number,
                        name=name,
                        site="agent-frontmatter",
                        suggestions=_suggest(name, canonical),
                    )
                )
    return findings


def _agent_files(agents_dir: Path) -> list[Path]:
    if not agents_dir.is_dir():
        return []
    return sorted(agents_dir.glob("*.md"))


def validate_skill_refs(
    *,
    search_dirs: list[Path] | None = None,
    supplementary_config: Path | None = None,
    agents_dir: Path | None = None,
) -> list[DanglingReference]:
    """Validate every skill-reference site against the canonical skill set.

    Returns the aggregated list of dangling references (empty == clean). The
    caller decides the exit code. ``search_dirs`` / ``supplementary_config`` /
    ``agents_dir`` default to the real install locations when omitted.
    """
    dirs = search_dirs if search_dirs is not None else default_search_dirs()
    canonical = canonical_skill_names(dirs)

    config = (
        supplementary_config
        if supplementary_config is not None
        else Path(os.environ.get("T3_SUPPLEMENTARY_SKILLS", str(Path.home() / ".teatree-skills.yml")))
    )
    agents = agents_dir if agents_dir is not None else Path(__file__).resolve().parents[3] / "agents"

    findings = validate_supplementary_config(config, canonical)
    for agent in _agent_files(agents):
        findings.extend(validate_agent_frontmatter(agent, canonical))
    return findings


def validate_repo_refs(repo_root: Path) -> list[DanglingReference]:
    """Validate the repo's OWN reference sites against its plugin skill set.

    Scoped to the repo: the canonical set is the plugin's ``skills/`` tree
    (CI-portable — no dependence on a developer's ``~/.claude/skills``), and
    the only reference site is ``agents/*.md`` frontmatter. The personal
    home-dir ``.teatree-skills.yml`` lives outside the repo and is validated by the
    runnable ``t3 tool validate-skill-refs`` command, not this repo gate.
    """
    canonical = canonical_skill_names([repo_root / "skills"])
    findings: list[DanglingReference] = []
    for agent in _agent_files(repo_root / "agents"):
        findings.extend(validate_agent_frontmatter(agent, canonical))
    return findings


def main() -> None:  # pragma: no cover — pre-commit entry point (orchestrates tested helpers)
    """Pre-commit entry point — validate the repo's own agent skill references."""
    repo_root = Path(__file__).resolve().parents[3]
    findings = validate_repo_refs(repo_root)
    for finding in findings:
        sys.stderr.write(finding.render() + "\n")
    if findings:
        sys.stderr.write(f"\nFAIL — {len(findings)} dangling skill reference(s)\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
