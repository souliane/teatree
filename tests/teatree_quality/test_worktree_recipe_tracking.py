"""Fitness function: no published recipe leaves a new branch tracking a remote ref (#4225).

:class:`TestLiveTree` is the gate — every ``git worktree add`` recipe under
``skills/`` and ``docs/`` that creates a branch from ``origin/<branch>`` carries
``--no-track``. It was observed RED against the three sites the #4225 fix commit
missed (``skills/ship/SKILL.md`` and both worked-dispatch examples).

:class:`TestDefectiveShape` is the anti-vacuity proof: the scanner names a
planted defective recipe in a fence and in an inline-backtick span.

:class:`TestCleanShapes` and :class:`TestFragmentBounding` are the symmetric
must-NOT-flag proofs — a recipe that creates no branch, one starting from a local
ref, one whose ``--no-track`` sits on a joined continuation line, and the prose
shape where a ``-b`` belongs to a DIFFERENT command quoted in the same sentence.
"""

from pathlib import Path

import pytest

from teatree.quality.worktree_recipe_tracking import (
    ALLOW_PRAGMA,
    NO_TRACK,
    Finding,
    _repo_root,
    collect_files,
    invocations_in,
    is_defective,
    logical_lines,
    run,
    scan_source,
    scan_tree,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFECTIVE = "git worktree add ../wt -b 42-feature ../wt origin/main"
_CLEAN = f"git worktree add -b 42-feature {NO_TRACK} ../wt origin/main"


def _fence(body: str) -> str:
    return f"Prose first.\n\n```bash\n{body}\n```\n"


def _plant(root: Path, relpath: str, body: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestLiveTree:
    def test_no_published_recipe_omits_no_track(self) -> None:
        findings = scan_tree(_REPO_ROOT)
        assert findings == [], "\n".join(f.message for f in findings)

    def test_the_scanned_roots_are_actually_present(self) -> None:
        # A gate whose corpus is empty is green for the wrong reason.
        assert len(collect_files(_REPO_ROOT)) > 1

    def test_the_module_entry_point_resolves_the_repo_root(self) -> None:
        # `python -m …` reaches the tree only through this path arithmetic.
        assert _repo_root() == _REPO_ROOT


class TestDefectiveShape:
    def test_fenced_recipe_is_named(self) -> None:
        findings = scan_source(_fence(_DEFECTIVE), "skills/x/SKILL.md")
        assert [(f.path, f.line_no) for f in findings] == [("skills/x/SKILL.md", 4)]
        assert NO_TRACK in findings[0].message

    def test_inline_backticked_recipe_is_named(self) -> None:
        findings = scan_source(f"Run `{_DEFECTIVE}` from the clone.\n", "docs/x.md")
        assert len(findings) == 1
        assert findings[0].argv.endswith("origin/main")

    def test_upstream_remote_counts_as_a_start_point(self) -> None:
        assert is_defective(" -b feature ../wt upstream/main")

    def test_capital_b_creates_a_branch_too(self) -> None:
        assert is_defective(" -B feature ../wt origin/main")

    def test_scan_tree_walks_both_roots(self, tmp_path: Path) -> None:
        _plant(tmp_path, "skills/a/SKILL.md", _fence(_DEFECTIVE))
        _plant(tmp_path, "docs/b.md", _fence(_DEFECTIVE))
        assert [f.path for f in scan_tree(tmp_path)] == ["docs/b.md", "skills/a/SKILL.md"]


class TestCleanShapes:
    @pytest.mark.parametrize(
        "argv",
        [
            f" -b feature {NO_TRACK} ../wt origin/main",
            " ../wt origin/main",  # detached checkout — creates no branch
            " -b feature ../wt HEAD",  # local start point, nothing to inherit
            " -b feature ../wt",  # no start point at all
            "",  # a bare prose mention of the command
        ],
    )
    def test_shape_cannot_reach_the_defect(self, argv: str) -> None:
        assert not is_defective(argv)

    def test_no_track_on_a_joined_continuation_counts(self) -> None:
        body = f"git worktree add -b feature \\\n  {NO_TRACK} ../wt origin/main"
        assert scan_source(_fence(body), "skills/x/SKILL.md") == []

    def test_a_trailing_continuation_still_gets_scanned(self) -> None:
        # The source ends mid-continuation, so the flush is the only thing that emits it.
        lines = logical_lines("```bash\ngit worktree add -b f ../wt \\\n")
        assert lines == [(2, "git worktree add -b f ../wt", True)]

    def test_a_repo_without_the_scanned_roots_yields_nothing(self, tmp_path: Path) -> None:
        _plant(tmp_path, "notes/x.md", _fence(_DEFECTIVE))
        assert collect_files(tmp_path) == []
        assert scan_tree(tmp_path) == []


class TestFragmentBounding:
    def test_a_flag_from_another_quoted_command_does_not_leak(self) -> None:
        line = "After `git worktree add <path> origin/<branch>` a later `git checkout -B <branch>` fails.\n"
        assert scan_source(line, "skills/workspace/references/troubleshooting.md") == []

    def test_a_trailing_comment_is_not_argv(self) -> None:
        findings = scan_source(_fence(f"{_DEFECTIVE}   # remember {NO_TRACK}"), "skills/x/SKILL.md")
        assert len(findings) == 1, "a --no-track sitting in a comment must not clear the recipe"

    def test_a_chained_second_command_is_cut_off(self) -> None:
        assert invocations_in("git worktree add ../wt origin/main && cd ../wt -b x") == [" ../wt origin/main "]

    def test_two_recipes_in_one_fenced_line_are_both_read(self) -> None:
        assert len(invocations_in(f"{_CLEAN} ; {_DEFECTIVE}")) == 2


class TestPragma:
    def test_allow_pragma_suppresses_the_line(self) -> None:
        body = f"Quoting the defect: `{_DEFECTIVE}` <!-- {ALLOW_PRAGMA} — this IS the defect -->\n"
        assert scan_source(body, "skills/x/SKILL.md") == []


class TestRun:
    def test_clean_tree_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _plant(tmp_path, "skills/a/SKILL.md", _fence(_CLEAN))
        assert run(tmp_path) == 0
        assert "OK" in capsys.readouterr().out

    def test_defective_tree_exits_one_and_names_the_site(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _plant(tmp_path, "skills/a/SKILL.md", _fence(_DEFECTIVE))
        assert run(tmp_path) == 1
        assert "skills/a/SKILL.md:4" in capsys.readouterr().out


class TestFinding:
    def test_message_carries_the_remedy_and_the_escape(self) -> None:
        message = Finding(path="skills/x/SKILL.md", line_no=7, argv=" -b f ../wt origin/main").message
        assert "skills/x/SKILL.md:7" in message
        assert NO_TRACK in message
        assert ALLOW_PRAGMA in message
