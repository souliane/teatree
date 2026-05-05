"""SKILL.md frontmatter schema validation.

Validates required fields, regex patterns, and cross-references.
Can be used as a CLI tool: ``uv run python -m teatree.skill_schema <paths>``.

Teatree frontmatter is a superset of APM's SKILL.md format:
- APM requires: ``name``, ``description``
- Teatree adds: ``triggers``, ``search_hints``, ``requires``, ``metadata``, ``compatibility``

Unknown fields produce warnings (not errors) to preserve APM compatibility —
APM or other tools may add fields teatree doesn't know about.
"""

import re
import sys
from pathlib import Path

_KNOWN_TOP_LEVEL = frozenset(
    {
        "name",
        "description",
        "version",
        "triggers",
        "search_hints",
        "requires",
        "metadata",
        "compatibility",
    }
)

_KNOWN_TRIGGER_KEYS = frozenset(
    {
        "priority",
        "keywords",
        "urls",
        "exclude",
        "end_of_session",
    }
)


def validate_skill_md(path: Path, *, known_skills: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Validate a SKILL.md file's frontmatter.

    Returns (errors, warnings). Errors are blocking (pre-commit fails),
    warnings are informational.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not path.is_file():
        return [f"{path}: file not found"], []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: {exc}"], []

    if not text.startswith("---"):
        errors.append(f"{path}: missing YAML frontmatter (must start with ---)")
        return errors, warnings

    try:
        end = text.index("---", 3)
    except ValueError:
        errors.append(f"{path}: unclosed frontmatter (missing closing ---)")
        return errors, warnings

    frontmatter = text[3:end]
    fields = _extract_top_level_fields(frontmatter)

    # Required fields.
    if "name" not in fields:
        errors.append(f"{path}: missing required field 'name'")
    if "description" not in fields:
        errors.append(f"{path}: missing required field 'description'")

    # Unknown fields.
    warnings.extend(f"{path}: unknown field '{key}' (APM extension?)" for key in fields if key not in _KNOWN_TOP_LEVEL)

    # Validate trigger keyword regexes.
    _validate_trigger_keywords(path, frontmatter, errors)

    # Validate requires references.
    if known_skills is not None:
        _validate_requires_refs(path, frontmatter, known_skills, errors)

    return errors, warnings


def _extract_top_level_fields(frontmatter: str) -> set[str]:
    fields: set[str] = set()
    for line in frontmatter.splitlines():
        if not line.startswith((" ", "\t")) and ":" in line:
            key = line.split(":")[0].strip()
            if key:
                fields.add(key)
    return fields


def _validate_trigger_keywords(path: Path, frontmatter: str, errors: list[str]) -> None:
    in_keywords = False
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")) and ":" in stripped:
            in_keywords = False
            continue
        if stripped == "keywords:":
            in_keywords = True
            continue
        if in_keywords and stripped.startswith("- "):
            pattern = stripped.removeprefix("- ").strip().strip("'\"")
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{path}: invalid regex in triggers.keywords: '{pattern}' ({exc})")


def _validate_requires_refs(
    path: Path,
    frontmatter: str,
    known_skills: set[str],
    errors: list[str],
) -> None:
    in_requires = False
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")) and ":" in stripped:
            in_requires = stripped.split(":")[0].strip() == "requires"
            continue
        if in_requires and stripped.startswith("- "):
            ref = stripped.removeprefix("- ").strip().strip("'\"")
            if ref and ref not in known_skills:
                errors.append(f"{path}: requires unknown skill '{ref}'")


def validate_directory(root: Path) -> tuple[list[str], list[str]]:
    """Validate all SKILL.md files under *root*."""
    skill_dirs = sorted(d for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
    known_skills = {d.name for d in skill_dirs}

    all_errors: list[str] = []
    all_warnings: list[str] = []

    for skill_dir in skill_dirs:
        errs, warns = validate_skill_md(skill_dir / "SKILL.md", known_skills=known_skills)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    return all_errors, all_warnings


def main() -> None:
    """CLI entry point for pre-commit and manual validation."""
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        sys.stdout.write("Usage: python -m teatree.skill_schema <SKILL.md ...>\n")
        sys.exit(1)

    all_errors: list[str] = []
    all_warnings: list[str] = []

    for path in paths:
        if path.is_dir():
            errs, warns = validate_directory(path)
        else:
            errs, warns = validate_skill_md(path)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    for warning in all_warnings:
        sys.stdout.write(f"WARN: {warning}\n")
    for error in all_errors:
        sys.stdout.write(f"ERROR: {error}\n")

    if all_errors:
        sys.stdout.write(f"\nFAIL — {len(all_errors)} error(s)\n")
        sys.exit(1)
    else:
        sys.stdout.write("PASS\n")


if __name__ == "__main__":
    main()
