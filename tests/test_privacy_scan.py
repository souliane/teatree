"""Tests for privacy_scan.py."""

import json
from pathlib import Path

import pytest
from privacy_scan import _build_banned_re, _scan_line, main


class TestScanLine:
    def test_detects_email(self) -> None:
        assert any(c == "email" for c, _ in _scan_line("contact user@company.com", None))

    def test_ignores_example_email(self) -> None:
        assert not any(c == "email" for c, _ in _scan_line("contact user@example.com", None))

    def test_detects_home_path(self) -> None:
        assert any(c == "home_path" for c, _ in _scan_line("/Users/jane/workspace", None))

    def test_detects_linux_home_path(self) -> None:
        assert any(c == "home_path" for c, _ in _scan_line("/home/deploy/.ssh", None))

    def test_detects_private_ip(self) -> None:
        assert any(c == "private_ip" for c, _ in _scan_line("connect to 10.0.0.5", None))

    def test_detects_api_key(self) -> None:
        assert any(c == "api_key" for c, _ in _scan_line("glpat-" + "a" * 20, None))  # gitleaks:allow

    def test_detects_github_token(self) -> None:
        assert any(c == "api_key" for c, _ in _scan_line("ghp_" + "a" * 20, None))  # gitleaks:allow

    def test_detects_internal_hostname(self) -> None:
        assert any(c == "internal_hostname" for c, _ in _scan_line("db.internal.corp", None))

    def test_skips_false_positive_hostname(self) -> None:
        assert not any(c == "internal_hostname" for c, _ in _scan_line("placeholder.internal.corp", None))

    def test_detects_banned_term(self) -> None:
        banned_re = _build_banned_re("acme,secretproject")
        assert any(c == "banned_term" for c, _ in _scan_line("deploy to acme staging", banned_re))

    def test_clean_line(self) -> None:
        assert _scan_line("just a normal line of code", None) == []


class TestBuildBannedRe:
    def test_empty_returns_none(self) -> None:
        assert _build_banned_re("") is None

    def test_comma_separated_matches(self) -> None:
        pattern = _build_banned_re("foo,bar")
        assert pattern is not None
        assert pattern.search("this has foo")


class TestMain:
    def test_clean_input(self, tmp_path: Path) -> None:
        f = tmp_path / "clean.txt"
        f.write_text("just normal code\n")
        main(str(f), banned_terms="", strict=True, json_output=False)

    def test_findings_strict_exits_1(self, tmp_path: Path) -> None:
        f = tmp_path / "dirty.txt"
        f.write_text("admin@secret-corp.com\n")
        with pytest.raises(SystemExit, match="1"):
            main(str(f), banned_terms="", strict=True, json_output=False)

    def test_findings_relaxed_exits_0(self, tmp_path: Path) -> None:
        f = tmp_path / "dirty.txt"
        f.write_text("admin@secret-corp.com\n")
        main(str(f), banned_terms="", strict=False, json_output=False)

    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / "dirty.txt"
        f.write_text("/Users/jane/workspace\n")
        with pytest.raises(SystemExit, match="1"):
            main(str(f), banned_terms="", json_output=True, strict=True)
        data = json.loads(capsys.readouterr().out)
        assert data[0]["category"] == "home_path"

    def test_banned_terms(self, tmp_path: Path) -> None:
        f = tmp_path / "dirty.txt"
        f.write_text("deploy to secretcorp staging\n")
        with pytest.raises(SystemExit, match="1"):
            main(str(f), banned_terms="secretcorp,acmeclient", strict=True, json_output=False)
