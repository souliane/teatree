"""Structured EMIT records + banned-terms scan (#2763) — pure-logic units."""

from teatree.core.cleanup.cleanup_emit import (
    EMIT_SCHEMA_VERSION,
    CleanupEmitRecord,
    banned_terms_status,
    scan_banned_terms,
)


class TestScanBannedTerms:
    def test_high_signal_terms_are_detected(self) -> None:
        found = scan_banned_terms("fix(scope): remove the leaked credential and the password")
        assert "credential" in found
        assert "password" in found
        assert any("leak" in token for token in found)

    def test_local_path_is_detected(self) -> None:
        assert any("/users/" in token for token in scan_banned_terms("see /Users/alice/secret.txt"))

    def test_common_words_do_not_false_positive(self) -> None:
        assert scan_banned_terms("send the user an email about the key feature") == []


class TestBannedTermsStatus:
    def test_empty_inputs_are_unknown(self) -> None:
        assert banned_terms_status([]) == ("unknown", [])
        assert banned_terms_status(["", "  "]) == ("unknown", [])

    def test_clean_text_is_clean(self) -> None:
        assert banned_terms_status(["feat: add a dark mode toggle"]) == ("clean", [])

    def test_banned_text_is_contains_with_terms(self) -> None:
        status, found = banned_terms_status(["chore: rotate the secret token"])
        assert status == "contains"
        assert "secret" in found


class TestCleanupEmitRecordSchema:
    def test_to_dict_carries_schema_version_and_all_fields(self) -> None:
        record = CleanupEmitRecord(
            path="/ws/feat-x",
            branch="feat-x",
            kind="worktree",
            unique_commit_shas=["abc123"],
            merged_with_post_merge_work=True,
            banned_terms_status="contains",
            banned_terms_found=["secret"],
            liveness="",
            last_commit_date="2026-06-27T10:00:00+00:00",
            owner="souliane",
        )
        data = record.to_dict()
        assert data["schema_version"] == EMIT_SCHEMA_VERSION
        assert data == {
            "schema_version": EMIT_SCHEMA_VERSION,
            "path": "/ws/feat-x",
            "branch": "feat-x",
            "kind": "worktree",
            "unique_commit_shas": ["abc123"],
            "merged_with_post_merge_work": True,
            "banned_terms_status": "contains",
            "banned_terms_found": ["secret"],
            "liveness": "",
            "last_commit_date": "2026-06-27T10:00:00+00:00",
            "owner": "souliane",
        }
