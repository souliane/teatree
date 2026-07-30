"""Unit tests for curl data / multipart-form payload extraction.

Direct coverage of the ``curl`` body-fragment walker the leak gate relies on:
``-d``/``--data``/``--json`` values (JSON-aware), ``-F``/``--form`` field values,
and the file-reference forms (``@file`` / ``<file``) that must fail closed
because the gate cannot read them at PreToolUse scan time. Synthetic term
``acmecorp`` only.
"""

from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL
from teatree.hooks._curl_payload import _json_body_fields, _walk_curl_args


class TestJsonBodyFields:
    """The shared JSON field extractor keeps ``text``/``message``/``body`` values."""

    def test_extracts_known_body_keys(self) -> None:
        assert _json_body_fields('{"body": "acmecorp", "other": 1}') == ["acmecorp"]

    def test_non_dict_and_invalid_json_yield_nothing(self) -> None:
        assert _json_body_fields("[1, 2, 3]") == []
        assert _json_body_fields("not json") == []


class TestWalkCurlArgs:
    """``-d``/``--data``/``--json`` and ``-F``/``--form`` value extraction."""

    def test_space_separated_data_value_is_scanned(self) -> None:
        payloads: list[str] = []
        _walk_curl_args(["curl", "-d", "leak acmecorp", "https://example"], payloads)
        assert "leak acmecorp" in payloads

    def test_json_data_value_expands_body_field(self) -> None:
        payloads: list[str] = []
        _walk_curl_args(["curl", "--json", '{"body": "acmecorp"}'], payloads)
        assert "acmecorp" in payloads

    def test_at_file_data_reference_fails_closed(self) -> None:
        payloads: list[str] = []
        _walk_curl_args(["curl", "-d", "@/tmp/body.json"], payloads)
        assert payloads == [FAIL_CLOSED_SENTINEL]

    def test_form_field_value_is_scanned(self) -> None:
        payloads: list[str] = []
        _walk_curl_args(["curl", "-F", "text=acmecorp"], payloads)
        assert "acmecorp" in payloads

    def test_form_file_reference_fails_closed(self) -> None:
        payloads: list[str] = []
        _walk_curl_args(["curl", "--form", "file=@/tmp/x"], payloads)
        assert payloads == [FAIL_CLOSED_SENTINEL]
