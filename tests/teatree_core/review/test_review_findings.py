"""Unit tests for the deterministic ``retro review-findings`` scaffold (#1573).

Pure logic: fingerprint stability, the durable store, the issue-body builder
(clickable-link safe + fingerprint marker), and the dedup-aware filer. The
forge host is a stand-in object recording the calls it received.
"""

import json
import sqlite3
from pathlib import Path

import pytest

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
    find_existing_issue,
    neutralize_bare_references,
    parse_findings,
    process_review_findings,
)
from teatree.hooks import banned_terms_scanner

_CONTEXT = FilingContext(repo="o/r", pr_url="https://github.com/o/r/pull/1")


class _FakeHost:
    """Minimal CodeHostBackend stand-in recording create/search calls."""

    def __init__(self, *, existing: list[dict[str, object]] | None = None) -> None:
        self.created: list[dict[str, object]] = []
        self._existing = existing or []

    def search_open_issues(self, *, repo: str, query: str) -> list[dict[str, object]]:
        self.last_search = (repo, query)
        return self._existing

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
        return {"html_url": f"https://github.com/{repo}/issues/{number}", "number": number}


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


class TestFiler:
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
        existing = [
            {
                "html_url": "https://github.com/o/r/issues/9",
                "body": f"<!-- retro-finding-fingerprint: {finding.fingerprint} -->",
            }
        ]
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

    def test_find_existing_ignores_non_matching_marker(self) -> None:
        host = _FakeHost(existing=[{"html_url": "https://x/9", "body": "unrelated"}])
        assert find_existing_issue(host, repo="o/r", fingerprint="deadbeef") == ""

    def test_auto_filed_issue_carries_needs_triage(self) -> None:
        host = _FakeHost()
        file_class_c_issue(host, finding=_finding(), enforcement="Add a gate.", context=_CONTEXT)
        assert host.created[0]["labels"] == ["enforcement-gap", "needs-triage"]

    def test_user_directed_issue_omits_needs_triage(self) -> None:
        host = _FakeHost()
        context = FilingContext(repo="o/r", pr_url="https://github.com/o/r/pull/1", auto_filed=False)
        file_class_c_issue(host, finding=_finding(), enforcement="Add a gate.", context=context)
        assert host.created[0]["labels"] == ["enforcement-gap"]


class TestProcessReviewFindings:
    def test_files_only_class_c_and_counts(self, tmp_path: Path) -> None:
        host = _FakeHost()
        store = FindingsStore(data_dir=tmp_path)
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

    def test_rerun_does_not_refile(self, tmp_path: Path) -> None:
        finding = _finding(body="recurring gap")
        first = _FakeHost()
        store = FindingsStore(data_dir=tmp_path)
        process_review_findings(
            first,
            classified=[ClassifiedFinding(finding, FindingClass.C)],
            enforcement={finding.fingerprint: "Add a hook."},
            store=store,
            context=_CONTEXT,
        )
        filed_body = first.created[0]["body"]
        # Second run: the host now reports the already-filed issue via search.
        second = _FakeHost(existing=[{"html_url": "https://github.com/o/r/issues/100", "body": filed_body}])
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
    """A DB-home config banning a sample tenant name (legacy file tier removed)."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
            (json.dumps(["acmecorp"]),),
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


class TestLeakClosure:
    """The untrusted finding body must never leak bare refs or banned terms."""

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
