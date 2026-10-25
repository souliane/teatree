"""Git-admin receipt for deterministic review probes at one committed head."""

import ast
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from teatree.quality.duplicate_definitions import DuplicateDefinition, duplicate_definitions
from teatree.quality.kill_matrix import KillMatrix, KillRow, build_kill_matrix
from teatree.utils import git
from teatree.utils.django_db.runner import runner_prefix
from teatree.utils.run import run_allowed_to_fail


class ReviewEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    head_sha: str
    duplicates: tuple[DuplicateDefinition, ...]
    matrix: KillMatrix

    def render(self) -> str:
        duplicates = (
            "\n".join(
                f"{item.path}:{item.second_line} duplicates {item.name} at line {item.first_line}"
                for item in self.duplicates
            )
            or "No duplicate definitions in the changed Python files."
        )
        return f"Duplicate-definition scan:\n{duplicates}\nMutation kill matrix:\n{self.matrix.render()}"


def _receipt_path(repo: Path) -> Path:
    raw = git.run_strict(repo=str(repo), args=["rev-parse", "--git-path", "t3/review-evidence.json"]).strip()
    if not raw:
        message = "review evidence git path is empty"
        raise ReviewEvidenceError(message)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _payload(evidence: ReviewEvidence) -> str:
    return json.dumps(
        {
            "head_sha": evidence.head_sha,
            "duplicates": [asdict(item) for item in evidence.duplicates],
            "matrix": {
                "head_sha": evidence.matrix.head_sha,
                "rows": [asdict(row) for row in evidence.matrix.rows],
            },
        },
        sort_keys=True,
    )


def write_review_evidence(repo: Path, evidence: ReviewEvidence) -> None:
    head = git.head_sha(str(repo))
    if head != evidence.head_sha or evidence.matrix.head_sha != head:
        message = "review evidence head differs from the committed worktree head"
        raise ReviewEvidenceError(message)
    if git.run_strict(repo=str(repo), args=["status", "--porcelain", "--untracked-files=no"]).strip():
        message = "review evidence requires no uncommitted tracked changes"
        raise ReviewEvidenceError(message)
    path = _receipt_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o600)
            stream.write(_payload(evidence))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if read_review_evidence(repo, head) != evidence:
        message = "review evidence receipt did not read back"
        raise ReviewEvidenceError(message)


def read_review_evidence(repo: Path, head_sha: str) -> ReviewEvidence | None:
    try:
        payload = json.loads(_receipt_path(repo).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        message = "review evidence receipt could not be read"
        raise ReviewEvidenceError(message) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("head_sha"), str):
        message = "review evidence receipt is malformed"
        raise ReviewEvidenceError(message)
    if payload["head_sha"] != head_sha:
        return None
    try:
        matrix = payload["matrix"]
        rows = tuple(
            KillRow(
                node=str(raw["node"]),
                killed=tuple(raw["killed"]),
                survived=tuple(raw["survived"]),
                inconclusive=tuple(raw["inconclusive"]),
                redundant_with=tuple(raw["redundant_with"]),
            )
            for raw in matrix["rows"]
        )
        duplicates = tuple(DuplicateDefinition(**raw) for raw in payload["duplicates"])
        evidence = ReviewEvidence(head_sha, duplicates, KillMatrix(matrix["head_sha"], rows))
    except (KeyError, TypeError, ValueError) as exc:
        message = "review evidence receipt is malformed"
        raise ReviewEvidenceError(message) from exc
    if evidence.matrix.head_sha != head_sha:
        message = "review evidence matrix names a different head"
        raise ReviewEvidenceError(message)
    return evidence


def collect_changed_test_nodes(repo: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    result = run_allowed_to_fail(
        [
            *runner_prefix(repo),
            "-m",
            "pytest",
            "-n",
            "0",
            "--collect-only",
            "-q",
            "--color=no",
            "-o",
            "addopts=",
            "--ds=tests.django_settings",
            *paths,
        ],
        cwd=repo,
        expected_codes=None,
        timeout=180,
    )
    if result.returncode != 0:
        message = f"changed-test collection failed (exit {result.returncode}): {result.stderr[-300:]}"
        raise ReviewEvidenceError(message)
    nodes = tuple(
        sorted({line.strip() for line in result.stdout.splitlines() if "::" in line and line.startswith(paths)})
    )
    if not nodes:
        message = "changed test files collected no exact test nodes"
        raise ReviewEvidenceError(message)
    return nodes


def imported_modules_for_tests(repo: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    modules: set[str] = set()
    root = repo.resolve()
    for path in paths:
        source = (repo / path).resolve()
        if not source.is_relative_to(root):
            message = f"changed test path leaves repository: {path}"
            raise ReviewEvidenceError(message)
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=path)
        names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        names.extend(
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        for name in names:
            if not name.startswith("teatree."):
                continue
            stem = Path("src", *name.split("."))
            for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                if (repo / candidate).is_file():
                    modules.add(candidate.as_posix())
    return tuple(sorted(modules))


def measure_review_evidence(repo: Path, *, base_ref: str, expect_sha: str) -> ReviewEvidence:
    head = git.head_sha(str(repo))
    if head != expect_sha:
        message = f"review evidence expected {expect_sha}, worktree is {head}"
        raise ReviewEvidenceError(message)
    if git.run_strict(repo=str(repo), args=["status", "--porcelain", "--untracked-files=no"]).strip():
        message = "review evidence measures only committed tracked files"
        raise ReviewEvidenceError(message)
    base = git.merge_base(str(repo), base_ref)
    changed = git.run_strict(repo=str(repo), args=["diff", "--name-only", "--diff-filter=ACMRT", base, head, "--"])
    paths = tuple(sorted(set(changed.splitlines())))
    source = tuple(path for path in paths if path.startswith("src/teatree/") and path.endswith(".py"))
    tests = tuple(path for path in paths if path.startswith("tests/") and path.endswith(".py"))
    duplicates = duplicate_definitions(repo, tuple(Path(path) for path in source))
    nodes = collect_changed_test_nodes(repo, tests)
    modules = source or imported_modules_for_tests(repo, tests)
    if nodes and not modules:
        message = "changed tests have no identifiable source module to mutate"
        raise ReviewEvidenceError(message)
    matrix = build_kill_matrix(nodes, modules, head_sha=head)
    return ReviewEvidence(head, duplicates, matrix)
