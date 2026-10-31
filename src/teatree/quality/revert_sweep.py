"""Revert each non-inert hunk of in-scope production code and record which tests notice, keyed by head."""

import json
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from teatree.utils import git
from teatree.utils.django_db.runner import runner_prefix
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

SCOPE_MARKERS = (
    "/core/merge/",
    "/core/gates/",
    "/core/cleanup/",
    "/core/retention/",
    "reap",
    "merge_clear",
    "review_verdict",
)
MAX_HUNKS = 40
TEST_RUN_TIMEOUT_SECONDS = 900
_OUTPUT_TAIL_LINES = 15
_OUTER_SESSION_ENV_PREFIXES = ("COV_CORE_", "COVERAGE_", "PYTEST_XDIST_")
_OUTER_SESSION_ENV = frozenset({"PYTEST_ADDOPTS", "PYTHONPATH"})
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.MULTILINE)
_KILLED_EXIT_CODES = frozenset({1, 2})


class RevertSweepError(RuntimeError):
    pass


class InnerRunOutcome(NamedTuple):
    code: int
    output_tail: str


class HunkStatus(StrEnum):
    KILLED = "KILLED"
    SURVIVOR = "SURVIVOR"
    UNMEASURED = "UNMEASURED"


@dataclass(frozen=True, slots=True)
class HunkVerdict:
    path: str
    line: int
    status: HunkStatus

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True, slots=True)
class RevertSweep:
    head_sha: str
    verdicts: tuple[HunkVerdict, ...]

    @property
    def survivors(self) -> tuple[HunkVerdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.status is HunkStatus.SURVIVOR)

    @property
    def unmeasured(self) -> tuple[HunkVerdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.status is HunkStatus.UNMEASURED)

    @property
    def clean(self) -> bool:
        return not self.survivors and not self.unmeasured

    def render(self) -> str:
        killed = len(self.verdicts) - len(self.survivors) - len(self.unmeasured)
        summary = (
            f"revert sweep {self.head_sha}: {len(self.verdicts)} hunk(s), {killed} killed, "
            f"{len(self.survivors)} survivors, {len(self.unmeasured)} unmeasured"
        )
        return "\n".join([summary, *(f"{verdict.status} {verdict.location}" for verdict in self.verdicts)])


@dataclass(frozen=True, slots=True)
class Hunk:
    path: str
    line: int
    patch: str

    @property
    def inert(self) -> bool:
        changed = [
            row[1:].strip() for row in self.patch.splitlines() if row[:1] in "+-" and row[:3] not in {"+++", "---"}
        ]
        return all(not row or row.startswith("#") for row in changed)


def in_scope(path: str) -> bool:
    return (
        path.startswith("src/teatree/") and path.endswith(".py") and any(marker in path[3:] for marker in SCOPE_MARKERS)
    )


def receipt_path(repo: Path) -> Path:
    raw = git.run_strict(repo=str(repo), args=["rev-parse", "--git-path", "t3/revert-sweep.json"])
    if not raw:
        message = "revert sweep git path is empty"
        raise RevertSweepError(message)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def write_revert_sweep(repo: Path, sweep: RevertSweep) -> None:
    path = receipt_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "head_sha": sweep.head_sha,
        "verdicts": [{"path": item.path, "line": item.line, "status": str(item.status)} for item in sweep.verdicts],
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    if read_revert_sweep(repo) != sweep:
        message = "revert sweep receipt did not read back"
        raise RevertSweepError(message)


def read_revert_sweep(repo: Path) -> RevertSweep | None:
    try:
        payload = json.loads(receipt_path(repo).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        message = "revert sweep receipt could not be read"
        raise RevertSweepError(message) from exc
    try:
        verdicts = tuple(
            HunkVerdict(str(raw["path"]), int(raw["line"]), HunkStatus(raw["status"])) for raw in payload["verdicts"]
        )
        head_sha = str(payload["head_sha"])
    except (KeyError, TypeError, ValueError) as exc:
        message = "revert sweep receipt is malformed"
        raise RevertSweepError(message) from exc
    return RevertSweep(head_sha, verdicts)


def in_scope_diff_paths(repo: Path, *, base: str, head: str) -> tuple[str, ...]:
    changed = git.run_strict(repo=str(repo), args=["diff", "--name-only", "--no-renames", base, head, "--"])
    return tuple(sorted(path for path in set(changed.splitlines()) if in_scope(path)))


def mirrored_test_path(source: str) -> str:
    parts = Path(source).parts[2:]
    package, directories, stem = parts[0], parts[1:-1], Path(parts[-1]).stem
    return Path("tests", f"teatree_{package}", *directories, f"test_{stem}.py").as_posix()


def default_test_command(repo: Path, scratch: Path) -> tuple[str, ...]:
    return (
        *runner_prefix(repo),
        "-m",
        "pytest",
        "-c",
        str(scratch / "pyproject.toml"),
        "--rootdir",
        str(scratch),
        "-n",
        "0",
        "-q",
        "-x",
        "--no-migrations",
        "--create-db",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:tach",
    )


class HunkSweeper:
    def __init__(
        self, repo: Path, scratch: Path, *, test_command: Sequence[str], changed_tests: tuple[str, ...]
    ) -> None:
        self.repo = repo
        self.scratch = scratch
        self.test_command = tuple(test_command)
        self.changed_tests = changed_tests
        self._baseline: dict[tuple[str, ...], InnerRunOutcome] = {}

    def verdict(self, hunk: Hunk) -> HunkVerdict:
        tests = self._tests_for(hunk.path)
        status = self._status(hunk, tests) if tests else HunkStatus.UNMEASURED
        return HunkVerdict(hunk.path, hunk.line, status)

    def _tests_for(self, source: str) -> tuple[str, ...]:
        candidates = {mirrored_test_path(source), *self.changed_tests}
        return tuple(sorted(str(self.scratch / path) for path in candidates if (self.scratch / path).is_file()))

    def _status(self, hunk: Hunk, tests: tuple[str, ...]) -> HunkStatus:
        if (baseline := self._unreverted_run(tests)).code != 0:
            message = (
                f"the tests for {hunk.path} do not pass at the measured head; the sweep cannot attribute kills\n"
                f"{baseline.output_tail}"
            )
            raise RevertSweepError(message)
        applied = run_allowed_to_fail(
            ["git", "apply", "-R", "--unidiff-zero", "--whitespace=nowarn", "-"],
            cwd=self.scratch,
            expected_codes=None,
            stdin_text=hunk.patch,
        )
        if applied.returncode != 0:
            return HunkStatus.UNMEASURED
        try:
            code = self._run(tests).code
        finally:
            self._restore()
        if code == 0:
            return HunkStatus.SURVIVOR
        return HunkStatus.KILLED if code in _KILLED_EXIT_CODES else HunkStatus.UNMEASURED

    def _unreverted_run(self, tests: tuple[str, ...]) -> InnerRunOutcome:
        if tests not in self._baseline:
            self._baseline[tests] = self._run(tests)
        return self._baseline[tests]

    def _environment(self) -> dict[str, str]:
        inherited = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_OUTER_SESSION_ENV_PREFIXES) and key not in _OUTER_SESSION_ENV
        }
        return {**inherited, "PYTHONPATH": str(self.scratch / "src"), "PYTHONDONTWRITEBYTECODE": "1"}

    def _run(self, tests: tuple[str, ...]) -> InnerRunOutcome:
        try:
            result = run_allowed_to_fail(
                [*self.test_command, *tests],
                cwd=self.scratch,
                env=self._environment(),
                expected_codes=None,
                timeout=TEST_RUN_TIMEOUT_SECONDS,
            )
        except TimeoutExpired:
            return InnerRunOutcome(-1, f"timed out after {TEST_RUN_TIMEOUT_SECONDS}s")
        lines = f"{result.stdout}\n{result.stderr}".strip().splitlines()
        return InnerRunOutcome(result.returncode, "\n".join(lines[-_OUTPUT_TAIL_LINES:]))

    def _restore(self) -> None:
        git.run_strict(repo=str(self.scratch), args=["reset", "--hard", "-q", "HEAD"])
        git.run_strict(repo=str(self.scratch), args=["clean", "-fdq"])


def diff_hunks(repo: Path, *, base: str, head: str, paths: Sequence[str]) -> list[Hunk]:
    hunks: list[Hunk] = []
    for path in paths:
        diff = git.run_strict_verbatim(
            repo=str(repo), args=["diff", "-U0", "--no-color", "--no-renames", base, head, "--", path]
        )
        matches = list(_HUNK_HEADER.finditer(diff))
        if not matches:
            continue
        header = diff[: matches[0].start()]
        ends = [match.start() for match in matches[1:]] + [len(diff)]
        hunks.extend(
            Hunk(path, int(match.group(1)), header + diff[match.start() : end])
            for match, end in zip(matches, ends, strict=True)
        )
    return hunks


def measure_revert_sweep(
    repo: Path,
    *,
    base_ref: str,
    expect_sha: str,
    test_command: Sequence[str] | None = None,
) -> RevertSweep:
    head = git.head_sha(str(repo))
    if head != expect_sha:
        message = f"revert sweep expected {expect_sha}, worktree is {head}"
        raise RevertSweepError(message)
    if git.run_strict(repo=str(repo), args=["status", "--porcelain", "--untracked-files=no"]):
        message = "revert sweep measures only committed tracked files"
        raise RevertSweepError(message)
    base = git.merge_base(str(repo), base_ref)
    hunks = [
        hunk
        for hunk in diff_hunks(repo, base=base, head=head, paths=in_scope_diff_paths(repo, base=base, head=head))
        if not hunk.inert
    ]
    if len(hunks) > MAX_HUNKS:
        message = f"{len(hunks)} non-inert hunks exceed the revert sweep limit of {MAX_HUNKS}; split the change"
        raise RevertSweepError(message)
    if not hunks:
        return RevertSweep(head, ())
    changed = git.run_strict(
        repo=str(repo), args=["diff", "--name-only", "--no-renames", "--diff-filter=ACMT", base, head, "--"]
    )
    changed_tests = tuple(
        sorted(path for path in changed.splitlines() if path.startswith("tests/") and path.endswith(".py"))
    )
    return RevertSweep(head, _sweep_in_scratch(repo, head, hunks, test_command, changed_tests))


def _sweep_in_scratch(
    repo: Path,
    head: str,
    hunks: list[Hunk],
    test_command: Sequence[str] | None,
    changed_tests: tuple[str, ...],
) -> tuple[HunkVerdict, ...]:
    workspace = Path(tempfile.mkdtemp(prefix="revert-sweep-"))
    scratch = workspace / "tree"
    git.run_strict(repo=str(repo), args=["worktree", "add", "--detach", "-q", str(scratch), head])
    try:
        command = test_command or default_test_command(repo, scratch)
        sweeper = HunkSweeper(repo, scratch, test_command=command, changed_tests=changed_tests)
        return tuple(sweeper.verdict(hunk) for hunk in hunks)
    finally:
        git.run_strict(repo=str(repo), args=["worktree", "remove", "--force", str(scratch)])
        shutil.rmtree(workspace, ignore_errors=True)
