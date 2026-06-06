"""Tests for ``teatree.core.account_fingerprint`` — pure Django-free readers (#1916)."""

import json
from pathlib import Path

from teatree.core.account_fingerprint import (
    AccountIdentity,
    current_account_fingerprint,
    current_account_identity,
    fingerprint_switched,
    load_recorded_fingerprint,
    record_fingerprint,
)


def _write_active_account(home: Path, account_uuid: str) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": account_uuid, "emailAddress": "u@e.com"}}),
        encoding="utf-8",
    )


class TestCurrentFingerprint:
    def test_reads_account_uuid(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        assert current_account_fingerprint(home=tmp_path) == "uuid-A"

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert current_account_fingerprint(home=tmp_path) == ""

    def test_malformed_file_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text("{bad", encoding="utf-8")
        assert current_account_fingerprint(home=tmp_path) == ""

    def test_no_oauth_block_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text(json.dumps({"userID": "x"}), encoding="utf-8")
        assert current_account_fingerprint(home=tmp_path) == ""


class TestCurrentAccountIdentity:
    def test_reads_uuid_and_email_in_one_parse(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        assert current_account_identity(home=tmp_path) == AccountIdentity(account_uuid="uuid-A", email="u@e.com")

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert current_account_identity(home=tmp_path) is None

    def test_no_uuid_is_none(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "u@e.com"}}), encoding="utf-8"
        )
        assert current_account_identity(home=tmp_path) is None

    def test_missing_email_defaults_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "uuid-A"}}), encoding="utf-8"
        )
        identity = current_account_identity(home=tmp_path)
        assert identity is not None
        assert identity.account_uuid == "uuid-A"
        assert identity.email == ""

    def test_fingerprint_delegates_to_identity(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        assert current_account_fingerprint(home=tmp_path) == "uuid-A"


class TestRecordedFingerprint:
    def test_roundtrip(self, tmp_path: Path) -> None:
        record_fingerprint("uuid-A", home=tmp_path)
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-A"

    def test_overwrite_is_idempotent_last_wins(self, tmp_path: Path) -> None:
        record_fingerprint("uuid-A", home=tmp_path)
        record_fingerprint("uuid-B", home=tmp_path)
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-B"

    def test_absent_record_is_empty(self, tmp_path: Path) -> None:
        assert load_recorded_fingerprint(home=tmp_path) == ""

    def test_malformed_record_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude" / "teatree-account-switch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert load_recorded_fingerprint(home=tmp_path) == ""

    def test_non_string_value_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude" / "teatree-account-switch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"accountUuid": 42}), encoding="utf-8")
        assert load_recorded_fingerprint(home=tmp_path) == ""


class TestFingerprintSwitched:
    def test_first_run_no_record_is_not_switch(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        assert fingerprint_switched(home=tmp_path) is False

    def test_same_account_is_not_switch(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        record_fingerprint("uuid-A", home=tmp_path)
        assert fingerprint_switched(home=tmp_path) is False

    def test_changed_account_is_switch(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)
        assert fingerprint_switched(home=tmp_path) is True

    def test_empty_active_fingerprint_is_not_switch(self, tmp_path: Path) -> None:
        record_fingerprint("uuid-A", home=tmp_path)
        assert fingerprint_switched(home=tmp_path) is False
