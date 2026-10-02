"""``gh``/``glab api`` field assignments other than ``body=`` reach the leak payload.

The extractor recognised only ``-f body=``, so GitLab's own field name for an
issue/MR body — ``description`` — extracted an EMPTY payload and every
body-based leak gate scanned nothing while detection still called the command a
publish. That is a fail-OPEN on the gate that stops customer-identifying content
reaching a public repo.
"""

from pathlib import Path

import pytest

from teatree.hooks._command_parser import extract_bash_payload, extract_secret_scan_text
from teatree.hooks._parser_primitives import BODY_FIELD_NAMES, BODY_LONG_OPTION_FIELDS, is_fail_closed_sentinel

LEAK = "customer-identifying-text"
ISSUE = "projects/123/issues/7"

# Every spelling ``gh``/``glab`` accept for one field assignment: spaced short,
# spaced long, and the pflag-attached forms of both.
FIELD_SPELLINGS = ["-f {a}", "-F {a}", "--field {a}", "--raw-field {a}", "--field={a}", "-f{a}", "-F{a}"]


def _api_put(assignment: str) -> str:
    return f"glab api -X PUT {ISSUE} {assignment}"


def _api_write_for_field(name: str, assignment: str) -> str:
    if name == "name":
        return f"gh api -X PATCH repos/o/r/releases/1 {assignment}"
    return _api_put(assignment)


class TestEveryFieldSpellingReachesThePayload:
    """One hardcoded name left all seven spellings open at once — close them all."""

    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_description_field_is_extracted(self, spelling: str) -> None:
        assert LEAK in extract_bash_payload(_api_put(spelling.format(a=f"description={LEAK}")))

    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_body_field_is_still_extracted(self, spelling: str) -> None:
        assert LEAK in extract_bash_payload(_api_put(spelling.format(a=f"body={LEAK}")))


# A real publishing call per body-bearing field name. Parametrizing over the
# catalogue itself would pass however few names it holds; this table is written
# from the two forges' API docs, so a name missing from the catalogue goes RED.
PUBLISH_CALLS_BY_FIELD = {
    "body": "gh api -X POST repos/o/r/issues/1/comments -f body={leak}",
    "commit_message": "gh api -X PUT repos/o/r/pulls/2/merge -f commit_message={leak}",
    "commit_title": "gh api -X PUT repos/o/r/pulls/2/merge -f commit_title={leak}",
    "content": "glab api -X POST projects/1/repository/files/a.md -f content={leak}",
    "description": "glab api -X PUT projects/1/issues/7 -f description={leak}",
    "merge_commit_message": "glab api -X PUT projects/1/merge_requests/2/merge -f merge_commit_message={leak}",
    "message": "gh api -X POST repos/o/r/git/commits -f message={leak}",
    "name": "gh api -X POST repos/o/r/releases -f name={leak}",
    "note": "glab api -X POST projects/1/repository/commits/abc/comments -f note={leak}",
    "squash_commit_message": "glab api -X PUT projects/1/merge_requests/2/merge -f squash_commit_message={leak}",
    "tag_message": "glab api -X POST projects/1/releases -f tag_message={leak}",
    "title": "glab api -X POST projects/1/issues -f title={leak}",
}


class TestEveryBodyCarryingFieldName:
    @pytest.mark.parametrize(("name", "command"), sorted(PUBLISH_CALLS_BY_FIELD.items()))
    def test_real_publish_call_reaches_the_payload(self, name: str, command: str) -> None:
        assert LEAK in extract_bash_payload(command.format(leak=LEAK)), name

    def test_catalogue_covers_exactly_the_documented_calls(self) -> None:
        assert set(PUBLISH_CALLS_BY_FIELD) == BODY_FIELD_NAMES

    @pytest.mark.parametrize("name", sorted(BODY_LONG_OPTION_FIELDS))
    def test_long_flag_spelling_is_extracted(self, name: str) -> None:
        assert LEAK in extract_bash_payload(f"glab mr update 1 --{name} '{LEAK}'")


class TestTheGlobalFlagGrammarStaysNarrowerThanTheFieldCatalogue:
    """``_walk_body_flags`` runs on EVERY segment, so it may not inherit the widening.

    The field walker is scoped to a ``gh``/``glab`` leader, where every call is a
    forge API call. The long-option walker has no such scope: a name added there
    starts consuming the next token of whichever unrelated tool spells an option
    the same way. The two also disagree on spelling — the API's ``commit_message``
    is a CLI's ``--commit-message`` — so deriving one from the other is wrong even
    where it is safe.
    """

    def test_long_option_fields_are_body_fields(self) -> None:
        assert BODY_LONG_OPTION_FIELDS <= BODY_FIELD_NAMES

    def test_multi_word_api_fields_are_not_long_options(self) -> None:
        assert not {name for name in BODY_LONG_OPTION_FIELDS if "_" in name}

    @pytest.mark.parametrize("flag", ["--content", "--note", "--commit_message", "--tag_message"])
    def test_unrelated_tool_option_is_not_consumed_as_a_body(self, flag: str) -> None:
        assert extract_bash_payload(f"mytool run {flag} {LEAK}") == ""


class TestALiveVariableFieldFailsClosed:
    """Liveness is decided from the VALUE's source span, not the whole token's.

    Every liveness check in ``_inline_body_resolution`` is anchored on the span it
    is handed, and a field token's span opens with ``description=`` — so a live
    ``"$VAR"`` matched no anchor, read as inert literal text, and the gate scanned
    the unexpanded token while bash published the variable's real value. The flag
    spelling of the same body (``--description "$VAR"``) has always failed closed.
    """

    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_absent_variable_fails_closed(self, spelling: str) -> None:
        assignment = spelling.format(a='description="$T3_ABSENT_BODY_VAR"')
        assert is_fail_closed_sentinel(extract_bash_payload(_api_put(assignment)))

    def test_present_variable_value_is_scanned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_PRESENT_BODY_VAR", LEAK)
        assert LEAK in extract_bash_payload(_api_put('-f description="$T3_PRESENT_BODY_VAR"'))

    def test_single_quoted_variable_is_inert_prose_not_a_block(self) -> None:
        payload = extract_bash_payload(_api_put("-f 'description=$T3_ABSENT_BODY_VAR'"))
        assert "$T3_ABSENT_BODY_VAR" in payload
        assert not is_fail_closed_sentinel(payload)


# A ``$(...)`` an author merely MENTIONS inside a single-quoted field body. bash
# passes it verbatim, so the body is fully present and scannable.
INERT_BODY = "ran $(date) to check the clock"


class TestAnInertFieldBodyIsScannedNotBlocked:
    """The quote that decides liveness opens in the NAME half of the field's span.

    ``$VAR`` liveness is decided by ANCHORED patterns, which need the value's
    span alone; ``$(...)`` liveness is decided by a quote-state WALK, which needs
    the whole span — the opening ``'`` of ``'body=… $(date) …'`` sits before the
    name. One slice cannot serve both: cutting the span down to the value starts
    the walk unquoted, so every single-quoted field body reads as live and
    hard-blocks. This gate sits in front of every Bash call, so that is a
    lockout, and it lands on the long-standing ``body=`` path, not only on the
    field names added beside it.
    """

    @pytest.mark.parametrize("name", sorted(BODY_FIELD_NAMES))
    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_whole_token_single_quoted_body_is_scanned(self, spelling: str, name: str) -> None:
        payload = extract_bash_payload(
            _api_write_for_field(name, spelling.format(a=f"'{name}={INERT_BODY}'")), fail_closed_body_file=True
        )
        assert INERT_BODY in payload
        assert not is_fail_closed_sentinel(payload)

    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_value_only_single_quoted_body_is_scanned(self, spelling: str) -> None:
        payload = extract_bash_payload(
            _api_put(spelling.format(a=f"description='{INERT_BODY}'")), fail_closed_body_file=True
        )
        assert INERT_BODY in payload
        assert not is_fail_closed_sentinel(payload)

    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_double_quoted_substitution_still_fails_closed(self, spelling: str, tmp_path: Path) -> None:
        assignment = spelling.format(a=f'description="prefix $(cat {tmp_path / "gone.md"})"')
        assert is_fail_closed_sentinel(extract_bash_payload(_api_put(assignment)))

    @pytest.mark.parametrize("spelling", FIELD_SPELLINGS)
    def test_unquoted_substitution_still_fails_closed(self, spelling: str, tmp_path: Path) -> None:
        assignment = spelling.format(a=f"description=$(cat {tmp_path / 'gone.md'})")
        assert is_fail_closed_sentinel(extract_bash_payload(_api_put(assignment)))


class TestTypedFieldReadsItsAtFile:
    """``-F``/``--field`` give ``@<path>`` its documented file-read meaning."""

    @pytest.mark.parametrize("flag", ["-F", "--field"])
    def test_at_file_content_is_scanned(self, flag: str, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text(LEAK, encoding="utf-8")
        assert LEAK in extract_bash_payload(_api_put(f"{flag} description=@{body}"))

    def test_at_stdin_fails_closed(self) -> None:
        assert is_fail_closed_sentinel(extract_bash_payload(_api_put("-F description=@-")))

    def test_unreadable_at_file_fails_closed(self, tmp_path: Path) -> None:
        assert is_fail_closed_sentinel(extract_bash_payload(_api_put(f"-F description=@{tmp_path / 'gone.md'}")))


class TestRawFieldTreatsAtAsLiteralText:
    """``-f``/``--raw-field`` send the value verbatim, so a leading ``@`` is prose.

    Reading it as a path would fail closed on an ordinary comment opening with an
    ``@mention`` — an over-block on a command that publishes nothing unreadable.
    """

    @pytest.mark.parametrize("flag", ["-f", "--raw-field"])
    def test_at_mention_is_scanned_verbatim_not_blocked(self, flag: str) -> None:
        payload = extract_bash_payload(_api_put(f"{flag} body='@alice {LEAK}'"))
        assert LEAK in payload
        assert not is_fail_closed_sentinel(payload)


class TestNonBodyFieldsStayOutOfTheLeakPayload:
    """Structured values carry no prose; scanning them only widens the fail-closed surface.

    ``name`` is endpoint-sensitive: a release name is public prose, while most API
    uses are identifiers — a repo, branch, webhook, or CI variable. The release
    routes are tested separately; ordinary routes must not inherit that wider
    fail-closed surface.
    """

    @pytest.mark.parametrize(
        "assignment",
        [
            "state_event=close",
            "confidential=true",
            "assignee_id=42",
            "branch=main",
            "path=src/app.py",
            "visibility=private",
        ],
    )
    def test_structured_field_contributes_no_body(self, assignment: str) -> None:
        assert extract_bash_payload(_api_put(f"-f {assignment}")) == ""

    def test_structured_field_still_reaches_the_secret_scan(self) -> None:
        assert "glpat-DEADBEEF" in extract_secret_scan_text(_api_put("-f assignee_id=glpat-DEADBEEF"))

    def test_name_on_an_ordinary_api_route_contributes_no_body(self) -> None:
        assert extract_bash_payload("gh api -X POST repos/o/r -f name=$REPO_NAME") == ""


class TestOrdinaryCommandsAreUntouched:
    """The gate sits in front of every Bash call — a false positive is a lockout."""

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la /tmp",
            "git status --short",
            "grep --text description=secret src/app.py",
            "rg 'body=' vendor/teatree/src",
            "pytest tests/ -k description",
            "glab api projects/123/issues/7",
            "gh api repos/o/r/issues --method GET -f state=open",
            "docker run --name $CONTAINER --rm alpine",
            "jq --arg content \"$BLOB\" '.a = $content' in.json",
            "aws s3 cp --content-type text/plain f s3://b/k",
            "git log --format=%s --grep description",
            "helm upgrade app ./chart --set note=$RELEASE_NOTE",
        ],
    )
    def test_nothing_is_captured_and_nothing_fails_closed(self, command: str) -> None:
        assert extract_bash_payload(command, fail_closed_body_file=True) == ""
