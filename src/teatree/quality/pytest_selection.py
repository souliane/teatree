"""Local pytest path selection and recorded-duration budget."""

import math
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.durations_file import DURATIONS_PATH, DurationsUnreadableError, read_durations

LOCAL_DIRECTORY_BUDGET_SECONDS = 1200


def ci_environment_enabled(value: str | None) -> bool:
    token = value.strip().lower() if value else ""
    return bool(token and token not in {"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class SelectionContext:
    root: Path
    sharded: bool
    tach_active: bool = False
    full_selected: bool = False
    invocation_dir: Path | None = None
    in_ci: bool = False


def oversized_selection_refusal(args: list[str], *, context: SelectionContext) -> str:
    """Refuse accidental large local selections before pytest collects them.

    ``tach_active`` must exempt both scoped shapes: flags only, and an explicit
    ``tests`` root alongside changed modules for doctest collection.
    """
    if context.sharded or context.tach_active or context.full_selected:
        return ""
    tests_root = (context.root / "tests").resolve()
    base = context.invocation_dir or context.root
    selected = [(base / arg.split("::", 1)[0]).resolve() for arg in args if not arg.startswith("-")]
    whole_roots = {context.root.resolve(), tests_root}
    if not selected or any(path in whole_roots for path in selected):
        return (
            "unsharded whole-tree pytest is refused locally; run dev/ci-parity-fast.sh for scoped feedback "
            "or use the sharded CI lane. For direct test paths, -n auto is bounded by "
            "PYTEST_XDIST_AUTO_NUM_WORKERS and the memory-aware local default."
        )
    if context.in_ci:
        return ""
    directories = {path for path in selected if path.is_dir() and path.is_relative_to(tests_root)}
    return _directory_budget_refusal(context.root, tests_root, directories)


def _recorded_seconds(root: Path, directories: set[Path], recorded: dict[str, float]) -> tuple[float, set[Path]]:
    seconds = 0.0
    covered: set[Path] = set()
    by_file: dict[str, float] = {}
    invalid_files: set[str] = set()
    for node_id, duration in recorded.items():
        file_id = node_id.split("::", 1)[0]
        by_file.setdefault(file_id, 0.0)
        if not math.isfinite(duration) or duration < 0:
            invalid_files.add(file_id)
        else:
            by_file[file_id] += duration
    for file_id, file_seconds in by_file.items():
        path = (root / file_id).resolve()
        matching = {directory for directory in directories if path.is_relative_to(directory)}
        if matching:
            if file_id in invalid_files:
                message = f"{root / DURATIONS_PATH} has invalid recorded duration"
                raise DurationsUnreadableError(message)
            seconds += file_seconds
            covered.update(matching)
    return seconds, covered


def _directory_budget_refusal(root: Path, tests_root: Path, directories: set[Path]) -> str:
    if not directories:
        return ""
    durations_path = root / DURATIONS_PATH
    recorded = read_durations(durations_path)
    if not recorded and not durations_path.is_file():
        return ""
    seconds, covered = _recorded_seconds(root, directories, recorded)
    if uncovered := directories - covered:
        paths = ", ".join(str(path.relative_to(tests_root)) for path in sorted(uncovered))
        return f"local pytest directory selection has no recorded durations for {paths}; select test files/node ids"
    if seconds > LOCAL_DIRECTORY_BUDGET_SECONDS:
        budget = LOCAL_DIRECTORY_BUDGET_SECONDS
        return (
            f"local pytest directory selection records {seconds:.0f}s, above the {budget}s budget; "
            "run bash dev/test-affected.sh or select test files/node ids. "
            "Use --teatree-selected-full for a deliberate large selection; CI owns whole-tree proof."
        )
    return ""
