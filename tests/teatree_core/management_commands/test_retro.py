"""``t3 <overlay> retro review-findings`` — classify A/B/C, file class-C gates (#1573).

Fetch is mocked at the resolved code host. The classification verdicts are
supplied via a JSON file (the scaffold never guesses A/B/C). The tests assert:
a class-C finding files exactly one enforcement issue while A/B file nothing;
the filed body is banned-terms-safe and uses a clickable PR link (no bare
``#N``); a re-run does not refile; and the summary reports correct per-class
counts + links.
"""

import json
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core.review import review_findings as rf_mod
from teatree.core.review.review_findings import ReviewFinding
from tests.teatree_core.conftest import CommandOverlay

_PR_URL = "https://github.com/souliane/teatree/pull/1573"
_REPO = "souliane/teatree"
_MOCK_OVERLAY = {"test": CommandOverlay()}

_COMMENTS = [
    {"body": "This is already enforced by the lint gate.", "path": "a.py", "line": 1, "user": {"login": "rev"}},
    {"body": "Typo in a one-off comment.", "path": "b.py", "line": 2, "user": {"login": "rev"}},
    {"body": "Prefer composition over this mixin pattern.", "path": "c.py", "line": 3, "user": {"login": "rev"}},
]


def _fingerprint(body: str, *, path: str, line: int) -> str:
    return ReviewFinding(body=body, path=path, line=line, author="rev").fingerprint


_FP_A = _fingerprint(_COMMENTS[0]["body"], path="a.py", line=1)
_FP_B = _fingerprint(_COMMENTS[1]["body"], path="b.py", line=2)
_FP_C = _fingerprint(_COMMENTS[2]["body"], path="c.py", line=3)


def _verdicts_file(tmp_path: Path) -> Path:
    path = tmp_path / "verdicts.json"
    path.write_text(
        json.dumps(
            {
                _FP_A: {"class": "A"},
                _FP_B: {"class": "B"},
                _FP_C: {"class": "C", "enforcement": "Add a structural-design review test."},
            }
        ),
        encoding="utf-8",
    )
    return path


class RetroReviewFindingsTest(TestCase):
    def _run(self, *args: str, store_dir: Path, **kwargs: object) -> dict[str, object]:
        with patch.object(rf_mod, "get_data_dir", return_value=store_dir):
            output = call_command("retro", "review-findings", *args, stdout=StringIO(), **kwargs)
        return cast("dict[str, object]", json.loads(output))

    def test_lists_findings_with_fingerprints(self) -> None:
        host = MagicMock()
        host.list_pr_comments.return_value = _COMMENTS
        store_dir = Path(self._tmp())
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = self._run(_PR_URL, store_dir=store_dir)
        host.list_pr_comments.assert_called_once_with(repo=_REPO, pr_iid=1573)
        fingerprints = {f["fingerprint"] for f in cast("list[dict[str, object]]", result["findings"])}
        assert fingerprints == {_FP_A, _FP_B, _FP_C}

    def test_files_only_class_c_with_clean_payload(self) -> None:
        store_dir = Path(self._tmp())
        verdicts = _verdicts_file(store_dir)
        host = MagicMock()
        host.list_pr_comments.return_value = _COMMENTS
        host.search_open_issues.return_value = []
        host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/2000"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = self._run(_PR_URL, classification=str(verdicts), store_dir=store_dir)

        # Only the class-C finding files an issue; A and B file nothing.
        host.create_issue.assert_called_once()
        kwargs = host.create_issue.call_args.kwargs
        assert kwargs["repo"] == _REPO
        body = kwargs["body"]
        # Clickable PR link, no bare ref the command authored.
        assert "[review thread](https://github.com/souliane/teatree/pull/1573)" in body
        assert rf_mod.find_bare_references(body) == []
        # Summary reports correct per-class counts + filed link.
        assert result["counts"] == {"A": 1, "B": 1, "C": 1}
        filed = cast("list[dict[str, object]]", result["filed"])
        assert len(filed) == 1
        assert filed[0]["url"] == "https://github.com/souliane/teatree/issues/2000"
        assert filed[0]["already_filed"] is False

    def test_rerun_does_not_refile(self) -> None:
        store_dir = Path(self._tmp())
        verdicts = _verdicts_file(store_dir)
        host = MagicMock()
        host.list_pr_comments.return_value = _COMMENTS
        host.search_open_issues.return_value = []
        host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/2000"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            self._run(_PR_URL, classification=str(verdicts), store_dir=store_dir)
            filed_body = host.create_issue.call_args.kwargs["body"]
            host.reset_mock()
            host.list_pr_comments.return_value = _COMMENTS
            # The second run sees the already-filed issue via search.
            host.search_open_issues.return_value = [
                {"html_url": "https://github.com/souliane/teatree/issues/2000", "body": filed_body}
            ]
            result = self._run(_PR_URL, classification=str(verdicts), store_dir=store_dir)

        host.create_issue.assert_not_called()
        filed = cast("list[dict[str, object]]", result["filed"])
        assert filed[0]["already_filed"] is True

    def test_errors_on_non_pr_url(self) -> None:
        store_dir = Path(self._tmp())
        result = self._run("https://github.com/souliane/teatree/issues/1573", store_dir=store_dir)
        assert result == {"error": "Not a recognised PR/MR URL: https://github.com/souliane/teatree/issues/1573"}

    def test_errors_when_no_host_resolves(self) -> None:
        store_dir = Path(self._tmp())
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=None),
        ):
            result = self._run(_PR_URL, store_dir=store_dir)
        assert result == {"error": f"No code host could be resolved for {_PR_URL}"}

    def test_banned_terms_safe_payload(self) -> None:
        """The command-authored body trips no banned-terms gate when inputs are clean."""
        import tempfile  # noqa: PLC0415

        from teatree.hooks import banned_terms_scanner  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = tmp_path / ".teatree.toml"
            cfg.write_text('[teatree]\nbanned_terms = ["acmecorp"]\n', encoding="utf-8")
            verdicts = tmp_path / "verdicts.json"
            verdicts.write_text(
                json.dumps({_FP_C: {"class": "C", "enforcement": "Add a structural-design review test."}}),
                encoding="utf-8",
            )
            host = MagicMock()
            host.list_pr_comments.return_value = _COMMENTS
            host.search_open_issues.return_value = []
            host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/2000"}
            with (
                patch.object(rf_mod, "get_data_dir", return_value=tmp_path / "store"),
                patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
                patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            ):
                call_command("retro", "review-findings", _PR_URL, classification=str(verdicts), stdout=StringIO())

            body = host.create_issue.call_args.kwargs["body"]
            assert banned_terms_scanner.scan_text(body, config_path=cfg) is None

    def test_untrusted_finding_bare_refs_neutralized_in_filed_payload(self) -> None:
        """A finding body with bare refs files a payload that is bare-ref clean.

        Asserts on the ACTUAL body passed to ``create_issue`` (the published
        payload), not the scaffold — the untrusted comment is the leak vector.
        """
        comments = [
            {
                "body": "Same recurrence as #1234 / !99 / ts 1716900000.123456 — see https://github.com/x/y/issues/3",
                "path": "c.py",
                "line": 9,
                "user": {"login": "rev"},
            }
        ]
        fp = _fingerprint(str(comments[0]["body"]), path="c.py", line=9)
        store_dir = Path(self._tmp())
        verdicts = store_dir / "verdicts.json"
        verdicts.write_text(json.dumps({fp: {"class": "C", "enforcement": "Add a gate."}}), encoding="utf-8")
        host = MagicMock()
        host.list_pr_comments.return_value = comments
        host.search_open_issues.return_value = []
        host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/2001"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            self._run(_PR_URL, classification=str(verdicts), store_dir=store_dir)

        host.create_issue.assert_called_once()
        sent = host.create_issue.call_args.kwargs
        assert rf_mod.find_bare_references(sent["body"]) == []
        assert rf_mod.find_bare_references(sent["title"]) == []

    def test_untrusted_finding_with_banned_term_is_withheld(self) -> None:
        """A finding whose body carries a banned term is withheld — never filed."""
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        comments = [
            {
                "body": "This breaks the acmecorp tenant onboarding flow",
                "path": "c.py",
                "line": 9,
                "user": {"login": "rev"},
            }
        ]
        fp = _fingerprint(str(comments[0]["body"]), path="c.py", line=9)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = tmp_path / ".teatree.toml"
            cfg.write_text('[teatree]\nbanned_terms = ["acmecorp"]\n', encoding="utf-8")
            verdicts = tmp_path / "verdicts.json"
            verdicts.write_text(json.dumps({fp: {"class": "C", "enforcement": "Add a gate."}}), encoding="utf-8")
            host = MagicMock()
            host.list_pr_comments.return_value = comments
            host.search_open_issues.return_value = []
            with (
                patch.dict(os.environ, {"T3_BANNED_TERMS_CONFIG": str(cfg)}),
                patch.object(rf_mod, "get_data_dir", return_value=tmp_path / "store"),
                patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
                patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            ):
                result = self._run(_PR_URL, classification=str(verdicts), store_dir=tmp_path / "store")

        host.create_issue.assert_not_called()
        filed = cast("list[dict[str, object]]", result["filed"])
        assert filed[0]["withheld"] is True
        assert "acmecorp" in str(filed[0]["withheld_reason"])

    @staticmethod
    def _tmp() -> str:
        import tempfile  # noqa: PLC0415

        return tempfile.mkdtemp()
