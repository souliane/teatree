"""Fitness function: skills don't regress to raw ``gh``/``glab``/``sentry-cli`` calls (#35).

Forward-guard for the MCP-serves-everything migration (umbrella #3076). The
current raw-call surface is grandfathered as a per-item LEDGER
(``[tool.teatree.skill_cli_ratchet] baseline_file``); the gate turns RED only
when a NEW shell-out appears or a ledgered key no longer occurs (forced banking).

Load-bearing halves:

:class:`TestLiveTree` is the gate itself — no live raw call is un-grandfathered
and no ledger key is stale, and the committed ledger equals the live set.

:class:`TestUnknownCall` / :class:`TestForcedBanking` are the anti-vacuity
proofs: a NEW raw ``gh`` call not in the ledger is RED (named), a stale ledger
key is RED.

:class:`TestProhibitionAndPragma` proves the classifier does NOT flag a
prohibition example ("never ``gh pr merge``") or a ``mcp-ratchet: allow`` line —
the trap the migration must not invert.

:class:`TestCodeContext` proves a bare prose mention of ``gh`` is not an
invocation, only a fenced/inline-backtick command is.
"""

from pathlib import Path

import pytest

from teatree.quality import skill_cli_ratchet
from teatree.quality.skill_cli_ratchet import (
    ALLOW_PRAGMA,
    Ledger,
    RatchetConfig,
    RatchetReport,
    RawCall,
    build_report,
    find_raw_calls,
    is_prohibition,
    load_config,
    raw_calls_in,
    run,
    signatures_in_fragment,
    update_baseline,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _plant_skill(root: Path, name: str, body: str) -> Path:
    path = root / "skills" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


_FENCED_CALL = "```bash\ngh issue create --title x --body y\n```\n"


class TestLiveTree:
    def test_live_tree_ledger_is_exact(self) -> None:
        config = load_config(_REPO_ROOT / "pyproject.toml")
        report = build_report(root=_REPO_ROOT, config=config)
        assert not report.failed, (
            f"{len(report.unknown_calls)} new raw call(s):\n"
            + "\n".join(report.summary_lines())
            + f"\n{len(report.stale_entries)} stale ledger entry(ies):\n"
            + "\n".join(report.stale_lines())
        )

    def test_committed_ledger_matches_live_set(self) -> None:
        config = load_config(_REPO_ROOT / "pyproject.toml")
        report = build_report(root=_REPO_ROOT, config=config)
        assert config.grandfathered == report.live_keys

    def test_ledger_is_non_empty(self) -> None:
        config = load_config(_REPO_ROOT / "pyproject.toml")
        assert config.grandfathered, "an empty ledger makes every live raw call an un-grandfathered RED"


class TestUnknownCall:
    def test_new_raw_call_not_in_ledger_is_red_and_named(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "probe/SKILL.md", _FENCED_CALL)
        report = build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset()))
        assert report.failed
        assert [call.key for call in report.unknown_calls] == ["skills/probe/SKILL.md::gh issue create"]
        assert "skills/probe/SKILL.md" in "\n".join(report.summary_lines())

    def test_grandfathered_call_is_not_flagged(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "probe/SKILL.md", _FENCED_CALL)
        config = RatchetConfig(grandfathered=frozenset({"skills/probe/SKILL.md::gh issue create"}))
        assert not build_report(root=tmp_path, config=config).failed


class TestForcedBanking:
    def test_stale_ledger_key_is_red(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "probe/SKILL.md", "no commands here\n")
        config = RatchetConfig(grandfathered=frozenset({"skills/probe/SKILL.md::gh issue create"}))
        report = build_report(root=tmp_path, config=config)
        assert report.failed
        assert report.stale_entries == ("skills/probe/SKILL.md::gh issue create",)
        assert "remove it from the ledger" in "\n".join(report.stale_lines())

    def test_migrated_call_leaves_a_stale_key(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "probe/SKILL.md", "Use `mcp__teatree__github_issue` instead.\n")
        config = RatchetConfig(grandfathered=frozenset({"skills/probe/SKILL.md::gh issue view"}))
        assert build_report(root=tmp_path, config=config).failed


class TestProhibitionAndPragma:
    def test_prohibition_example_is_not_a_raw_call(self, tmp_path: Path) -> None:
        body = "Never merge by hand:\n\n```bash\ngh pr merge 5   # FORBIDDEN — mechanically refused\n```\n"
        _plant_skill(tmp_path, "probe/SKILL.md", body)
        assert build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset())).raw_calls == ()

    def test_prohibition_marker_on_nearby_line_excludes(self, tmp_path: Path) -> None:
        body = "```bash\n# never do this\ngh issue create --title x\n```\n"
        _plant_skill(tmp_path, "probe/SKILL.md", body)
        assert build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset())).raw_calls == ()

    def test_allow_pragma_line_is_excluded(self, tmp_path: Path) -> None:
        body = f"    gh run watch  <!-- {ALLOW_PRAGMA}: ratified long-running exception -->\n"
        _plant_skill(tmp_path, "probe/SKILL.md", "```bash\n" + body + "```\n")
        assert build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset())).raw_calls == ()

    @pytest.mark.parametrize("marker", ["forbidden", "never", "mechanically refused", "do not", "instead of"])
    def test_markers_classify_as_prohibition(self, marker: str) -> None:
        assert is_prohibition([f"the {marker} rule", "gh pr merge 5"], 1)

    def test_absent_marker_is_not_prohibition(self) -> None:
        assert not is_prohibition(["fetch the PR state", "gh pr view 5"], 1)


class TestCodeContext:
    def test_prose_mention_of_command_is_not_flagged(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "probe/SKILL.md", "The gh CLI handles auth for you automatically.\n")
        assert find_raw_calls(tmp_path) == []

    def test_backticked_tool_name_alone_is_not_an_invocation(self) -> None:
        assert raw_calls_in("Prefer the `gh` tool over `glab` when possible.\n", "skills/x.md") == []

    def test_inline_backtick_command_is_flagged(self) -> None:
        calls = raw_calls_in("Read it with `gh pr view <N> --json state`.\n", "skills/x.md")
        assert [c.signature for c in calls] == ["gh pr view"]

    def test_fenced_comment_line_is_ignored(self) -> None:
        source = "```bash\n# gh api returns the title field\ngh issue view <N>\n```\n"
        assert [c.signature for c in raw_calls_in(source, "skills/x.md")] == ["gh issue view"]

    def test_github_word_is_not_the_gh_command(self) -> None:
        assert raw_calls_in("Call `mcp__teatree__github_issue(url)` first.\n", "skills/x.md") == []


class TestSignatures:
    @pytest.mark.parametrize(
        ("fragment", "expected"),
        [
            ("gh issue view <N> --repo x/y", ["gh issue view"]),
            ("gh --repo souliane/teatree issue create --title x", ["gh issue create"]),
            ("glab mr list --source-branch b", ["glab mr list"]),
            ("gh api repos/x/y/issues", ["gh api"]),
            ("sentry-cli issues list", ["sentry-cli issues list"]),
            ("gh auth status", ["gh auth status"]),
        ],
    )
    def test_signature_extraction(self, fragment: str, expected: list[str]) -> None:
        assert signatures_in_fragment(fragment) == expected

    def test_bare_command_yields_no_signature(self) -> None:
        assert signatures_in_fragment("gh") == []


class TestLedgerConfig:
    def test_baseline_file_resolves_relative_to_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.teatree.skill_cli_ratchet]\nbaseline_file = "sub/ledger.txt"\n', encoding="utf-8"
        )
        assert Ledger.path_for(tmp_path / "pyproject.toml") == tmp_path / "sub" / "ledger.txt"

    def test_missing_baseline_file_yields_empty_config(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "pyproject.toml")
        assert config.grandfathered == frozenset()
        assert config.mode == "warn"

    def test_ledger_round_trips_ignoring_comments_and_blanks(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.txt"
        Ledger.write(ledger, ["skills/b.md::gh pr view", "skills/a.md::gh issue view"])
        assert Ledger.load(ledger) == frozenset({"skills/a.md::gh issue view", "skills/b.md::gh pr view"})

    def test_write_is_sorted_and_deduplicated(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.txt"
        Ledger.write(ledger, ["skills/z.md::gh x", "skills/a.md::gh x", "skills/a.md::gh x"])
        body = [line for line in ledger.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        assert body == ["skills/a.md::gh x", "skills/z.md::gh x"]


class TestCollisionPin:
    def test_disjoint_ledger_edits_union_stays_green(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "a/SKILL.md", "```bash\ngh pr view <N>\n```\n")
        _plant_skill(tmp_path, "b/SKILL.md", "```bash\ngh pr list --state open\n```\n")
        key_a, key_b = "skills/a/SKILL.md::gh pr view", "skills/b/SKILL.md::gh pr list"
        union = build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset({key_a, key_b})))
        assert not union.failed
        partial = build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset({key_a})))
        assert partial.failed
        assert [c.key for c in partial.unknown_calls] == [key_b]


def _make_repo(root: Path, *, grandfathered: list[str], with_call: bool) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.teatree.skill_cli_ratchet]\nbaseline_file = "ledger.txt"\n', encoding="utf-8"
    )
    Ledger.write(root / "ledger.txt", grandfathered)
    if with_call:
        _plant_skill(root, "probe/SKILL.md", _FENCED_CALL)


class TestRunner:
    def test_run_exits_zero_when_ledger_exact(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _make_repo(tmp_path, grandfathered=["skills/probe/SKILL.md::gh issue create"], with_call=True)
        assert run(tmp_path) == 0
        assert "OK" in capsys.readouterr().out

    def test_run_exits_nonzero_and_names_new_call(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _make_repo(tmp_path, grandfathered=[], with_call=True)
        assert run(tmp_path) == 1
        assert "skills/probe/SKILL.md" in capsys.readouterr().out

    def test_run_exits_nonzero_on_stale_entry(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _make_repo(tmp_path, grandfathered=["skills/gone/SKILL.md::gh issue view"], with_call=False)
        assert run(tmp_path) == 1
        assert "remove it from the ledger" in capsys.readouterr().out

    def test_update_baseline_rewrites_ledger_to_live_set(self, tmp_path: Path) -> None:
        _make_repo(tmp_path, grandfathered=["skills/gone/SKILL.md::gh issue view"], with_call=True)
        assert update_baseline(tmp_path) == 0
        assert load_config(tmp_path / "pyproject.toml").grandfathered == frozenset(
            {"skills/probe/SKILL.md::gh issue create"}
        )

    def test_update_baseline_reports_missing_config(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.teatree]\n", encoding="utf-8")
        assert update_baseline(tmp_path) == 1
        assert "no [tool.teatree.skill_cli_ratchet]" in capsys.readouterr().out

    def test_repo_root_resolves_to_a_real_pyproject(self) -> None:
        assert (skill_cli_ratchet._repo_root() / "pyproject.toml").is_file()


class TestDegradation:
    def test_main_on_real_tree_is_green(self) -> None:
        assert skill_cli_ratchet._main([]) == 0

    def test_read_unreadable_path_yields_empty(self, tmp_path: Path) -> None:
        assert skill_cli_ratchet._read(tmp_path / "missing.md") == ""

    def test_load_missing_ledger_is_empty(self, tmp_path: Path) -> None:
        assert Ledger.load(tmp_path / "absent.txt") == frozenset()

    def test_no_skills_dir_yields_no_calls(self, tmp_path: Path) -> None:
        assert find_raw_calls(tmp_path) == []


class TestReportShape:
    def test_summary_only_covers_unknown_calls(self) -> None:
        calls = (
            RawCall(path="skills/a.md", signature="gh pr view", line_no=3, text="gh pr view"),
            RawCall(path="skills/b.md", signature="gh issue view", line_no=4, text="gh issue view"),
        )
        report = RatchetReport(raw_calls=calls, grandfathered=frozenset({"skills/a.md::gh pr view"}))
        assert len(report.summary_lines()) == 1
        assert "skills/b.md" in report.summary_lines()[0]

    def test_duplicate_signature_in_file_collapses_to_one_unknown(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "probe/SKILL.md", "```bash\ngh pr view 1\ngh pr view 2\n```\n")
        report = build_report(root=tmp_path, config=RatchetConfig(grandfathered=frozenset()))
        assert [c.key for c in report.unknown_calls] == ["skills/probe/SKILL.md::gh pr view"]
