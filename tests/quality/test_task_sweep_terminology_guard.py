"""Greppable guard against the retired ``todo_sweep`` / ``TodoSweep`` conflation (#129).

The loop unit that reconciles teatree ``Task`` rows was renamed ``todo_sweep`` →
``task_sweep`` (class ``TaskSweepScanner``, settings ``task_sweep_*``, signals
``task.completion_detected`` / ``task.orphaned``, handler ``task_completion``)
because it acts on teatree **tasks**, never the harness **TODO** list. This guard
turns that rename into a mechanical floor: any reappearance of the conflating
identifiers in ``src/`` / ``tests/`` / ``skills/`` / ``BLUEPRINT.md`` is RED.

The single sanctioned exception is the backward-compat alias surface: the retired-key
registry (``config/retired_settings.py``, #3527), its test, the settings docstring
that documents the rename, and the BLUEPRINT row that points at it all reference the
retired ``todo_sweep_*`` key on purpose — they pair the old name with the new
rather than conflating the two. That surface is allow-listed by exact relative
path so the guard cannot be defeated by planting a conflation elsewhere.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = ("src", "tests", "skills")
_EXTRA_FILES = ("BLUEPRINT.md",)

# The conflating identifiers the rename retired. ``TODO list`` is excluded from
# this symbol guard because the phrase legitimately names the harness TODO list
# in unrelated surfaces (``tasks.py``, the todos skill); a separate assertion
# below pins that the scanner's own former "The TODO list" docstring is gone.
_FORBIDDEN = re.compile(r"todo_sweep|TodoSweep|todo_completion|todo\.completion_detected|todo\.orphaned")

# Files that reference the retired ``todo_sweep_*`` config key on purpose, to
# keep a stored legacy row resolving (the backward-compat alias). Relative to
# the repo root; matched exactly, never by substring, so the carve-out cannot
# be widened by accident.
_LEGACY_ALIAS_ALLOWLIST = frozenset(
    {
        "src/teatree/config/retired_settings.py",
        "src/teatree/config/settings.py",
        "tests/teatree_loop/test_task_sweep_wiring.py",
        "tests/quality/test_task_sweep_terminology_guard.py",
        "BLUEPRINT.md",
    },
)


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        base = _REPO_ROOT / root
        files.extend(p for p in base.rglob("*") if p.is_file() and p.suffix in {".py", ".md"})
    files.extend(_REPO_ROOT / name for name in _EXTRA_FILES)
    return files


def _rel(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


class TestNoTodoSweepConflation:
    def test_no_forbidden_identifier_outside_alias_allowlist(self) -> None:
        offenders: list[str] = []
        for path in _candidate_files():
            rel = _rel(path)
            if rel in _LEGACY_ALIAS_ALLOWLIST:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except (FileNotFoundError, OSError):
                # A transient file (e.g. another process's probe) may vanish
                # between the rglob listing and the read — skip it, never crash
                # the guard on a TOCTOU race.
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _FORBIDDEN.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert offenders == [], (
            "Retired todo_sweep/TodoSweep conflation reappeared — rename to task_sweep/TaskSweepScanner "
            "(the unit reconciles teatree Task rows, not the harness TODO list):\n" + "\n".join(offenders)
        )

    def test_scanner_docstring_no_longer_calls_tasks_the_todo_list(self) -> None:
        # The scanner's former docstring literally called the open Task rows
        # "The TODO list" — the exact conflation the rename purged.
        scanner = _REPO_ROOT / "src" / "teatree" / "loop" / "scanners" / "task_sweep.py"
        assert scanner.exists(), "task_sweep.py must exist after the rename"
        assert "TODO list" not in scanner.read_text(encoding="utf-8")
        # The retired module name must be gone entirely.
        assert not (_REPO_ROOT / "src" / "teatree" / "loop" / "scanners" / "todo_sweep.py").exists()

    def test_allowlist_entries_exist_and_actually_reference_the_alias(self) -> None:
        # An allow-listed file that no longer references the retired key is dead
        # carve-out — drop it, so the guard cannot silently widen its exemption.
        alias_refs = {"src/teatree/config/retired_settings.py", "src/teatree/config/settings.py"}
        for rel in alias_refs:
            path = _REPO_ROOT / rel
            assert path.exists(), f"allow-listed alias file missing: {rel}"
            assert "todo_sweep" in path.read_text(encoding="utf-8"), (
                f"{rel} is allow-listed but no longer references the legacy todo_sweep alias — "
                "remove it from the allowlist"
            )


class TestGuardBites:
    """Anti-vacuity: the guard must turn RED on a planted conflation."""

    def test_planted_conflation_in_a_non_allowlisted_file_is_caught(self, tmp_path: Path) -> None:
        planted = tmp_path / "src" / "teatree" / "loop" / "scanners" / "planted.py"
        planted.parent.mkdir(parents=True)
        planted.write_text("class TodoSweepScanner: ...\n", encoding="utf-8")
        assert _FORBIDDEN.search(planted.read_text(encoding="utf-8")) is not None
        # And the same string in an allow-listed path is permitted (the alias surface).
        assert "src/teatree/config/retired_settings.py" in _LEGACY_ALIAS_ALLOWLIST
