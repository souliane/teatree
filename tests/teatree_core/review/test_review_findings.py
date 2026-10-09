"""Unit tests for the deterministic ``retro review-findings`` scaffold (#1573).

Pure logic: fingerprint stability, the durable store, the issue-body builder
(clickable-link safe + fingerprint marker), and the dedup-aware filer. The
forge host is a stand-in object recording the calls it received.
"""

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from django.test import TestCase

from teatree.core.review.review_findings import (
    ClassifiedFinding,
    FilingContext,
    FindingClass,
    FindingsStore,
    ReviewFinding,
    build_issue_body,
    build_issue_title,
    file_class_c_issue,
    find_bare_references,
    fingerprint_marker,
    neutralize_bare_references,
    parse_findings,
    process_review_findings,
)
from teatree.hooks import banned_terms_scanner
from tests._send_gate import allow_forge_repos

_CONTEXT = FilingContext(repo="o/r", pr_url="https://github.com/o/r/pull/1")


_SELF = "souliane"


@pytest.fixture
def allow_review_filing_repo() -> None:
    allow_forge_repos("o/r")


def _open_issue(url: str, body: str, *, author: str = _SELF) -> dict[str, object]:
    return {"html_url": url, "title": "t", "body": body, "state": "open", "user": {"login": author}}


class _FakeHost:
    """Minimal CodeHostBackend stand-in: the reads and writes the hygiene facade makes."""

    def __init__(self, *, existing: list[dict[str, object]] | None = None) -> None:
        self.created: list[dict[str, object]] = []
        self.updated: list[tuple[str, str]] = []
        self.issues: dict[str, dict[str, object]] = {str(issue["html_url"]): dict(issue) for issue in (existing or [])}

    def current_user(self) -> str:
        return _SELF

    def list_repo_open_issues(self, *, repo: str) -> list[dict[str, object]]:
        return [dict(issue) for issue in self.issues.values() if issue.get("state") == "open"]

    def get_issue(self, issue_url: str) -> dict[str, object]:
        return dict(self.issues.get(issue_url, {"error": f"not found: {issue_url}"}))

    def repo_for_issue_url(self, issue_url: str) -> str:
        return "o/r"

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, object]:
        self.updated.append((issue_url, body))
        self.issues.setdefault(issue_url, {})["body"] = body
        return {"html_url": issue_url}

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, object]:
        number = len(self.created) + 100
        record = {"repo": repo, "title": title, "body": body, "labels": labels}
        self.created.append(record)
        url = f"https://github.com/{repo}/issues/{number}"
        self.issues[url] = _open_issue(url, body)
        return {"html_url": url, "number": number}


def _finding(body: str = "Use a context manager here.", *, path: str = "src/a.py", line: int = 12) -> ReviewFinding:
    return ReviewFinding(body=body, path=path, line=line, author="reviewer")


class TestFingerprint:
    def test_is_stable_across_whitespace_and_case(self) -> None:
        a = _finding(body="Use a CONTEXT manager   here.")
        b = _finding(body="use a context manager here.")
        assert a.fingerprint == b.fingerprint

    def test_differs_on_different_line(self) -> None:
        assert _finding(line=12).fingerprint != _finding(line=13).fingerprint

    def test_differs_on_different_body(self) -> None:
        assert _finding(body="one").fingerprint != _finding(body="two").fingerprint


class TestParseFindings:
    def test_drops_empty_bodies(self) -> None:
        findings = parse_findings([{"body": "  "}, {"body": "real", "path": "x.py", "line": 3}])
        assert len(findings) == 1
        assert findings[0].body == "real"

    def test_reads_github_and_gitlab_author(self) -> None:
        findings = parse_findings(
            [
                {"body": "gh", "user": {"login": "octocat"}},
                {"body": "gl", "author": {"username": "tanuki"}},
            ]
        )
        assert {f.author for f in findings} == {"octocat", "tanuki"}


class TestIssueBody:
    def test_carries_fingerprint_marker_and_clickable_pr_link(self) -> None:
        finding = _finding()
        body = build_issue_body(
            finding=finding,
            enforcement="Add a pre-commit hook that rejects bare resource handles.",
            pr_url="https://github.com/souliane/teatree/pull/1573",
        )
        assert f"retro-finding-fingerprint: {finding.fingerprint}" in body
        assert "[review thread](https://github.com/souliane/teatree/pull/1573)" in body
        assert "#1573" not in body  # no bare ref

    def test_title_is_scoped_and_short(self) -> None:
        title = build_issue_title(_finding(body="Prefer composition over this mixin. More context here."))
        assert title.startswith("Enforcement gate for recurring review finding:")
        assert "More context" not in title


class TestFindingsStore:
    def test_records_and_reloads_verdicts(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        finding = _finding()
        store.record(
            "https://github.com/o/r/pull/1",
            [ClassifiedFinding(finding=finding, classification=FindingClass.C)],
        )
        assert store.load("https://github.com/o/r/pull/1") == {finding.fingerprint: "C"}

    def test_recurring_fingerprints_across_prs(self, tmp_path: Path) -> None:
        store = FindingsStore(data_dir=tmp_path)
        finding = _finding()
        store.record("https://github.com/o/r/pull/1", [ClassifiedFinding(finding, FindingClass.B)])
        store.record("https://github.com/o/r/pull/2", [ClassifiedFinding(finding, FindingClass.C)])
        assert finding.fingerprint in store.recurring_fingerprints(min_occurrences=2)
        once = _finding(body="seen once")
        assert once.fingerprint not in store.recurring_fingerprints(min_occurrences=2)


@pytest.mark.usefixtures("allow_review_filing_repo", "configured_banned_term_registry")
class TestFiler(TestCase):
    def test_files_when_no_existing_issue(self) -> None:
        host = _FakeHost()
        filed = file_class_c_issue(
            host,
            finding=_finding(),
            enforcement="Add a gate.",
            context=_CONTEXT,
        )
        assert not filed.already_filed
        assert filed.url.startswith("https://github.com/o/r/issues/")
        assert len(host.created) == 1

    def test_dedups_against_existing_marker(self) -> None:
        finding = _finding()
        existing = [_open_issue("https://github.com/o/r/issues/9", fingerprint_marker(finding.fingerprint))]
        host = _FakeHost(existing=existing)
        filed = file_class_c_issue(
            host,
            finding=finding,
            enforcement="Add a gate.",
            context=_CONTEXT,
        )
        assert filed.already_filed
        assert filed.url == "https://github.com/o/r/issues/9"
        assert host.created == []

    def test_a_marked_ticket_gains_this_recurrence(self) -> None:
        # #162 Rule 1: a fitting ticket is EXTENDED, where the search-index dedupe
        # used to drop the re-file on the floor and record nothing.
        finding = _finding()
        existing = [_open_issue("https://github.com/o/r/issues/9", fingerprint_marker(finding.fingerprint))]
        host = _FakeHost(existing=existing)
        file_class_c_issue(host, finding=finding, enforcement="Add a gate.", context=_CONTEXT)
        assert [url for url, _ in host.updated] == ["https://github.com/o/r/issues/9"]

    def test_an_unmarked_ticket_is_not_reused_and_is_recorded_as_rejected(self) -> None:
        host = _FakeHost(existing=[_open_issue("https://github.com/o/r/issues/9", "unrelated")])
        filed = file_class_c_issue(host, finding=_finding(), enforcement="Add a gate.", context=_CONTEXT)
        assert not filed.already_filed
        assert len(host.created) == 1
        assert "https://github.com/o/r/issues/9" in str(host.created[0]["body"])

    def test_a_marked_ticket_someone_else_filed_is_neither_edited_nor_duplicated(self) -> None:
        finding = _finding()
        theirs = _open_issue(
            "https://github.com/o/r/issues/9", fingerprint_marker(finding.fingerprint), author="a.colleague"
        )
        host = _FakeHost(existing=[theirs])
        filed = file_class_c_issue(host, finding=finding, enforcement="Add a gate.", context=_CONTEXT)
        assert filed.withheld
        assert host.created == []
        assert host.updated == []

    def test_auto_filed_issue_carries_needs_triage(self) -> None:
        host = _FakeHost()
        file_class_c_issue(host, finding=_finding(), enforcement="Add a gate.", context=_CONTEXT)
        assert host.created[0]["labels"] == ["enforcement-gap", "needs-triage"]

    def test_user_directed_issue_omits_needs_triage(self) -> None:
        host = _FakeHost()
        context = FilingContext(repo="o/r", pr_url="https://github.com/o/r/pull/1", auto_filed=False)
        file_class_c_issue(host, finding=_finding(), enforcement="Add a gate.", context=context)
        assert host.created[0]["labels"] == ["enforcement-gap"]


@pytest.mark.usefixtures("allow_review_filing_repo", "configured_banned_term_registry")
class TestProcessReviewFindings(TestCase):
    def setUp(self) -> None:
        temporary_dir = TemporaryDirectory()
        self.addCleanup(temporary_dir.cleanup)
        self.tmp_path = Path(temporary_dir.name)

    def test_files_only_class_c_and_counts(self) -> None:
        host = _FakeHost()
        store = FindingsStore(data_dir=self.tmp_path)
        a = ClassifiedFinding(_finding(body="already enforced", line=1), FindingClass.A)
        b = ClassifiedFinding(_finding(body="one off thing", line=2), FindingClass.B)
        c = ClassifiedFinding(_finding(body="recurring gap", line=3), FindingClass.C)
        summary = process_review_findings(
            host,
            classified=[a, b, c],
            enforcement={c.finding.fingerprint: "Add a hook."},
            store=store,
            context=_CONTEXT,
        )
        assert summary.counts == {"A": 1, "B": 1, "C": 1}
        assert len(summary.filed) == 1
        assert len(host.created) == 1
        assert host.created[0]["labels"] == ["enforcement-gap", "needs-triage"]

    def test_rerun_does_not_refile(self) -> None:
        finding = _finding(body="recurring gap")
        first = _FakeHost()
        store = FindingsStore(data_dir=self.tmp_path)
        process_review_findings(
            first,
            classified=[ClassifiedFinding(finding, FindingClass.C)],
            enforcement={finding.fingerprint: "Add a hook."},
            store=store,
            context=_CONTEXT,
        )
        filed_body = str(first.created[0]["body"])
        # Second run: the host now reports the already-filed issue in the open backlog.
        second = _FakeHost(existing=[_open_issue("https://github.com/o/r/issues/100", filed_body)])
        summary = process_review_findings(
            second,
            classified=[ClassifiedFinding(finding, FindingClass.C)],
            enforcement={finding.fingerprint: "Add a hook."},
            store=store,
            context=_CONTEXT,
        )
        assert second.created == []
        assert summary.filed[0].already_filed


@pytest.fixture
def banned_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A DB-home registry banning a sample tenant name."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps({"leak": ["acmecorp"], "prose_collider": ["acmecorp"]}),),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return db


class TestNeutralizeBareReferences:
    def test_defangs_issue_mr_ts_and_url(self) -> None:
        text = "See #1234 and !99, ts 1716900000.123456, https://github.com/o/r/issues/5"
        out = neutralize_bare_references(text)
        assert find_bare_references(out) == []
        assert "`issue 1234`" in out
        assert "`MR 99`" in out
        assert "<https://github.com/o/r/issues/5>" in out

    def test_leaves_plain_prose_untouched(self) -> None:
        text = "Prefer composition over this mixin pattern."
        assert neutralize_bare_references(text) == text


@pytest.mark.usefixtures("allow_review_filing_repo")
class TestLeakClosure(TestCase):
    """The untrusted finding body must never leak bare refs or banned terms."""

    @pytest.mark.usefixtures("configured_banned_term_registry")
    def test_filed_body_has_no_bare_references(self) -> None:
        finding = _finding(body="Same as #1234 / !99 / ts 1716900000.123456 — see the thread")
        host = _FakeHost()
        filed = file_class_c_issue(host, finding=finding, enforcement="Add a gate.", context=_CONTEXT)

        assert not filed.withheld
        assert len(host.created) == 1
        # Assert on the ACTUAL payload sent to create_issue, not the scaffold.
        sent_body = host.created[0]["body"]
        sent_title = host.created[0]["title"]
        assert find_bare_references(str(sent_body)) == []
        assert find_bare_references(str(sent_title)) == []

    @pytest.mark.usefixtures("banned_config")
    def test_withholds_finding_with_banned_term(self) -> None:
        finding = _finding(body="This breaks the acmecorp tenant flow")
        host = _FakeHost()
        filed = file_class_c_issue(host, finding=finding, enforcement="Add a gate.", context=_CONTEXT)

        # Withheld — nothing leaks, no issue filed.
        assert filed.withheld
        assert "acmecorp" in filed.withheld_reason
        assert filed.url == ""
        assert host.created == []

    @pytest.mark.usefixtures("banned_config")
    def test_clean_finding_files_and_payload_is_banned_term_clean(self) -> None:
        finding = _finding(body="Prefer composition over this mixin pattern")
        host = _FakeHost()
        filed = file_class_c_issue(host, finding=finding, enforcement="Add a gate.", context=_CONTEXT)

        assert not filed.withheld
        assert len(host.created) == 1
        # The actual filed body trips no banned-terms gate.
        assert banned_terms_scanner.scan_text(str(host.created[0]["body"])) is None


class TestFilingRefusesWhateverTheStoreCannotVouchFor:
    """Filing an issue IS a publishing path, so its no-term-list disposition is the gate's.

    Both an absent registry and an unreadable store refuse the publish.
    """

    @pytest.fixture(autouse=True)
    def _no_ambient_terms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)

    def _file(self) -> tuple[_FakeHost, object]:
        host = _FakeHost()
        filed = file_class_c_issue(
            host, finding=_finding(body="Prefer composition here"), enforcement="Add a gate.", context=_CONTEXT
        )
        return host, filed

    def test_an_absent_store_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        host, filed = self._file()
        assert filed.withheld
        assert banned_terms_scanner.TERMS_UNSET_MARKER in filed.withheld_reason
        assert host.created == []

    def test_an_errored_store_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        corrupt = tmp_path / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a sqlite database")
        monkeypatch.setenv("T3_CONFIG_DB", str(corrupt))
        host, filed = self._file()
        assert filed.withheld
        assert banned_terms_scanner.STORE_UNREADABLE_MARKER in filed.withheld_reason
        assert host.created == []
