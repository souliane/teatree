"""The exact set of skill files a dispatched agent may ``Read`` outside its worktree.

A skill cites its references by repo-relative path (``skills/<skill>/references/<f>.md``),
and a dispatch whose worktree is not a teatree checkout — or that has no worktree —
cannot resolve that path under its own jail. This index registers every
``<root>/<skill>/SKILL.md`` and ``<root>/<skill>/references/*.md`` that is a regular,
non-symlinked file, and :meth:`SkillFileIndex.lookup` answers only an exact match
against those registered paths. There is no path arithmetic, so ``..``, a prefix
look-alike, a symlink out of a skill folder, and any other file under a skills root
resolve to nothing by construction. Django-free, like :mod:`skill_injection`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SkillFileIndex:
    by_key: Mapping[str, Path] = field(default_factory=dict)
    by_absolute: Mapping[Path, Path] = field(default_factory=dict)

    @classmethod
    def build(cls, roots: Sequence[Path]) -> "SkillFileIndex":
        by_key: dict[str, Path] = {}
        by_absolute: dict[Path, Path] = {}
        for root in roots:
            for path in _skill_files(root):
                by_key.setdefault(f"skills/{path.relative_to(root).as_posix()}", path)
                by_absolute.setdefault(path, path)
                by_absolute.setdefault(path.resolve(), path)
        return cls(by_key=by_key, by_absolute=by_absolute)

    def lookup(self, candidate: str) -> Path | None:
        if candidate in self.by_key:
            return self.by_key[candidate]
        as_path = Path(candidate)
        return self.by_absolute.get(as_path) if as_path.is_absolute() else None


NO_SKILL_FILES = SkillFileIndex()


def _skill_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for skill_dir in sorted(root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not _is_plain_file(skill_md):
            continue
        found.append(skill_md)
        references = skill_dir / "references"
        if references.is_dir() and not references.is_symlink():
            found.extend(path for path in sorted(references.glob("*.md")) if _is_plain_file(path))
    return found


def _is_plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def reach_line(skills_dir: Path) -> str:
    return f"Skill files cited as `skills/<skill>/…` are at `{skills_dir}/<skill>/…`; open one with the Read tool."
