"""``t3 notion`` — the surface agents call, including its failure exit codes."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import typer.testing

from teatree.backends.types import Service
from teatree.cli.notion import notion_app
from teatree.cli.notion_replace import notion_replace
from tests.teatree_backends.notion._fake_notion import UNSEEN_OBJECT_FRAGMENTS, FakeNotion, install_fake_notion

CANONICAL = "🔧 /prd-agent — engineering delivery notes"
MARKER = "[t3:bdd-test-creation]"


_ELSEWHERE = "99999999-9999-9999-9999-999999999999"


def _routing(entry: str, *services: Service) -> SimpleNamespace:
    config = SimpleNamespace(required_third_party_services=frozenset(services), secret_pass_key=lambda _name: entry)
    return SimpleNamespace(config=config)


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

    def test_a_bare_whoami_runs_as_the_overlay_that_owns_notion_and_says_so(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = "notion/integration-token"
        overlays = {"t3-acme": _routing(entry, Service.NOTION), "t3-teatree": _routing(entry)}
        monkeypatch.delenv("NOTION_TOKEN")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.setattr("teatree.core.overlay_loader.get_all_overlays", lambda: overlays)
        monkeypatch.setattr(
            "teatree.backends.notion.credentials.overlay_notion_pass_key",
            lambda name: overlays[name].config.secret_pass_key("notion_token"),
        )
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda key: "ntn_bot" if key == entry else "")

        result = runner.invoke(notion_app, ["whoami"])

        assert result.exit_code == 0, result.output
        assert "[overlay t3-acme]" in result.output
        assert notion.bearer_tokens[-1] == "ntn_bot"

    def test_whoami_names_the_environment_token_rather_than_an_overlays_entry(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        result = runner.invoke(notion_app, ["whoami"])

        assert result.exit_code == 0, result.output
        assert "[token from $NOTION_TOKEN;" in result.output
        assert "[overlay " not in result.output

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

    def test_comments_finds_a_thread_anchored_on_a_block_not_on_the_page(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        paragraph = notion.paragraph("the lookup returns the customer number")
        notion.comment_on(
            paragraph, "this contradicts the line above", discussion_id="disc-inline", author="Adrien Cossa"
        )

        result = runner.invoke(notion_app, ["comments", notion.page_id])

        assert result.exit_code == 0, result.output
        assert "disc-inline" in result.output
        assert "this contradicts the line above" in result.output
        assert "COVERAGE: COMPLETE" in result.output

    def test_each_thread_names_the_text_of_the_block_it_is_attached_to(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        paragraph = notion.paragraph("the lookup returns the customer number")
        notion.comment_on(paragraph, "which number?", discussion_id="disc-anchor")

        result = runner.invoke(notion_app, ["comments", notion.page_id])

        assert result.exit_code == 0, result.output
        assert f"on paragraph {paragraph}: “the lookup returns the customer number”" in result.output

    def test_a_page_with_no_open_thread_states_what_was_scanned(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph("a requirement nobody commented on")

        result = runner.invoke(notion_app, ["fetch", notion.page_id, "--comments"])

        assert result.exit_code == 0, result.output
        assert "page-level + 1 block(s) scanned" in result.output

    def test_a_block_whose_comments_cannot_be_read_is_named_not_scanned(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comments_fail_for[paragraph] = (403, "restricted_resource")

        result = runner.invoke(notion_app, ["comments", notion.page_id])

        assert result.exit_code == 18, result.output
        assert f"{paragraph} not scanned" in result.output

    def test_comments_exits_18_when_part_of_the_page_could_not_be_read(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        paragraph = notion.paragraph("requirement")
        notion.comment_on(paragraph, "an open question", discussion_id="disc-seen")
        notion.comments_fail_for[notion.page_id] = (403, "restricted_resource")

        result = runner.invoke(notion_app, ["comments", notion.page_id])

        assert result.exit_code == 18, result.output
        assert "COVERAGE: INCOMPLETE" in result.output
        assert "disc-seen" in result.output

    def test_comments_verify_reports_a_thread_that_vanished_between_two_reads(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        paragraph = notion.paragraph("the disputed line")
        notion.vanishing_comment_ids.add(
            notion.comment_on(paragraph, "the comment the dispute rests on", discussion_id="disc-vanishes")
        )

        result = runner.invoke(notion_app, ["comments", notion.page_id, "--verify"])

        assert result.exit_code == 18, result.output
        assert "divergent_reread" in result.output
        assert "the comment the dispute rests on" in result.output

    def test_fetch_with_comments_walks_the_block_tree_too(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        toggle = notion.add({"type": "toggle", "toggle": {"rich_text": [], "children": []}})
        buried = notion.paragraph("nested requirement", parent=toggle)
        notion.comment_on(buried, "still open?", discussion_id="disc-buried")

        result = runner.invoke(notion_app, ["fetch", notion.page_id, "--comments"])

        assert result.exit_code == 0, result.output
        assert "disc-buried" in result.output

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
        assert json.loads(result.stdout) == [{"id": "row-1"}]

    def test_query_limit_stops_paging_at_the_limit(self, runner: typer.testing.CliRunner, notion: FakeNotion) -> None:
        notion.rows = [{"id": f"row-{number}"} for number in range(250)]

        result = runner.invoke(notion_app, ["query", notion.page_id, "--limit", "5"])

        assert result.exit_code == 0, result.output
        assert len(json.loads(result.stdout)) == 5
        assert [method for method, path in notion.requests if path.endswith("/query")] == ["POST"]

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
        payload = json.loads(result.stdout)
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
        assert json.loads(result.stdout)["outcome"] == "replaced"
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
        assert json.loads(result.stdout)["outcome"] == "created"

    def test_there_is_no_whole_page_replace_command(self) -> None:
        names = {command.name for command in notion_app.registered_commands}
        assert "replace-content" not in names
        assert {name for name in names if "replace" in str(name)} == {"replace"}, (
            "the only replaces on this surface are `section replace` and the anchored, one-block `replace`"
        )
        assert "inside one block" in (notion_replace.__doc__ or "")


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

    def test_a_raw_blocks_append_that_does_not_land_is_not_reported_as_verified(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        """``--blocks-file`` carries no Markdown, so there is no text probe to look for.

        The re-fetch check was skipped entirely and the command still printed
        "verified by re-fetch" — a write that never landed reported as verified.
        """
        notion.suppress_appends = True
        blocks_file = tmp_path / "blocks.json"
        blocks_file.write_text(
            json.dumps([{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]),
            encoding="utf-8",
        )

        result = runner.invoke(notion_app, ["append", notion.page_id, "--blocks-file", str(blocks_file)])

        assert result.exit_code == 9, result.output
        assert "treat the write as failed" in result.output

    def test_a_markdown_append_too_short_to_probe_is_still_verified(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.suppress_appends = True
        body_file = tmp_path / "body.md"
        body_file.write_text("ok\n", encoding="utf-8")

        result = runner.invoke(notion_app, ["append", notion.page_id, "--body-file", str(body_file)])

        assert result.exit_code == 9, result.output

    def test_a_raw_blocks_append_that_lands_reports_success(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        blocks_file = tmp_path / "blocks.json"
        blocks_file.write_text(
            json.dumps(
                [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": "landed"}}]},
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(notion_app, ["append", notion.page_id, "--blocks-file", str(blocks_file)])

        assert result.exit_code == 0, result.output
        assert "verified by re-fetch" in result.output

    def test_append_after_a_heading_lands_before_the_next_section(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        for name in ("Alpha", "Beta", "Gamma"):
            notion.heading(name, level=3)
            notion.paragraph(f"{name.lower()} body")
        body_file = tmp_path / "body.md"
        body_file.write_text("inserted line\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["append", notion.page_id, "--body-file", str(body_file), "--after-heading", "Beta"]
        )

        assert result.exit_code == 0, result.output
        assert notion.body_texts(notion.page_id) == [
            "Alpha",
            "alpha body",
            "Beta",
            "beta body",
            "inserted line",
            "Gamma",
            "gamma body",
        ]

    def test_append_after_an_absent_heading_exits_15(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.heading("Alpha", level=3)
        body_file = tmp_path / "body.md"
        body_file.write_text("inserted line\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["append", notion.page_id, "--body-file", str(body_file), "--after-heading", "Absent"]
        )

        assert result.exit_code == 15, result.output
        assert "Absent" in result.output
        assert notion.body_texts(notion.page_id) == ["Alpha"]

    def test_append_after_a_toggle_heading_lands_as_its_next_sibling(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        toggle_id = notion.heading("Owned", toggle=True)
        notion.paragraph("inside the toggle", parent=toggle_id)
        notion.heading("Next Section")
        body_file = tmp_path / "body.md"
        body_file.write_text("inserted line\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["append", notion.page_id, "--body-file", str(body_file), "--after-heading", "Owned"]
        )

        assert result.exit_code == 0, result.output
        assert notion.body_texts(notion.page_id) == ["Owned", "inserted line", "Next Section"]
        assert notion.body_texts(toggle_id) == ["inside the toggle"]

    def test_append_after_an_explicitly_empty_heading_exits_15_and_writes_nothing(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        """A blank ``--after-heading`` names no position, so it must refuse rather than land at the end."""
        notion.heading("Alpha", level=3)
        notion.paragraph("alpha body")
        body_file = tmp_path / "body.md"
        body_file.write_text("inserted line\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["append", notion.page_id, "--body-file", str(body_file), "--after-heading", ""]
        )

        assert result.exit_code == 15, result.output
        assert notion.body_texts(notion.page_id) == ["Alpha", "alpha body"]
        assert ("PATCH", f"/blocks/{notion.page_id}/children") not in notion.requests

    def test_append_after_a_heading_with_no_body_anchors_on_the_heading(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.heading("Beta", level=3)
        notion.heading("Gamma", level=3)
        body_file = tmp_path / "body.md"
        body_file.write_text("inserted line\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["append", notion.page_id, "--body-file", str(body_file), "--after-heading", "Beta"]
        )

        assert result.exit_code == 0, result.output
        assert notion.body_texts(notion.page_id) == ["Beta", "inserted line", "Gamma"]


class TestCommentOn:
    def test_the_comment_is_anchored_on_the_block_holding_the_quote_and_quotes_it(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.paragraph("Intro.")
        target = notion.paragraph("Rates reset every quarter.")
        body_file = tmp_path / "note.md"
        body_file.write_text("Monthly, per the bank's mail?\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["comment", "on", notion.page_id, "--quote", "every quarter", "--body-file", str(body_file)]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["block_id"] == target
        assert notion.comments[-1]["parent"] == {"block_id": target}
        assert notion.comment_texts()[-1] == "“every quarter”\n\nMonthly, per the bank's mail?"

    def test_a_quote_that_is_not_on_the_page_exactly_once_posts_nothing(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.paragraph("Rates reset every quarter.")
        body_file = tmp_path / "note.md"
        body_file.write_text("question\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["comment", "on", notion.page_id, "--quote", "every year", "--body-file", str(body_file)]
        )

        assert result.exit_code == 19, result.output
        assert notion.comments == []


class TestCommentReply:
    def test_the_reply_lands_inside_the_discussion_and_is_verified_on_its_anchor(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        paragraph = notion.paragraph("the lookup returns the customer number")
        notion.comment_on(paragraph, "which number?", discussion_id="disc-9")
        body_file = tmp_path / "reply.md"
        body_file.write_text("The one from the core banking system.\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, ["comment", "reply", notion.page_id, "--discussion", "disc-9", "--body-file", str(body_file)]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["outcome"] == "posted"
        assert notion.comments[-1]["discussion_id"] == "disc-9"
        assert notion.comments[-1]["parent"] == {"type": "block_id", "block_id": paragraph}

    def test_a_repeated_reply_is_a_duplicate_and_a_fresh_marker_posts_it_on_purpose(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.comment_on(notion.paragraph("requirement"), "open question", discussion_id="disc-9")
        body_file = tmp_path / "reply.md"
        body_file.write_text("answer\n", encoding="utf-8")
        reply = ["comment", "reply", notion.page_id, "--discussion", "disc-9", "--body-file", str(body_file)]

        outcomes = [
            json.loads(runner.invoke(notion_app, [*reply, *extra]).stdout)["outcome"]
            for extra in ([], [], ["--marker", "[t3:second]"])
        ]

        assert outcomes == ["posted", "duplicate", "posted"]
        assert len(notion.comments) == 3

    def test_a_discussion_that_is_not_on_the_page_gets_no_reply(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        notion.comment_on(notion.paragraph("requirement"), "open question", discussion_id="disc-here")
        body_file = tmp_path / "reply.md"
        body_file.write_text("answer\n", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["comment", "reply", notion.page_id, "--discussion", "disc-elsewhere", "--body-file", str(body_file)],
        )

        assert result.exit_code == 21, result.output
        assert len(notion.comments) == 1


class TestCommentWritesStayInsideTheWriteScope:
    @pytest.fixture(autouse=True)
    def _anchored_discussion(self, notion: FakeNotion, tmp_path: Path) -> None:
        self.paragraph = notion.paragraph("Rates reset every quarter.")
        notion.comment_on(self.paragraph, "which rate?", discussion_id="disc-9")
        self.body_file = tmp_path / "note.md"
        self.body_file.write_text("answer\n", encoding="utf-8")

    @pytest.mark.parametrize("scope", ["no-root-configured", "allowed-root-elsewhere", "under-a-denied-root"])
    @pytest.mark.parametrize(
        "verb", [["on", "--quote", "every quarter"], ["reply", "--discussion", "disc-9"]], ids=["on", "reply"]
    )
    def test_a_comment_outside_the_scope_exits_17_and_posts_nothing(
        self,
        runner: typer.testing.CliRunner,
        notion: FakeNotion,
        monkeypatch: pytest.MonkeyPatch,
        verb: list[str],
        scope: str,
    ) -> None:
        roots = {
            "no-root-configured": ([], []),
            "allowed-root-elsewhere": ([_ELSEWHERE], []),
            "under-a-denied-root": ([notion.page_id], [notion.page_id]),
        }[scope]
        monkeypatch.setattr("teatree.backends.notion.write_guard.notion_write_roots", lambda _overlay: roots)

        result = runner.invoke(
            notion_app, ["comment", verb[0], notion.page_id, *verb[1:], "--body-file", str(self.body_file)]
        )

        assert result.exit_code == 17, result.output
        assert ("POST", "/comments") not in notion.requests


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
        payload = json.loads(result.stdout)
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
        assert json.loads(result.stdout)["outcome"] == "duplicate"
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
        assert json.loads(result.stdout)["status"] == {"name": "In review"}

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
        payload = json.loads(result.stdout)
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
        assert "config_setting set notion_token_pass_key" in result.output

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

    def test_an_unreadable_parent_database_names_the_integration_to_share_it_with(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph(self.DEAD_SPEC)
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (404, "object_not_found")

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert "parent_database_unreadable" in result.output
        assert "share the parent database with integration 'Factory'" in result.output

    def test_a_parent_database_query_that_fails_for_another_reason_is_not_blamed_on_sharing(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        notion.paragraph(self.DEAD_SPEC)
        notion.make_database_row(database_id="db-backlog")
        notion.query_fail_with = (503, "service_unavailable")

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert "parent_database_unverified" in result.output
        assert "share the parent database" not in result.output
        assert "share that database" not in result.output

    def test_a_row_its_database_does_not_return_is_not_blamed_on_sharing(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        notion.paragraph(self.DEAD_SPEC)
        notion.make_database_row(database_id="db-backlog")
        notion.rows = []

        result = runner.invoke(notion_app, ["fetch", notion.page_id])

        assert result.exit_code == 14, result.output
        assert "absent_from_parent_database" in result.output
        assert "share the parent database" not in result.output

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

    @pytest.mark.parametrize("fragment", UNSEEN_OBJECT_FRAGMENTS.values(), ids=UNSEEN_OBJECT_FRAGMENTS.keys())
    def test_the_page_line_for_an_unseen_page_carries_the_bot_the_causes_and_the_checks(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, fragment: str
    ) -> None:
        notion.fail_with = (404, "object_not_found")

        result = runner.invoke(notion_app, ["doctor", notion.page_id])

        page_line = next(line for line in result.output.splitlines() if line.startswith("page:  FAIL"))
        assert result.exit_code == 6
        assert fragment in page_line

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

    def test_a_capability_denial_keeps_its_own_code_on_the_page_line(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        # The page/live stages are shared with `t3 notion setup` through `page_verdict`;
        # the exit codes and the line prefixes are the CLI contract that sharing must not shift.
        notion.fail_with = (403, "restricted_resource")

        result = runner.invoke(notion_app, ["doctor", notion.page_id])

        assert result.exit_code == 5, result.output
        assert "token: OK" in result.output
        assert "page:  FAIL" in result.output


_WRITES = {
    "append": ["append", "{page}", "--body-file", "{body}"],
    "section-replace": ["section", "replace", "{page}", "--heading", "Notes", "--body-file", "{body}"],
    "comment-post": ["comment", "post", "{page}", "--body-file", "{body}"],
    "comment-on": ["comment", "on", "{page}", "--quote", "every quarter", "--body-file", "{body}"],
    "comment-reply": ["comment", "reply", "{page}", "--discussion", "disc-9", "--body-file", "{body}"],
    "property-set": ["property", "set", "{page}", "--name", "Status", "--value", "Merged"],
    "create": ["create", "{page}", "--title", "Fresh page", "--body-file", "{body}"],
    "replace": ["replace", "{page}", "--old-file", "{old}", "--new-file", "{new}"],
    "append-text": ["append", "{page}", "--body-text", "a note long enough to probe"],
    "section-replace-text": ["section", "replace", "{page}", "--heading", "Notes", "--body-text", "a note to probe"],
    "comment-post-text": ["comment", "post", "{page}", "--body-text", "a note long enough to probe"],
    "comment-on-text": ["comment", "on", "{page}", "--quote", "every quarter", "--body-text", "a note to probe"],
    "comment-reply-text": ["comment", "reply", "{page}", "--discussion", "disc-9", "--body-text", "a note to probe"],
    "create-text": ["create", "{page}", "--title", "Fresh page", "--body-text", "a note long enough to probe"],
    "replace-text": ["replace", "{page}", "--old-text", "every quarter", "--new-text", "every month"],
}


class TestEveryWriteNamesWhoItWritesAs:
    @pytest.fixture(autouse=True)
    def _page(self, notion: FakeNotion, tmp_path: Path) -> None:
        notion.comment_on(notion.paragraph("Rates reset every quarter."), "which rate?", discussion_id="disc-9")
        notion.set_property("Status", {"type": "status", "status": {"name": "In review"}})
        files = {"body": "a note long enough to probe\n", "old": "every quarter\n", "new": "every month\n"}
        for name, text in files.items():
            (tmp_path / f"{name}.md").write_text(text, encoding="utf-8")
        self.paths = {name: str(tmp_path / f"{name}.md") for name in files}

    def command(self, notion: FakeNotion, verb: str) -> list[str]:
        return [part.format(page=notion.page_id, **self.paths) for part in _WRITES[verb]]

    @pytest.mark.parametrize("verb", list(_WRITES))
    def test_the_identity_and_overlay_are_printed_on_stderr(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, verb: str
    ) -> None:
        result = runner.invoke(notion_app, self.command(notion, verb))

        assert result.exit_code == 0, result.output
        assert "writing as integration 'Factory' (bot id bot-1)" in result.stderr

    @pytest.mark.parametrize("verb", list(_WRITES))
    def test_an_unreadable_identity_stops_the_write_with_a_named_error(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch, verb: str
    ) -> None:
        monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        notion.identity_fail_with = (503, "service_unavailable")

        result = runner.invoke(notion_app, self.command(notion, verb))

        assert result.exit_code == 2, result.output
        assert "nothing was written" in result.stderr
        writes = [(method, path) for method, path in notion.requests if method in {"PATCH", "DELETE"}]
        assert writes + [(method, path) for method, path in notion.requests if path in {"/pages", "/comments"}] == []


_FILE_AND_TEXT = {
    "append": ["append", "{page}", "--body-file", "{body}", "--body-text", "x"],
    "section-replace": [
        "section",
        "replace",
        "{page}",
        "--heading",
        "Notes",
        "--body-file",
        "{body}",
        "--body-text",
        "x",
    ],
    "comment-post": ["comment", "post", "{page}", "--body-file", "{body}", "--body-text", "x"],
    "comment-on": ["comment", "on", "{page}", "--quote", "q", "--body-file", "{body}", "--body-text", "x"],
    "comment-reply": [
        "comment",
        "reply",
        "{page}",
        "--discussion",
        "d",
        "--body-file",
        "{body}",
        "--body-text",
        "x",
    ],
    "create": ["create", "{page}", "--title", "T", "--body-file", "{body}", "--body-text", "x"],
}


class TestEveryBodyTakesExactlyOneOfFileOrText:
    @pytest.mark.parametrize("verb", list(_FILE_AND_TEXT))
    def test_a_file_together_with_text_is_refused_and_nothing_is_sent(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path, verb: str
    ) -> None:
        body = tmp_path / "body.md"
        body.write_text("a note long enough to probe\n", encoding="utf-8")

        result = runner.invoke(
            notion_app, [part.format(page=notion.page_id, body=body) for part in _FILE_AND_TEXT[verb]]
        )

        assert result.exit_code == 1, result.output
        assert "--body-text" in result.stderr
        assert notion.requests == []

    @pytest.mark.parametrize(
        "args",
        [
            ["comment", "post", "{page}"],
            ["section", "replace", "{page}", "--heading", "Notes"],
            ["comment", "on", "{page}", "--quote", "q"],
            ["comment", "reply", "{page}", "--discussion", "d"],
            ["append", "{page}"],
        ],
    )
    def test_neither_is_refused_too(self, runner: typer.testing.CliRunner, notion: FakeNotion, args: list[str]) -> None:
        result = runner.invoke(notion_app, [part.format(page=notion.page_id) for part in args])

        assert result.exit_code == 1, result.output
        assert notion.requests == []

    @pytest.mark.parametrize("args", [["append", "{page}"], ["create", "{page}", "--title", "T"]])
    def test_the_verbs_that_take_raw_blocks_name_that_option_when_nothing_is_given(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, args: list[str]
    ) -> None:
        result = runner.invoke(notion_app, [part.format(page=notion.page_id) for part in args])

        assert result.exit_code == 1, result.output
        assert "--blocks-file" in result.stderr

    def test_an_empty_comment_text_exits_1_rather_than_raising(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        result = runner.invoke(notion_app, ["comment", "post", notion.page_id, "--body-text", "  "])

        assert result.exit_code == 1, result.output
        assert "empty comment" in result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert ("POST", "/comments") not in notion.requests
