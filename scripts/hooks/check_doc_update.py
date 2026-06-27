"""Pre-push hook: block when an unambiguous trigger lacks a matching doc diff.

Background: souliane/teatree#1461 — README and BLUEPRINT drift silently
when source-code changes that introduce new user-visible surface (a top-level
``t3`` command, a new ``Ticket.State`` enum value, a new ``SKILL.md``, a new
``LoopLease`` row name) ship without the matching doc edit.

The hook catches only HIGH-CONFIDENCE triggers — no judgment, no false
positives. Soft cases (renamed flag defaults, user-observable error-message
shape, internal refactors) are governed by the skill prose in
``plugins/t3/skills/ship/SKILL.md`` § Documentation Discipline, where the
agent attests with ``docs: n/a — <reason>``.

On a no-trigger diff the hook is silent. On a trigger paired with the
required doc the hook is silent. Only the unpaired-trigger case prints
a finding and exits non-zero.
"""

import re
import subprocess
from dataclasses import dataclass

_CLI_INIT_PATH = "src/teatree/cli/__init__.py"
_TICKET_MODEL_PATH = "src/teatree/core/models/ticket.py"
_SKILL_MD_PATTERN = re.compile(r"^plugins/.+/skills/.+/SKILL\.md$|^skills/.+/SKILL\.md$")

_ADD_TYPER_RE = re.compile(r"""app\.add_typer\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*name\s*=\s*['"][^'"]+['"]""")
_APP_COMMAND_RE = re.compile(r"""app\.command\(\s*\)\s*\(""")
_TEXTCHOICE_ROW_RE = re.compile(r"""^\s*[A-Z][A-Z0-9_]*\s*=\s*['"][a-z0-9_-]+['"]\s*,""")
_LOOP_LEASE_RE = re.compile(r"""LoopLease\.objects\.(?:acquire|filter|get|get_or_create)\(\s*['"][a-z0-9_-]+['"]""")


@dataclass(frozen=True)
class Finding:
    """Single missing-doc finding produced by the gate."""

    trigger: str
    required_doc: str

    @property
    def message(self) -> str:
        return (
            f"{self.trigger} detected in this push — {self.required_doc} "
            f"must update in the same commit. Add the doc edit and retry, "
            f"or move the trigger to a separate PR."
        )


def _run_git(cmd: list[str]) -> str:
    """Run a git query, FAIL-LOUD on a non-zero exit.

    ``check=False`` would let a git failure (corrupt index, runner misconfig)
    return an empty string, which ``main()`` reads as "no staged changes" and
    silently exits 0 — every doc-update trigger skipped, the gate fake-green.
    A ``CalledProcessError`` instead crashes the hook with a visible diagnostic.
    """
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result.stdout


def _staged_diff() -> str:
    return _run_git(["git", "diff", "--cached", "--diff-filter=ACMR", "-U0"])


def _staged_files() -> list[str]:
    return [
        line for line in _run_git(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]).splitlines() if line
    ]


def _added_files() -> list[str]:
    return [
        line for line in _run_git(["git", "diff", "--cached", "--name-only", "--diff-filter=A"]).splitlines() if line
    ]


def _added_lines_for_path(diff: str, target_path: str) -> list[str]:
    added: list[str] = []
    current_file = ""
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            current_file = raw[4:].removeprefix("b/")
            continue
        if raw.startswith("+") and not raw.startswith("+++") and current_file == target_path:
            added.append(raw[1:])
    return added


def detect_top_level_command_added(diff: str) -> bool:
    """Detect a new top-level Typer command in ``cli/__init__.py``."""
    added = _added_lines_for_path(diff, _CLI_INIT_PATH)
    return any(_ADD_TYPER_RE.search(line) or _APP_COMMAND_RE.search(line) for line in added)


def detect_ticket_state_added(diff: str) -> bool:
    """Detect a new ``Ticket.State`` enum value in the ticket model."""
    added = _added_lines_for_path(diff, _TICKET_MODEL_PATH)
    return any(_TEXTCHOICE_ROW_RE.match(line) for line in added)


def detect_loop_lease_added(diff: str) -> bool:
    """Detect a new ``LoopLease`` row-name literal in any staged source file."""
    return _scan_added_lines_under(diff, _LOOP_LEASE_RE, ("src/teatree/",))


def detect_new_skill_md(staged_files: list[str], added_files: list[str]) -> bool:
    """Detect a brand-new ``SKILL.md`` file added to the tree."""
    added_set = set(added_files)
    return any(path in added_set and _SKILL_MD_PATTERN.match(path) for path in staged_files)


def _scan_added_lines_under(diff: str, pattern: re.Pattern[str], path_prefixes: tuple[str, ...]) -> bool:
    current_file = ""
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            current_file = raw[4:].removeprefix("b/")
            continue
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        if not any(current_file.startswith(prefix) for prefix in path_prefixes):
            continue
        if pattern.search(raw[1:]):
            return True
    return False


def find_missing_docs(
    diff: str,
    staged_files: list[str],
    added_files: list[str],
) -> list[Finding]:
    """Return one ``Finding`` per trigger without a matching doc diff."""
    files_set = set(staged_files)
    readme_changed = "README.md" in files_set
    blueprint_changed = "BLUEPRINT.md" in files_set

    findings: list[Finding] = []

    if detect_top_level_command_added(diff) and not readme_changed:
        findings.append(Finding(trigger="New top-level t3 command", required_doc="README.md"))

    if detect_new_skill_md(staged_files, added_files) and not readme_changed:
        findings.append(Finding(trigger="New SKILL.md file", required_doc="README.md"))

    if detect_ticket_state_added(diff) and not blueprint_changed:
        findings.append(Finding(trigger="New Ticket.State value", required_doc="BLUEPRINT.md"))

    if detect_loop_lease_added(diff) and not blueprint_changed:
        findings.append(Finding(trigger="New LoopLease row name", required_doc="BLUEPRINT.md"))

    return findings


def _format_failure(findings: list[Finding]) -> str:
    bullets = "\n".join(f"  - {f.message}" for f in findings)
    return (
        "Documentation drift gate (souliane/teatree#1461):\n\n"
        f"{bullets}\n\n"
        "Either: stage the doc edit in this commit, or move the trigger to\n"
        "a separate PR. Soft cases (renames, internal refactors) belong to\n"
        "the /t3:ship skill's `docs: n/a — <reason>` attestation, not this\n"
        "deterministic gate."
    )


def main() -> int:
    diff = _staged_diff()
    if not diff:
        return 0

    staged = _staged_files()
    added = _added_files()
    findings = find_missing_docs(diff, staged, added)
    if not findings:
        return 0

    print(_format_failure(findings))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
