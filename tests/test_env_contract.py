"""Stream C of robustness plan (#390).

Every ``${VAR}`` reference in a compose file must be either:

- defaulted (``${VAR:-fallback}``) so absence is self-handled, or
- produced by core or the overlay's ``declared_env_keys()``.

Keeps silent-failure-by-missing-env out of the repo: if an author references
``${FOO}`` with no producer, CI turns red here.
"""

from pathlib import Path

import pytest

from teatree.core.worktree_env import _declared_core_keys
from teatree.utils.compose_contract import (
    ComposeVarRef,
    check_contract,
    extract_refs,
)

REPO_ROOT = Path(__file__).parent.parent
COMPOSE_FILES = [
    REPO_ROOT / "dev" / "docker-compose.yml",
]

# Keys that come from the caller's environment, not the env cache.
# Keep this list small and explicit — every entry is a deliberate opt-out.
ALLOWED_SHELL_KEYS: set[str] = set()


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestExtractRefs:
    def test_finds_bare_var(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "a.yml", "services:\n  web:\n    image: ${IMAGE}\n")
        assert extract_refs(path) == [ComposeVarRef(var="IMAGE", path=path, line=3)]

    def test_skips_var_with_default(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "b.yml", "services:\n  web:\n    ports:\n      - ${PORT:-8000}:8000\n")
        assert extract_refs(path) == []

    def test_skips_var_with_required_marker(self, tmp_path: Path) -> None:
        """``${VAR:?err}`` is compose's 'fail fast' — not a silent-failure bug."""
        path = _write(tmp_path, "c.yml", "services:\n  db:\n    image: ${DB_IMG:?required}\n")
        assert extract_refs(path) == []

    def test_reports_line_numbers(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "d.yml",
            "services:\n  web:\n    image: ${IMAGE}\n    environment:\n      DB: ${DB_HOST}\n",
        )
        refs = extract_refs(path)
        assert {(r.var, r.line) for r in refs} == {("IMAGE", 3), ("DB_HOST", 5)}

    def test_multiple_refs_on_one_line(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "e.yml", '    command: "${CMD} --flag ${FLAG:-x} ${OTHER}"\n')
        refs = extract_refs(path)
        # FLAG has default, should be skipped.
        assert {r.var for r in refs} == {"CMD", "OTHER"}


class TestCheckContract:
    def test_passes_when_all_keys_produced(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "a.yml", "    image: ${IMAGE}\n    host: ${HOST}\n")
        assert check_contract([path], produced={"IMAGE", "HOST"}) == []

    def test_passes_when_key_allowed(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "b.yml", "    home: ${HOME}\n")
        assert check_contract([path], produced=set(), allowed={"HOME"}) == []

    def test_reports_missing_producer(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "c.yml", "    image: ${FOO_BAR}\n")
        violations = check_contract([path], produced=set())
        assert len(violations) == 1
        v = violations[0]
        assert v.var == "FOO_BAR"
        assert "no declared producer" in v.format()
        assert str(path) in v.format()

    def test_reports_all_references_for_same_var(self, tmp_path: Path) -> None:
        path1 = _write(tmp_path, "d1.yml", "    a: ${MISSING}\n")
        path2 = _write(tmp_path, "d2.yml", "    b: ${MISSING}\n")
        violations = check_contract([path1, path2], produced=set())
        assert len(violations) == 1
        assert len(violations[0].refs) == 2

    def test_skips_defaulted_references(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "e.yml", "    port: ${PORT:-8000}\n")
        assert check_contract([path], produced=set()) == []


class TestCoreComposeFiles:
    """Core's own dev compose files must satisfy their own contract."""

    def test_core_compose_files_exist(self) -> None:
        for path in COMPOSE_FILES:
            assert path.is_file(), f"expected compose file at {path}"

    def test_core_compose_files_have_no_undeclared_vars(self) -> None:
        produced = _declared_core_keys()
        violations = check_contract(
            COMPOSE_FILES,
            produced=produced,
            allowed=ALLOWED_SHELL_KEYS,
        )
        if violations:
            formatted = "\n  ".join(v.format() for v in violations)
            pytest.fail(f"Compose contract violations:\n  {formatted}")
