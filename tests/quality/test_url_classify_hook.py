"""Conformance ledger for the PR/MR-URL-classification-regex ban hook.

The green-on-tree assertion lets the checker be a trusted blocking gate; the
anti-vacuous flag/allow pair proves it bites and does not over-block the forge
API-endpoint matchers it must leave alone.
"""

import pathlib

import pytest

import scripts.hooks.check_url_classify as checker

_URL_CLASSIFY_PATTERN = 'import re\n_RE = re.compile(r"https?://[^/]+/(?:merge_requests|pull|pulls)/\\d+")\n'
_GITLAB_MR_SHAPE = 'import re\n_RE = re.compile(r"^/(?P<project>[^?#]+?)/-/merge_requests/(?P<iid>\\d+)/?$")\n'
_API_ENDPOINT_PATTERN = 'import re\n_RE = re.compile(r"(?:merge_requests|pulls)/\\d+/merge\\b")\n'
_URL_CONSTRUCTOR = 'def link(repo, n):\n    return f"https://gitlab.com/{repo}/-/merge_requests/{n}"\n'


@pytest.fixture
def src_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "src" / "teatree" / "loop" / "scanners" / "thing.py"
    target.parent.mkdir(parents=True)
    return target


class TestCheckerBehavior:
    def test_green_on_real_tree(self) -> None:
        assert checker.main(["--all"]) == 0

    def test_flags_url_classification_regex_outside_url_classify(
        self, src_file: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src_file.write_text(_URL_CLASSIFY_PATTERN, encoding="utf-8")
        assert checker.main([str(src_file)]) == 1
        assert "teatree.url_classify" in capsys.readouterr().err

    def test_flags_gitlab_mr_shape_regex(self, src_file: pathlib.Path) -> None:
        src_file.write_text(_GITLAB_MR_SHAPE, encoding="utf-8")
        assert checker.main([str(src_file)]) == 1

    def test_allows_pattern_inside_url_classify(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "url_classify.py"
        target.parent.mkdir(parents=True)
        target.write_text(_URL_CLASSIFY_PATTERN, encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_allows_pattern_inside_backends(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "teatree" / "backends" / "gitlab.py"
        target.parent.mkdir(parents=True)
        target.write_text(_GITLAB_MR_SHAPE, encoding="utf-8")
        assert checker.main([str(target)]) == 0

    def test_does_not_flag_api_endpoint_matcher(self, src_file: pathlib.Path) -> None:
        src_file.write_text(_API_ENDPOINT_PATTERN, encoding="utf-8")
        assert checker.main([str(src_file)]) == 0

    def test_does_not_flag_url_constructor_fstring(self, src_file: pathlib.Path) -> None:
        src_file.write_text(_URL_CONSTRUCTOR, encoding="utf-8")
        assert checker.main([str(src_file)]) == 0

    def test_tests_directory_never_scanned(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "tests" / "test_thing.py"
        target.parent.mkdir(parents=True)
        target.write_text(_URL_CLASSIFY_PATTERN, encoding="utf-8")
        assert checker.main([str(target)]) == 0
