"""Pre-commit hook: stop skill files from absorbing rules that should be code.

Background: souliane/teatree#140 — every recurring agent failure ends up as a
new ``Non-Negotiable`` bullet in a ``SKILL.md`` instead of a ``PreToolUse``
hook deny, an FSM transition condition, or a CLI argparse rejection. Prose
piles up, agents stop loading it, and the next session repeats the failure.
This hook stops the bleeding while staged transition work (#140) shrinks the
existing rule set.

The hook fails when ``skills/**/SKILL.md`` or ``skills/**/references/*.md``
adds new imperative bullets (``Non-Negotiable``, leading ``Always``/``Never``/
``Stop``/``Run``) without an accompanying change in ``src/``,
``hooks/scripts/``, or ``tests/``. A ``<!-- prose-allowed: <reason> -->``
marker on the line directly above grandfathers a section.
"""

import re
import subprocess
from dataclasses import dataclass

NEW_RULE_PATTERN = re.compile(
    r"^[\s]*[-*]\s+\*\*(?:[^*]*?\b(?:Non-Negotiable|Always|Never|Stop|Run)\b)",
)
PROSE_ALLOWED_PATTERN = re.compile(r"<!--\s*prose-allowed:")
SKILL_PATH_PATTERN = re.compile(r"^skills/.+/(SKILL\.md|references/.+\.md)$")
COMPANION_PREFIXES = ("src/", "hooks/scripts/", "tests/")


@dataclass(frozen=True)
class RuleAddition:
    path: str
    line_number: int
    line: str


def _staged_diff() -> str:
    cmd = ["git", "diff", "--cached", "--diff-filter=ACMR", "-U1", "--", "skills/"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout


def _staged_files() -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return [line for line in result.stdout.splitlines() if line]


def has_companion_code_change(files: list[str]) -> bool:
    return any(path.startswith(COMPANION_PREFIXES) for path in files)


def count_new_rule_lines(diff: str) -> list[RuleAddition]:
    findings: list[RuleAddition] = []
    current_file = ""
    line_num = 0
    in_allowed_section = False

    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ "):
            current_file = raw_line[4:].removeprefix("b/")
            in_allowed_section = False
            continue

        if raw_line.startswith("@@ "):
            for part in raw_line.split():
                if part.startswith("+") and "," in part:
                    line_num = int(part[1:].split(",")[0])
                    break
                if part.startswith("+") and part[1:].isdigit():
                    line_num = int(part[1:])
                    break
            in_allowed_section = False
            continue

        if raw_line.startswith(("---", "diff ")):
            in_allowed_section = False
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            content = raw_line[1:]
            if not content.strip():
                in_allowed_section = False
            elif PROSE_ALLOWED_PATTERN.search(content):
                in_allowed_section = True
            elif SKILL_PATH_PATTERN.match(current_file) and NEW_RULE_PATTERN.search(content) and not in_allowed_section:
                findings.append(RuleAddition(current_file, line_num, content))
            line_num += 1
            continue

        if raw_line.startswith(" "):
            content = raw_line[1:]
            if not content.strip():
                in_allowed_section = False
            elif PROSE_ALLOWED_PATTERN.search(content):
                in_allowed_section = True
            line_num += 1

    return findings


def _format_failure(findings: list[RuleAddition]) -> str:
    bullet_lines = "\n".join(f"  {item.path}:{item.line_number}: {item.line.strip()[:120]}" for item in findings)
    return (
        "Skill prose grew without a code change (souliane/teatree#140 Stage 0):\n\n"
        f"{bullet_lines}\n\n"
        "Each new imperative rule belongs in one of four homes — pick one:\n"
        "  1. PreToolUse hook deny     → hooks/scripts/hook_router.py\n"
        "  2. FSM transition condition → src/teatree/core/models/*.py\n"
        "  3. CLI argparse rejection   → src/teatree/core/management/commands/\n"
        "  4. Legitimately prose       → add `<!-- prose-allowed: <reason> -->`\n"
        "                                 directly above the bullet (methodology only)\n\n"
        "If you are deleting other prose alongside, stage a src/ or hooks/scripts/\n"
        "or tests/ file in the same commit so this hook can verify the migration."
    )


def main() -> int:
    diff = _staged_diff()
    if not diff:
        return 0

    findings = count_new_rule_lines(diff)
    if not findings:
        return 0

    files = _staged_files()
    if has_companion_code_change(files):
        return 0

    print(_format_failure(findings))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
