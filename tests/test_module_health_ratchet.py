"""Shrink-ratchet for the module-health hook (#1983).

Over-cap files were permanently grandfathered: a violation was appended only
when ``prev_loc <= MAX_LOC``, so a 1543-LOC file could grow forever. The
ratchet flips that: an over-cap file may only SHRINK. Growing an already-over-cap
file (LOC or public-function count) blocks the commit; shrinking it (or holding
steady) passes. By construction the current tree passes — every over-cap file's
HEAD LOC is its ceiling — so the ratchet may hard-fail (it never fires on the
existing state, only on a regression).
"""

from pathlib import Path

import pytest

import scripts.hooks.check_module_health as mod

_OVER_CAP = mod.MAX_LOC + 200
_OVER_CAP_GREW = _OVER_CAP + 50
_OVER_CAP_SHRANK = _OVER_CAP - 50


def _src(loc: int) -> str:
    return "\n".join(f"a_{i} = {i}" for i in range(loc)) + "\n"


def _fn_src(n_funcs: int) -> str:
    return "\n".join(f"def fn_{i}():\n    return {i}" for i in range(n_funcs)) + "\n"


class TestLocShrinkRatchet:
    def test_over_cap_file_that_grows_is_blocked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "src" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP_GREW), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/big.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: _OVER_CAP)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 1

    def test_over_cap_file_that_shrinks_is_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "src" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP_SHRANK), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/big.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: _OVER_CAP)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 0

    def test_over_cap_file_held_steady_is_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "src" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/big.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: _OVER_CAP)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 0


class TestMergeCommitSkipsRatchet:
    def test_grown_over_cap_file_in_merge_commit_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP_GREW), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_is_merge_commit", lambda: True)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/big.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: _OVER_CAP)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 0

    def test_grown_over_cap_file_outside_merge_still_blocks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP_GREW), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_is_merge_commit", lambda: False)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/big.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: _OVER_CAP)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 1


class TestRenameFollowsGrandfather:
    """A renamed over-cap file is grandfathered via its pre-rename path.

    The file-hierarchy campaign git-mv's over-cap modules into subpackages
    (e.g. ``backends/slack_bot.py`` → ``backends/slack/bot.py``). The new
    path does not exist at HEAD, so a path-keyed grandfather lookup would
    read 0 LOC and block the move as a fresh over-cap file. The hook must
    follow the rename and compare against the source path's HEAD ceiling.
    """

    def test_renamed_over_cap_file_is_not_blocked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "src" / "pkg" / "moved.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/pkg/moved.py"])
        monkeypatch.setattr(mod, "_head_paths", lambda: {"src/pkg/moved.py": "src/old.py"})
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda f: _OVER_CAP if f == "src/old.py" else 0)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 0

    def test_renamed_over_cap_file_that_grows_is_blocked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "src" / "pkg" / "moved.py"
        target.parent.mkdir(parents=True)
        target.write_text(_src(_OVER_CAP_GREW), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/pkg/moved.py"])
        monkeypatch.setattr(mod, "_head_paths", lambda: {"src/pkg/moved.py": "src/old.py"})
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda f: _OVER_CAP if f == "src/old.py" else 0)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: [])
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 1


class TestFunctionCountShrinkRatchet:
    def test_over_cap_function_count_that_grows_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        head_funcs = [f"fn_{i}" for i in range(mod.MAX_MODULE_FUNCTIONS + 5)]
        target = tmp_path / "src" / "many_fns.py"
        target.parent.mkdir(parents=True)
        target.write_text(_fn_src(mod.MAX_MODULE_FUNCTIONS + 8), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/many_fns.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: 0)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: head_funcs)
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 1

    def test_over_cap_function_count_that_shrinks_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        head_funcs = [f"fn_{i}" for i in range(mod.MAX_MODULE_FUNCTIONS + 8)]
        target = tmp_path / "src" / "many_fns.py"
        target.parent.mkdir(parents=True)
        target.write_text(_fn_src(mod.MAX_MODULE_FUNCTIONS + 5), encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_staged_python_files", lambda: ["src/many_fns.py"])
        monkeypatch.setattr(mod, "_count_loc_at_head", lambda _f: 0)
        monkeypatch.setattr(mod, "_count_module_level_functions_at_head", lambda _f: head_funcs)
        monkeypatch.setattr(mod, "_added_line_numbers", lambda _f, _h: set())
        monkeypatch.setattr("sys.argv", ["check_module_health.py"])

        assert mod.main() == 0
