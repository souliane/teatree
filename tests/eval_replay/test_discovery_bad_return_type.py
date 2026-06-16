"""Discovery must isolate per-overlay failures when the hook returns a non-Path.

A misconfigured overlay returning a non-Path (string, bool, int, MagicMock,
etc.) must NOT crash discovery — only that one overlay is skipped, the rest
of the catalog still loads. Mirrors the per-overlay try/except guard for
``get_eval_scenarios_dir()``.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.eval.discovery import _discover_overlay_specs


def _fake_overlay(return_value: object) -> SimpleNamespace:
    return SimpleNamespace(get_eval_scenarios_dir=lambda: return_value)


@pytest.mark.parametrize(
    "bad_value",
    [
        "not a path",  # plain string
        True,  # bool
        42,  # int
        object(),  # arbitrary instance
        ["/tmp"],  # list (no fspath)
    ],
)
def test_skips_overlay_returning_non_path(bad_value: object) -> None:
    overlay = _fake_overlay(bad_value)
    with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"t3-bad": overlay}):
        specs = _discover_overlay_specs()
    assert specs == []


def test_other_overlays_still_load_when_one_returns_bad_type(tmp_path: Path) -> None:
    good_dir = tmp_path / "good" / "scenarios"
    good_dir.mkdir(parents=True)
    (good_dir / "good_one.yaml").write_text(
        "- name: good_one\n"
        "  scenario: example\n"
        "  prompt: do the thing\n"
        "  expect:\n"
        "    - tool_call: bash\n"
        '      args.command: contains "git worktree add"\n',
        encoding="utf-8",
    )
    good = _fake_overlay(good_dir)
    bad = _fake_overlay("not a path at all")
    with patch(
        "teatree.core.overlay_loader.get_all_overlays",
        return_value={"t3-good": good, "t3-bad": bad},
    ):
        specs = _discover_overlay_specs()
    assert [s.name for s in specs] == ["good_one"]
