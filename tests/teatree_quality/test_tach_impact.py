"""The tach pytest plugin's would-skip set, computed report-only (#3672).

Report-only means the plugin's DESELECTION never runs: the handler is driven directly
for its verdict, so this cannot change what pytest collects. That also side-steps the
``NO_TESTS_COLLECTED`` to ``OK`` exit rewrite entirely — that rewrite lives in the
plugin's ``pytest_sessionfinish``, which is never reached here.
"""

from pathlib import Path

import pytest

from teatree.quality import tach_impact
from teatree.quality.tach_impact import TACH_DEFAULT_BASE, would_skip_tests


class TestUnavailableProbeIsNoneNotEmpty:
    """A failed probe must never read as "the plugin would skip nothing"."""

    def test_a_directory_with_no_tach_config_yields_none(self, tmp_path: Path) -> None:
        assert would_skip_tests(tmp_path, candidates=()) is None

    def test_a_raising_probe_yields_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tach_impact, "_load_project_config", _boom)
        assert would_skip_tests(tmp_path, candidates=("tests/a/test_a.py",)) is None


def _boom(_root: Path) -> object:
    message = "tach config exploded"
    raise RuntimeError(message)


class TestLiveRepoProbe:
    """Anti-vacuous: on the real tree the verdict must DISCRIMINATE, not answer uniformly.

    A probe that returns everything (or nothing) proves only that the harness ran. The
    control is the split itself — a test importing a changed module must be kept while
    an unrelated one is skipped.
    """

    def test_the_verdict_splits_the_candidate_set(self) -> None:
        root = Path(__file__).resolve().parents[2]
        candidates = tuple(
            path.relative_to(root).as_posix() for path in sorted((root / "tests" / "teatree_quality").glob("test_*.py"))
        )
        skipped = would_skip_tests(root, candidates=candidates)
        if skipped is None:
            pytest.skip("tach impact probe unavailable in this environment")
        assert 0 < len(skipped) < len(candidates)
        assert set(skipped) <= set(candidates)

    def test_the_default_base_is_the_merge_base_ref_our_selector_uses(self) -> None:
        # A divergent base would make every comparison meaningless.
        assert TACH_DEFAULT_BASE == "origin/main"
