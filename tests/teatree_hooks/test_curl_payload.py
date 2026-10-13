"""Unit tests for curl data / multipart-form payload extraction.

Direct coverage of the ``curl`` body-fragment walker the leak gate relies on:
``-d``/``--data``/``--json`` values (JSON-aware), ``-F``/``--form`` field values,
and the file-reference forms (``@file`` / ``<file``) that must fail closed
because the gate cannot read them at PreToolUse scan time. Synthetic term
``acmecorp`` only.
"""

import re
import shutil
import subprocess

from scripts.hooks.generate_curl_option_table import parse_aliases
from teatree.hooks._curl_payload import (
    CURL_LONG_KINDS,
    CURL_SHORT_KINDS,
    _json_body_fields,
    _walk_curl_args,
    curl_groups,
    curl_method,
    curl_options,
)
from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL


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


class TestCurlOptionGrammar:
    def test_known_value_options_consume_dash_led_values(self) -> None:
        options = list(curl_options(["--header", "-H", "-d", "body=acmecorp"]))
        assert [(option.name, option.value) for option in options] == [("--header", "-H"), ("-d", "body=acmecorp")]

    def test_attached_value_does_not_consume_the_next_option(self) -> None:
        options = list(curl_options(["-d'a'", "-d", "body=acmecorp"]))
        assert [(option.name, option.value) for option in options] == [("-d", "'a'"), ("-d", "body=acmecorp")]

    def test_boolean_negation_and_expand_value_are_known(self) -> None:
        options = list(curl_options(["--no-location", "--expand-data=body=acmecorp", "--no-next"]))
        assert [(option.name, option.unknown) for option in options] == [
            ("--location", False),
            ("--data", False),
            ("--next", True),
        ]
        assert options[1].value == "body=acmecorp"

    def test_unknown_abbreviation_is_flagged(self) -> None:
        assert next(curl_options(["--data-asc", "body=acmecorp"])).unknown is True

    def test_methods_are_per_next_group_and_last_request_wins(self) -> None:
        groups = curl_groups(["-X", "POST", "-d", "x", "https://example.com", "--next", "-X", "GET"])
        assert [curl_method(options) for options, _ in groups] == ["POST", "GET"]
        assert curl_method(list(curl_options(["-X", "POST", "-X", "GET"]))) == "GET"

    def test_installed_curl_help_options_are_in_the_pinned_table(self) -> None:
        curl_binary = shutil.which("curl")
        assert curl_binary is not None
        help_text = subprocess.run(
            [curl_binary, "--help", "all"], capture_output=True, text=True, check=True, timeout=5
        ).stdout
        for short, long in re.findall(r"(?m)^\s*(?:-(.),\s*)?--([a-z0-9.-]+)\b", help_text):
            name = long.removeprefix("no-").removeprefix("expand-")
            assert f"--{name}" in CURL_LONG_KINDS
            if short:
                assert f"-{short}" in CURL_SHORT_KINDS

    def test_both_upstream_alias_formats_preserve_short_letter_rules(self) -> None:
        old = '{"da", "data-ascii", ARG_STRING},\n{":", "next", ARG_NONE},'
        new = "{\"header\", ARG_STRG, 'H', C_HEADER},\n{\"next\", ARG_NONE, ':', C_NEXT},"
        assert parse_aliases(old, "curl-8_5_0") == {"data-ascii": ("value", ""), "next": ("switch", ":")}
        assert parse_aliases(new, "curl-8_18_0") == {"header": ("value", "H"), "next": ("switch", ":")}
