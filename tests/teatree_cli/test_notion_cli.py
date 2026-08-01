"""``t3 notion`` — the surface agents call, including its failure exit codes."""

import json
from pathlib import Path

import httpx
import pytest
import typer.testing

from teatree.cli.notion import notion_app
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

CANONICAL = "🔧 /prd-agent — engineering delivery notes"
MARKER = "[t3:bdd-test-creation]"


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    return install_fake_notion(monkeypatch)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> typer.testing.CliRunner:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    return typer.testing.CliRunner()


class TestReads:
    def test_whoami_prints_the_integration_pages_must_be_shared_with(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        result = runner.invoke(notion_app, ["whoami"])

        assert result.exit_code == 0, result.output
        assert "Factory" in result.output
        assert "bot-1" in result.output

    def test_fetch_renders_the_page_as_markdown(self, runner: typer.testing.CliRunner, notion: FakeNotion) -> None:
        notion.heading("Requirements")
        notion.paragraph("The loan must price.")

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 0, result.output
        assert "## Requirements" in result.output
        assert "The loan must price." in result.output

    def test_fetch_with_comments_includes_the_open_discussions(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph("body")
        notion.comments.append(
            {
                "discussion_id": "disc-1",
                "created_by": {"id": "user-7"},
                "created_time": "2026-07-01T00:00:00Z",
                "rich_text": [{"plain_text": "is this still true?"}],
            }
        )

        result = runner.invoke(notion_app, ["fetch", notion.page_id, "--comments"])

        assert result.exit_code == 0, result.output
        assert "is this still true?" in result.output
        assert "disc-1" in result.output

    def test_fetch_writes_to_a_file_when_asked(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.paragraph("PRD body")
        out = tmp_path / "nested" / "prd.md"

        result = runner.invoke(notion_app, ["fetch", notion.page_id, "--out", str(out)])

        assert result.exit_code == 0, result.output
        assert "PRD body" in out.read_text(encoding="utf-8")

    def test_query_emits_the_rows_as_json(self, runner: typer.testing.CliRunner, notion: FakeNotion) -> None:
        result = runner.invoke(notion_app, ["query", notion.page_id])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [{"id": "row-1"}]

    def test_query_can_target_a_data_source(self, runner: typer.testing.CliRunner, notion: FakeNotion) -> None:
        result = runner.invoke(notion_app, ["query", notion.page_id, "--data-source"])

        assert result.exit_code == 0, result.output
        assert ("POST", f"/data_sources/{notion.page_id}/query") in notion.requests


class TestSectionSurface:
    def test_show_reports_which_blocks_the_section_owns(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        heading = notion.heading(CANONICAL, toggle=True)
        body = notion.paragraph("old notes", parent=heading)

        result = runner.invoke(notion_app, ["section", "show", notion.page_id, "--heading", CANONICAL])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "present"
        assert payload["body_block_ids"] == [body]

    def test_replace_rewrites_only_the_owned_section(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        prd = notion.paragraph("High level description")
        heading = notion.heading(CANONICAL, toggle=True)
        notion.paragraph("stale", parent=heading)
        body_file = tmp_path / "body.md"
        body_file.write_text("### Delivered in\n\n- MR !1\n", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["section", "replace", notion.page_id, "--heading", CANONICAL, "--body-file", str(body_file)],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["outcome"] == "replaced"
        assert prd in notion.children[notion.page_id]
        assert "Delivered in" in " ".join(notion.body_texts(heading))

    def test_replace_offers_no_raw_blocks_escape_so_the_section_contract_holds(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        blocks_file = tmp_path / "blocks.json"
        blocks_file.write_text("[]", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["section", "replace", notion.page_id, "--heading", CANONICAL, "--blocks-file", str(blocks_file)],
        )

        assert result.exit_code != 0, "the section body must go through the block builder"

    def test_replace_creates_the_section_as_a_toggle_when_absent(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.paragraph("High level description")
        body_file = tmp_path / "body.md"
        body_file.write_text("### Delivered in\n\n- MR !1\n", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["section", "replace", notion.page_id, "--heading", CANONICAL, "--body-file", str(body_file)],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["outcome"] == "created"

    def test_there_is_no_whole_page_replace_command(self) -> None:
        names = {command.name for command in notion_app.registered_commands}
        assert "replace-content" not in names
        assert not any("replace" in str(name) for name in names), (
            "the only replace on this surface is the block-scoped `section replace`"
        )


class TestAppend:
    def test_append_adds_at_the_end_and_verifies_by_re_fetch(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.paragraph("existing body")
        body_file = tmp_path / "body.md"
        body_file.write_text("## Addendum\n\nsomething new\n", encoding="utf-8")

        result = runner.invoke(notion_app, ["append", notion.page_id, "--body-file", str(body_file)])

        assert result.exit_code == 0, result.output
        assert "verified by re-fetch" in result.output
        assert notion.body_texts(notion.page_id)[-1] == "something new"

    def test_an_append_that_does_not_land_exits_with_the_write_not_landed_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.suppress_appends = True
        body_file = tmp_path / "body.md"
        body_file.write_text("something new\n", encoding="utf-8")

        result = runner.invoke(notion_app, ["append", notion.page_id, "--body-file", str(body_file)])

        assert result.exit_code == 9
        assert "treat the write as failed" in result.output


class TestCommentPost:
    def test_posting_lands_the_comment_and_reports_its_discussion(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        body_file = tmp_path / "note.md"
        body_file.write_text(f"{MARKER} 7 scenarios regenerated\n", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["comment", "post", notion.page_id, "--body-file", str(body_file), "--marker", MARKER],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "posted"
        assert payload["comment_id"] == notion.comments[-1]["id"]

    def test_the_same_marker_reports_duplicate_and_writes_nothing(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.comments.append(
            {"id": "comment-seed", "discussion_id": "disc-seed", "rich_text": [{"plain_text": f"{MARKER} earlier run"}]}
        )
        body_file = tmp_path / "note.md"
        body_file.write_text(f"{MARKER} 7 scenarios regenerated\n", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["comment", "post", notion.page_id, "--body-file", str(body_file), "--marker", MARKER],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["outcome"] == "duplicate"
        assert len(notion.comments) == 1

    def test_a_comment_that_does_not_land_exits_with_the_write_not_landed_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.suppress_comments = True
        body_file = tmp_path / "note.md"
        body_file.write_text(f"{MARKER} 7 scenarios regenerated\n", encoding="utf-8")

        result = runner.invoke(notion_app, ["comment", "post", notion.page_id, "--body-file", str(body_file)])

        assert result.exit_code == 9
        assert "treat the write as failed" in result.output

    def test_a_missing_insert_comment_capability_exits_as_capability_denied(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.fail_with = (403, "restricted_resource")
        body_file = tmp_path / "note.md"
        body_file.write_text(f"{MARKER} 7 scenarios regenerated\n", encoding="utf-8")

        result = runner.invoke(notion_app, ["comment", "post", notion.page_id, "--body-file", str(body_file)])

        assert result.exit_code == 5
        assert "lacks the capability" in result.output


class TestPropertySurface:
    def test_get_prints_the_plain_value_a_poller_can_branch_on(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.set_property("GitLab Reference", {"type": "rich_text", "rich_text": [{"plain_text": "BUG-23"}]})

        result = runner.invoke(notion_app, ["property", "get", notion.page_id, "--name", "GitLab Reference"])

        assert result.exit_code == 0, result.output
        assert result.output.strip() == "BUG-23"

    def test_get_emits_the_raw_property_object_when_asked(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.set_property("Status", {"type": "status", "status": {"name": "In review"}})

        result = runner.invoke(notion_app, ["property", "get", notion.page_id, "--name", "Status", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == {"name": "In review"}

    def test_a_property_the_page_does_not_have_exits_with_its_own_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.set_property("Status", {"type": "status", "status": None})

        result = runner.invoke(notion_app, ["property", "get", notion.page_id, "--name", "GitLab Reference"])

        assert result.exit_code == 12
        assert "no property named 'GitLab Reference'" in result.output

    def test_set_writes_through_the_properties_own_type_and_verifies(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.set_property("Status", {"type": "status", "status": {"name": "In review"}})

        result = runner.invoke(notion_app, ["property", "set", notion.page_id, "--name", "Status", "--value", "Merged"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == {
            "outcome": "set",
            "name": "Status",
            "type": "status",
            "previous": "In review",
            "value": "Merged",
        }
        assert notion.properties["Status"]["status"] == {"name": "Merged"}

    def test_a_property_write_that_does_not_land_exits_with_the_write_not_landed_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.set_property("Status", {"type": "status", "status": {"name": "In review"}})
        notion.suppress_property_writes = True

        result = runner.invoke(notion_app, ["property", "set", notion.page_id, "--name", "Status", "--value", "Merged"])

        assert result.exit_code == 9
        assert "treat the write as failed" in result.output

    def test_a_type_with_no_plain_text_write_exits_with_its_own_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.set_property("Owner", {"type": "people", "people": []})

        result = runner.invoke(notion_app, ["property", "set", notion.page_id, "--name", "Owner", "--value", "adrien"])

        assert result.exit_code == 13
        assert "people" in result.output

    def test_a_missing_update_content_capability_exits_as_capability_denied(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.fail_with = (403, "restricted_resource")

        result = runner.invoke(notion_app, ["property", "set", notion.page_id, "--name", "Status", "--value", "Merged"])

        assert result.exit_code == 5
        assert "lacks the capability" in result.output


class TestFailureExitCodes:
    @pytest.mark.parametrize(
        "condition",
        [
            (401, "unauthorized", 4, "rejected the integration token"),
            (403, "restricted_resource", 5, "lacks the capability"),
            (404, "object_not_found", 6, "not shared with this integration"),
            (400, "validation_error", 7, "does not recognise"),
        ],
        ids=["bad-token", "capability-denied", "not-shared", "not-an-object"],
    )
    def test_each_condition_exits_with_its_own_code(
        self,
        runner: typer.testing.CliRunner,
        notion: FakeNotion,
        condition: tuple[int, str, int, str],
    ) -> None:
        status, code, expected_exit, expected_text = condition
        notion.fail_with = (status, code)
        if status == httpx.codes.UNAUTHORIZED:
            notion.identity_fail_with = (status, code)

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == expected_exit
        assert expected_text in result.output

    def test_a_missing_token_exits_distinctly_and_names_the_setup(
        self, monkeypatch: pytest.MonkeyPatch, notion: FakeNotion
    ) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _: "")

        result = typer.testing.CliRunner().invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 3
        assert "pass insert notion/integration-token" in result.output

    def test_a_reference_that_is_not_a_notion_id_exits_as_not_found(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        result = runner.invoke(notion_app, ["fetch", "https://example.test/some-doc"])

        assert result.exit_code == 7
        assert "carries no Notion object id" in result.output


class TestArchivedPages:
    """The refusal that matters most: a dead page renders as a completely current one."""

    DEAD_SPEC = "AC-8: when the flag is off, the tooltip is absent."

    def test_fetching_an_archived_page_refuses_instead_of_returning_its_body(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.heading("Acceptance criteria")
        notion.paragraph(self.DEAD_SPEC)
        notion.page_archived = True

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert self.DEAD_SPEC not in result.output, "the body of a dead page must never reach the caller"
        assert "archived" in result.output

    def test_the_refusal_names_the_live_page_carrying_the_same_title(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.page_archived = True
        notion.rows = [{"id": "22222222-2222-2222-2222-222222222222", "url": "https://www.notion.so/2852"}]

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert "https://www.notion.so/2852" in result.output

    def test_an_unresolvable_current_version_is_said_plainly_never_guessed(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.make_database_row(database_id="db-backlog")
        notion.page_archived = True
        notion.rows = []

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert "could NOT be resolved" in result.output

    def test_a_row_its_own_database_no_longer_returns_is_refused_as_unknown(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph(self.DEAD_SPEC)
        notion.make_database_row(database_id="db-backlog")
        notion.rows = [{"id": "22222222-2222-2222-2222-222222222222", "url": "https://www.notion.so/2852"}]

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert self.DEAD_SPEC not in result.output
        assert "unknown" in result.output

    def test_a_parent_database_this_integration_cannot_read_is_unknown_not_fine(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph(self.DEAD_SPEC)
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (404, "object_not_found")

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert self.DEAD_SPEC not in result.output
        assert "could not be queried" in result.output

    def test_a_live_row_of_a_reachable_database_still_reads(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph("the current requirement")
        notion.make_database_row(database_id="db-backlog")

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 0, result.output
        assert "the current requirement" in result.output

    @pytest.mark.parametrize("command", ["fetch", "append", "section", "comment", "property"])
    def test_no_page_scoped_command_carries_an_audit_flag(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, command: str
    ) -> None:
        _ = notion
        result = runner.invoke(notion_app, [command, "--help"])

        assert "--archived-audit" not in result.output, (
            "the audit escape is its own command, never a flag that can be appended by habit"
        )

    def test_a_write_to_a_dead_page_is_refused_and_no_audited_write_exists(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.page_archived = True
        body_file = tmp_path / "body.md"
        body_file.write_text("## Addendum\n\nsomething new\n", encoding="utf-8")

        result = runner.invoke(notion_app, ["append", notion.page_id, "--body-file", str(body_file)])

        assert result.exit_code == 14, result.output
        assert notion.body_texts(notion.page_id) == []
        assert "audit-append" not in {command.name for command in notion_app.registered_commands}

    def test_the_audit_read_needs_a_written_reason(self, runner: typer.testing.CliRunner, notion: FakeNotion) -> None:
        notion.paragraph(self.DEAD_SPEC)
        notion.page_archived = True

        assert runner.invoke(notion_app, ["audit-fetch", notion.page_id]).exit_code != 0, "--reason is mandatory"

        blank = runner.invoke(notion_app, ["audit-fetch", notion.page_id, "--reason", "   "])

        assert blank.exit_code == 1, blank.output
        assert self.DEAD_SPEC not in blank.output

    def test_an_audit_read_stamps_the_document_it_hands_back(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.paragraph(self.DEAD_SPEC)
        notion.page_archived = True
        out = tmp_path / "audit.md"

        result = runner.invoke(
            notion_app,
            ["audit-fetch", notion.page_id, "--reason", "postmortem of WI-77", "--out", str(out)],
        )

        assert result.exit_code == 0, result.output
        written = out.read_text(encoding="utf-8")
        assert "ARCHIVED-PAGE AUDIT READ" in written, "an audited body must carry its own provenance"
        assert "postmortem of WI-77" in written
        assert self.DEAD_SPEC in written

    def test_an_audit_read_of_a_live_page_carries_no_stamp(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph("the current requirement")

        result = runner.invoke(notion_app, ["audit-fetch", notion.page_id, "--reason", "checking provenance"])

        assert result.exit_code == 0, result.output
        assert "ARCHIVED-PAGE AUDIT READ" not in result.output
        assert "the current requirement" in result.output


class TestDoctor:
    def test_doctor_separates_the_token_verdict_from_the_sharing_verdict(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.fail_with = (404, "object_not_found")

        result = runner.invoke(notion_app, ["doctor", notion.page_id])

        assert result.exit_code == 6
        assert "token: OK" in result.output
        assert "page:  FAIL" in result.output

    def test_a_bad_token_fails_the_token_line_not_the_page_line(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.fail_with = (401, "unauthorized")
        notion.identity_fail_with = (401, "unauthorized")

        result = runner.invoke(notion_app, ["doctor", notion.page_id])

        assert result.exit_code == 4
        assert "token: FAIL" in result.output
        assert "page:" not in result.output, "a credential failure must not be reported as a sharing failure"

    def test_doctor_is_green_when_the_page_is_reachable(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        result = runner.invoke(notion_app, ["doctor", notion.page_id])

        assert result.exit_code == 0, result.output
        assert "page:  OK" in result.output
