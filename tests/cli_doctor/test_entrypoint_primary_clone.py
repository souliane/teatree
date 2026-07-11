"""``_check_entrypoint_is_primary_clone`` — the silent-isolation guard (#1507).

The installed ``t3`` must run from the primary clone. When a stale editable
``.pth`` anchors the entrypoint to a worktree, ``paths.DATA_DIR_AUTO_ISOLATED``
is ``True`` and the process reads a per-worktree isolated DB while the loop and
canonical state live elsewhere. This guard FAILs that case at session start.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import teatree
import teatree.paths as paths_mod
from teatree.cli.doctor.checks import _check_entrypoint_is_primary_clone


def _run() -> tuple[bool, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        ok = _check_entrypoint_is_primary_clone()
    return ok, out.getvalue()


class TestCheckEntrypointIsPrimaryClone:
    def test_passes_on_primary_clone(self) -> None:
        """A primary-clone entrypoint (not auto-isolated) passes."""
        with patch.object(paths_mod, "DATA_DIR_AUTO_ISOLATED", new=False):
            ok, message = _run()
        assert ok is True
        assert "FAIL" not in message

    def test_fails_when_entrypoint_auto_isolated(self, tmp_path: Path) -> None:
        """An auto-isolated worktree entrypoint FAILs, naming the DBs + remedy."""
        worktree_root = tmp_path / "wt"
        pkg_init = worktree_root / "src" / "teatree" / "__init__.py"
        pkg_init.parent.mkdir(parents=True)
        pkg_init.touch()
        isolated_dir = tmp_path / "teatree-worktrees" / "abc123"
        canonical = tmp_path / "share" / "teatree" / "db.sqlite3"
        with (
            patch.object(paths_mod, "DATA_DIR_AUTO_ISOLATED", new=True),
            patch.object(paths_mod, "DATA_DIR", new=isolated_dir),
            patch.object(paths_mod, "TRUE_CANONICAL_DB", new=canonical),
            patch.object(teatree, "__file__", new=str(pkg_init)),
        ):
            ok, message = _run()
        assert ok is False
        assert "FAIL" in message
        # Exact repo root, not the ``src`` sub-dir — guards the parents[] depth.
        assert f": {worktree_root}." in message
        assert str(isolated_dir / "db.sqlite3") in message
        assert str(canonical) in message
        assert "setup" in message
