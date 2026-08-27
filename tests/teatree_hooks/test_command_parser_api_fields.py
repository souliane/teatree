"""Attached ``gh``/``glab api`` field spellings reach the body and secret surfaces.

``--field=body=x`` / ``-fbody=x`` are pflag-accepted spellings of the spaced
form, so detection classifying the call a publish while extraction yielded an
empty payload left every body-based leak gate scanning nothing.
"""

from pathlib import Path

import pytest

from teatree.hooks._command_parser import extract_bash_payload, extract_secret_scan_text


class TestAttachedFieldBodyReachesThePayload:
    @pytest.mark.parametrize(
        "flag",
        [
            "--field=body=customer-secret",
            "--raw-field=body=customer-secret",
            "-fbody=customer-secret",
            "-Fbody=customer-secret",
        ],
    )
    def test_attached_body_field_is_extracted(self, flag: str) -> None:
        assert "customer-secret" in extract_bash_payload(f"gh api repos/o/r/issues -X POST {flag}")


class TestAttachedFieldValueReachesTheSecretScan:
    @pytest.mark.parametrize(
        "flag",
        ["--field=title=glpat-DEADBEEF", "--raw-field=title=glpat-DEADBEEF", "-ftitle=glpat-DEADBEEF"],
    )
    def test_attached_non_body_field_is_scanned(self, flag: str) -> None:
        assert "glpat-DEADBEEF" in extract_secret_scan_text(f"gh api repos/o/r/issues -X POST {flag}")


class TestShortFDisambiguation:
    """An ``=``-free ``-F<value>`` stays the ``--body-file`` short form, not a field assignment."""

    def test_attached_short_f_body_file_still_resolves_to_its_file(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("drafted body text", encoding="utf-8")
        assert "drafted body text" in extract_bash_payload(f"gh pr create -F{body}")
