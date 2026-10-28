"""Failure text for the size floors: what this branch added, so an unrelated change is not blamed."""

import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from teatree.utils.git import run_strict

_SKILL_SECTION = re.compile(r"^--- SKILL: (?P<name>.+?) ---$", re.MULTILINE)
_BASE_REFS = ("origin/main", "main")
_TOP_GROUPS = 8
_TOP_SECTIONS = 6


def _git(repo: Path, *args: str) -> str:
    try:
        return run_strict(repo=str(repo), args=list(args))
    except RuntimeError:
        return ""


def branch_base(repo: Path) -> str:
    return next((base for ref in _BASE_REFS if (base := _git(repo, "merge-base", "HEAD", ref))), "")


def _blob_bytes(repo: Path, rev: str, path: str) -> int:
    size = _git(repo, "cat-file", "-s", f"{rev}:{path}")
    return int(size) if size.isdigit() else 0


def skill_section_sizes(context: str) -> dict[str, int]:
    marks = list(_SKILL_SECTION.finditer(context))
    ends = [mark.start() for mark in marks[1:]] + [len(context)]
    return {mark["name"]: len(context[mark.end() : end].encode()) for mark, end in zip(marks, ends, strict=True)}


def branch_skill_bytes(repo: Path) -> dict[str, int] | None:
    if not (base := branch_base(repo)):
        return None
    names = _git(repo, "diff", "--name-only", base, "HEAD", "--", "skills").splitlines()
    return {name: _blob_bytes(repo, "HEAD", name) - _blob_bytes(repo, base, name) for name in names}


def skill_report(context: str, repo: Path) -> str:
    sizes = skill_section_sizes(context)
    largest = ", ".join(
        f"{name} {size} B" for name, size in sorted(sizes.items(), key=lambda item: -item[1])[:_TOP_SECTIONS]
    )
    changed = branch_skill_bytes(repo)
    if changed is None:
        mine = "unknown (no merge base with main in this checkout)"
    else:
        mine = ", ".join(f"{name} {delta:+d} B" for name, delta in sorted(changed.items())) or "no skill file"
    return f"largest skill sections: {largest}. Skill bytes changed on this branch: {mine}."


def branch_added_modules(repo: Path) -> set[str] | None:
    if not (base := branch_base(repo)):
        return None
    paths = _git(repo, "diff", "--name-only", "--diff-filter=A", base, "HEAD", "--", "src").splitlines()
    return {
        path.removeprefix("src/").removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        for path in paths
        if path.endswith(".py")
    }


def import_report(modules: Iterable[str], repo: Path, forbidden: Iterable[str]) -> str:
    loaded = sorted(set(modules))
    groups = Counter(
        ".".join(name.split(".")[:2]) if name.startswith("teatree.") else name.split(".")[0] for name in loaded
    )
    top = ", ".join(f"{group} {count}" for group, count in groups.most_common(_TOP_GROUPS))
    added = branch_added_modules(repo)
    mine = (
        "unknown (no merge base with main in this checkout)"
        if added is None
        else (", ".join(sorted(added & set(loaded))) or "none")
    )
    forbidden_text = ", ".join(forbidden) or "none"
    return (
        f"largest import groups: {top}. Forbidden packages loaded: {forbidden_text}. "
        f"Modules this branch added that load at startup: {mine}."
    )
