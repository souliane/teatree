"""The scheduled metered eval skips cleanly when nothing new merged.

The weekly cron should not spend API budget when there is nothing new to test.
``merged_prs_since.py`` is the pre-check: given the repo's merged PRs (their
``merged_at`` timestamps) and a lookback window, it answers whether ANY PR
merged inside the window. Exit 0 → run the eval (there is new work); exit
``--skip-code`` → skip cleanly (nothing new). It is a pure decision function —
the platform query (``gh api`` / ``glab api``) stays in the CI YAML.

This is a PRE-CHECK that decides whether to invoke the eval at all. It is NOT a
skip-as-pass inside the eval: once the eval is invoked, ``--require-executed``
still makes it fail loud if it cannot execute.
"""

import datetime as dt
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "merged_prs_since",
    Path(__file__).parents[3] / "scripts" / "eval" / "merged_prs_since.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

any_merged_since = _MOD.any_merged_since
main = _MOD.main

NOW = dt.datetime(2026, 6, 7, 12, 0, tzinfo=dt.UTC)


def _pr(merged_at: str | None) -> dict:
    return {"number": 1, "merged_at": merged_at}


class TestAnyMergedSince:
    def test_pr_merged_inside_window_returns_true(self) -> None:
        prs = [_pr("2026-06-05T09:00:00Z")]  # 2 days ago
        assert any_merged_since(prs, now=NOW, days=7) is True

    def test_pr_merged_before_window_returns_false(self) -> None:
        prs = [_pr("2026-05-20T09:00:00Z")]  # ~18 days ago
        assert any_merged_since(prs, now=NOW, days=7) is False

    def test_unmerged_pr_is_ignored(self) -> None:
        prs = [_pr(None)]  # open PR, never merged
        assert any_merged_since(prs, now=NOW, days=7) is False

    def test_empty_list_returns_false(self) -> None:
        assert any_merged_since([], now=NOW, days=7) is False

    def test_one_recent_among_old_returns_true(self) -> None:
        prs = [_pr("2026-05-01T09:00:00Z"), _pr("2026-06-06T09:00:00Z")]
        assert any_merged_since(prs, now=NOW, days=7) is True

    def test_boundary_just_inside_window_is_true(self) -> None:
        prs = [_pr("2026-05-31T13:00:00Z")]  # ~6d23h ago, inside 7d
        assert any_merged_since(prs, now=NOW, days=7) is True

    def test_garbage_timestamp_is_ignored(self) -> None:
        prs = [{"number": 2, "merged_at": "not-a-date"}]
        assert any_merged_since(prs, now=NOW, days=7) is False


class TestMain:
    def test_exit_zero_when_something_merged(self, tmp_path: Path) -> None:
        prs_file = tmp_path / "prs.json"
        prs_file.write_text(json.dumps([_pr("2026-06-06T09:00:00Z")]), encoding="utf-8")
        code = main(["--prs-file", str(prs_file), "--days", "7", "--now", NOW.isoformat()])
        assert code == 0

    def test_exit_skip_code_when_nothing_merged(self, tmp_path: Path) -> None:
        prs_file = tmp_path / "prs.json"
        prs_file.write_text(json.dumps([_pr("2026-05-01T09:00:00Z")]), encoding="utf-8")
        code = main(["--prs-file", str(prs_file), "--days", "7", "--now", NOW.isoformat(), "--skip-code", "1"])
        assert code == 1

    def test_non_list_payload_errors(self, tmp_path: Path) -> None:
        prs_file = tmp_path / "prs.json"
        prs_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        code = main(["--prs-file", str(prs_file), "--days", "7", "--now", NOW.isoformat()])
        assert code == 2
