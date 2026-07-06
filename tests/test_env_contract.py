"""Stream C of robustness plan (#390).

Every ``${VAR}`` reference in a compose file must be either:

- defaulted (``${VAR:-fallback}``) so absence is self-handled, or
- produced by core or the overlay's ``declared_env_keys()``.

Keeps silent-failure-by-missing-env out of the repo: if an author references
``${FOO}`` with no producer, CI turns red here.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep
from teatree.core.worktree.worktree_env import _declared_core_keys, render_env_cache
from teatree.types import DbImportStrategy
from teatree.utils.compose_contract import ComposeVarRef, check_contract, extract_refs, unproduced_declared_keys

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


class TestUnproducedDeclaredKeys:
    """Unit-level guard for the declared → produced direction."""

    def test_returns_empty_when_every_declared_key_is_produced(self) -> None:
        assert unproduced_declared_keys({"A", "B"}, [{"A", "B"}]) == set()

    def test_satisfied_when_any_render_produces_the_key(self) -> None:
        # B only appears in the second render (e.g. the shared-postgres branch).
        assert unproduced_declared_keys({"A", "B"}, [{"A"}, {"A", "B"}]) == set()

    def test_reports_declared_key_no_render_produces(self) -> None:
        assert unproduced_declared_keys({"A", "B", "C"}, [{"A"}, {"A", "B"}]) == {"C"}

    def test_empty_renders_means_everything_unproduced(self) -> None:
        assert unproduced_declared_keys({"A"}, []) == {"A"}


class _ProducerOverlay(OverlayBase):
    """Minimal overlay: no extra env keys, dedicated postgres."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _SharedPostgresProducerOverlay(_ProducerOverlay):
    """Same, but requests the shared-postgres branch (produces POSTGRES_HOST)."""

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return DbImportStrategy(shared_postgres=True)


_DEDICATED_PG = {"test": _ProducerOverlay()}
_SHARED_PG = {"test": _SharedPostgresProducerOverlay()}


class TestProducerContract(TestCase):
    """Every key core *declares* must actually be *produced* by the generator.

    ``check_contract`` only guards compose → declared. This guards declared →
    produced: if a producer line is deleted from ``_core_env_pairs`` /
    ``render_env_cache`` while the declaration stays, this turns red instead
    of the failure going silent (the #390 ``POSTGRES_HOST`` / ``rd:`` class).
    """

    def _render_keys(self, overlays: dict[str, OverlayBase], *, slug: str) -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()
            ticket = Ticket.objects.create(
                overlay="test",
                issue_url=f"https://example.com/issues/{slug}",
            )
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="wt_1",
                extra={"worktree_path": str(wt_path)},
            )
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=overlays):
                spec = render_env_cache(wt)
            assert spec is not None
            return set(spec.keys)

    def test_every_declared_core_key_is_produced_by_the_generator(self) -> None:
        # POSTGRES_HOST is only emitted on the shared-postgres branch; the
        # union across both branches must still cover every declared key.
        renders = [
            self._render_keys(_DEDICATED_PG, slug="dedicated"),
            self._render_keys(_SHARED_PG, slug="shared"),
        ]
        missing = unproduced_declared_keys(_declared_core_keys(), renders)
        if missing:
            pytest.fail(
                "Declared core keys with no generator producer "
                f"(declared in _declared_core_keys but never emitted by "
                f"render_env_cache): {sorted(missing)}"
            )
