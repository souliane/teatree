"""Unit tests for curl data / multipart-form payload extraction.

Direct coverage of the ``curl`` body-fragment walker the leak gate relies on:
``-d``/``--data``/``--json`` values (JSON-aware), ``-F``/``--form`` field values,
and the file-reference forms (``@file`` / ``<file``) that must fail closed
because the gate cannot read them at PreToolUse scan time. Synthetic term
``acmecorp`` only.
"""

import hashlib
import io
import json
import re
import shutil
import subprocess

import pytest

from scripts.hooks import generate_curl_option_table as generator
from teatree.hooks._curl_payload import (
    CURL_LONG_KINDS,
    CURL_SHORT_KINDS,
    _json_body_fields,
    _walk_curl_args,
    curl_groups,
    curl_method,
    curl_options,
)
from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL, NAMED_FAIL_CLOSED_PREFIX, is_fail_closed_sentinel

_PINNED_CURL_RANGE = ((8, 5), (8, 18))


class TestJsonBodyFields:
    """The shared JSON field extractor keeps ``text``/``message``/``body`` values."""

    def test_extracts_known_body_keys(self) -> None:
        assert _json_body_fields('{"body": "acmecorp", "other": 1}') == ["acmecorp"]

    def test_non_dict_and_invalid_json_yield_nothing(self) -> None:
        assert _json_body_fields("[1, 2, 3]") == []
        assert _json_body_fields("not json") == []

    def test_a_decoded_field_never_carries_a_parser_reason(self) -> None:
        fields = _json_body_fields(json.dumps({"body": f"acmecorp\n{NAMED_FAIL_CLOSED_PREFIX}x"}))
        assert fields == [fields[0]]
        assert "acmecorp" in fields[0]
        assert is_fail_closed_sentinel(fields[0]) is False


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

    def test_double_dash_ends_option_parsing(self) -> None:
        assert list(curl_options(["-sS", "--", "--frobnicate", "-K", "/tmp/x"])) == list(curl_options(["-sS"]))

    def test_methods_are_per_next_group_and_last_request_wins(self) -> None:
        groups = curl_groups(["-X", "POST", "-d", "x", "https://example.com", "--next", "-X", "GET"])
        assert [curl_method(options) for options, _ in groups] == ["POST", "GET"]
        assert curl_method(list(curl_options(["-X", "POST", "-X", "GET"]))) == "GET"

    def test_installed_curl_help_options_are_in_the_pinned_table(self) -> None:
        curl_binary = shutil.which("curl")
        if curl_binary is None:
            pytest.skip("curl is not installed; cannot compare its options with the pinned table")
        version_text = subprocess.run(
            [curl_binary, "--version"], capture_output=True, text=True, check=True, timeout=5
        ).stdout
        version_match = re.search(r"curl (\d+)\.(\d+)\.\d+", version_text)
        if version_match is None:
            pytest.skip("installed curl version is unreadable; cannot compare its options with the pinned table")
        version = (int(version_match[1]), int(version_match[2]))
        if version < _PINNED_CURL_RANGE[0]:
            pytest.skip(f"{version_match[0]} is older than the pinned 8.5 table; its options are not compared")
        help_text = subprocess.run(
            [curl_binary, "--help", "all"], capture_output=True, text=True, check=True, timeout=5
        ).stdout
        newer_options: list[str] = []
        for short, long in re.findall(r"(?m)^\s*(?:-(.),\s*)?--([a-z0-9.-]+)\b", help_text):
            name = long.removeprefix("no-").removeprefix("expand-")
            if f"--{name}" not in CURL_LONG_KINDS:
                newer_options.append(f"--{name}")
            elif short and f"-{short}" not in CURL_SHORT_KINDS:
                newer_options.append(f"-{short}")
        if newer_options and version > _PINNED_CURL_RANGE[1]:
            pytest.skip(f"{version_match[0]} is newer than the pinned 8.18 table; options outside it: {newer_options}")
        assert newer_options == []

    def test_both_upstream_alias_formats_preserve_short_letter_rules(self) -> None:
        old = '{"da", "data-ascii", ARG_STRING},\n{":", "next", ARG_NONE},'
        new = "{\"header\", ARG_STRG, 'H', C_HEADER},\n{\"next\", ARG_NONE, ':', C_NEXT},"
        assert generator.parse_aliases(old, "curl-8_5_0") == {"data-ascii": ("value", ""), "next": ("switch", ":")}
        assert generator.parse_aliases(new, "curl-8_18_0") == {"header": ("value", "H"), "next": ("switch", ":")}

    @pytest.mark.parametrize(("new_kind", "expected"), [("ARG_FILE", "value"), ("ARG_NONE", "unknown")])
    def test_file_argness_survives_generator_merge(
        self, monkeypatch: pytest.MonkeyPatch, new_kind: str, expected: str
    ) -> None:
        sources = {
            "curl-8_5_0": b'aliases[] = {\n{"F", "form", ARG_FILE},\n{":", "next", ARG_NONE},\n};',
            "curl-8_18_0": (
                f"aliases[] = {{\n{{\"form\", {new_kind}, 'F', C_FORM}},\n{{\"next\", ARG_NONE, ':', C_NEXT}},\n}};"
            ).encode(),
        }
        monkeypatch.setattr(
            generator, "SOURCES", {tag: hashlib.sha256(source).hexdigest() for tag, source in sources.items()}
        )
        monkeypatch.setattr(
            generator.urllib.request,
            "urlopen",
            lambda url, timeout: io.BytesIO(sources[url.split("/")[-3]]),
        )
        assert [row for row in json.loads(generator.generate())["options"] if row[0] == "form"] == [
            ["form", expected, "F"]
        ]
