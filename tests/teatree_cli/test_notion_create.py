"""``t3 notion create`` — a child page under a reachable parent, verified by re-read."""

import json
from pathlib import Path

import pytest
import typer.testing

from teatree.cli.notion import notion_app
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

_PARENT = "11111111-1111-1111-1111-111111111111"
_ELSEWHERE = "99999999-9999-9999-9999-999999999999"
_DATABASE = "33333333-3333-3333-3333-333333333333"
_ROOTS = "teatree.backends.notion.write_guard.notion_write_roots"


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    return install_fake_notion(monkeypatch)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> typer.testing.CliRunner:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    return typer.testing.CliRunner()


@pytest.fixture
def body(tmp_path: Path) -> Path:
    path = tmp_path / "body.md"
    path.write_text("## Scope\n\nThe factory owns the build, never the request path.\n", encoding="utf-8")
    return path


def _create(runner: typer.testing.CliRunner, parent: str, body: Path, *extra: str) -> typer.testing.Result:
    return runner.invoke(notion_app, ["create", parent, "--title", "CTO brief", "--body-file", str(body), *extra])


def _posted_pages(notion: FakeNotion) -> list[tuple[str, str]]:
    return [request for request in notion.requests if request == ("POST", "/pages")]


class TestCreate:
    def test_creates_a_child_page_and_prints_its_url_once_the_re_read_confirms_it(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path
    ) -> None:
        result = _create(runner, notion.page_id, body, "--icon", "🏗️")

        assert result.exit_code == 0, result.output
        assert "writing as integration 'Factory' (bot id bot-1)" in result.stderr
        created = json.loads(result.stdout)
        assert created["outcome"] == "created"
        assert created["url"] == f"https://www.notion.so/{created['page_id']}"
        page = notion.pages[created["page_id"]]
        assert page["parent"] == {"type": "page_id", "page_id": notion.page_id}
        assert page["icon"] == {"type": "emoji", "emoji": "🏗️"}
        assert notion.body_texts(created["page_id"]) == ["Scope", "The factory owns the build, never the request path."]

    def test_a_database_parent_titles_the_row_through_its_title_property(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notion.databases.add(_DATABASE)
        notion.rows = []
        monkeypatch.setattr(_ROOTS, lambda _overlay: ([_DATABASE], []))

        result = _create(runner, _DATABASE, body)

        assert result.exit_code == 0, result.output
        page = notion.pages[json.loads(result.stdout)["page_id"]]
        assert page["parent"] == {"type": "database_id", "database_id": _DATABASE}
        assert page["properties"]["Name"]["title"][0]["plain_text"] == "CTO brief"

    def test_a_body_longer_than_one_request_lands_whole(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        long_body = tmp_path / "long.md"
        long_body.write_text("\n\n".join(f"paragraph number {n}" for n in range(150)), encoding="utf-8")

        result = _create(runner, notion.page_id, long_body)

        assert result.exit_code == 0, result.output
        page_id = json.loads(result.stdout)["page_id"]
        assert len(notion.body_texts(page_id)) == 150
        assert ("PATCH", f"/blocks/{page_id}/children") in notion.requests

    def test_a_page_already_titled_so_is_reported_and_nothing_is_written(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path
    ) -> None:
        first = json.loads(_create(runner, notion.page_id, body).stdout)

        result = _create(runner, notion.page_id, body)

        assert result.exit_code == 0, result.output
        again = json.loads(result.stdout)
        assert again["outcome"] == "exists"
        assert again["page_id"] == first["page_id"]
        assert len(_posted_pages(notion)) == 1

    def test_a_database_row_already_titled_so_and_carrying_the_body_is_reported_and_nothing_is_written(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notion.databases.add(_DATABASE)
        notion.rows = [
            {"id": "row-9", "properties": {"Name": {"type": "title", "title": [{"plain_text": "CTO brief"}]}}}
        ]
        for text in ("Scope", "The factory owns the build, never the request path."):
            notion.paragraph(text, parent="row-9")
        monkeypatch.setattr(_ROOTS, lambda _overlay: ([_DATABASE], []))

        result = _create(runner, _DATABASE, body)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["page_id"] == "row-9"
        assert notion.query_filters[-1] == {"property": "Name", "title": {"equals": "CTO brief"}}
        assert _posted_pages(notion) == []


class TestRefusals:
    @pytest.mark.parametrize(
        "roots",
        [([], []), ([_ELSEWHERE], []), ([_PARENT], [_PARENT])],
        ids=["no-root-configured", "allowed-root-elsewhere", "under-a-denied-root"],
    )
    def test_a_parent_the_write_scope_excludes_is_refused_before_anything_is_created(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path, roots: tuple[list[str], list[str]]
    ) -> None:
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(_ROOTS, lambda _overlay: roots)
            result = _create(runner, notion.page_id, body)

        assert result.exit_code == 17, result.output
        assert "refusing to write" in result.output
        assert _posted_pages(notion) == []

    @pytest.mark.parametrize(
        "roots",
        [([], []), ([_ELSEWHERE], []), ([_DATABASE], [_DATABASE])],
        ids=["no-root-configured", "allowed-root-elsewhere", "under-a-denied-root"],
    )
    def test_a_database_parent_the_write_scope_excludes_is_refused(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path, roots: tuple[list[str], list[str]]
    ) -> None:
        notion.databases.add(_DATABASE)
        notion.rows = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(_ROOTS, lambda _overlay: roots)
            result = _create(runner, _DATABASE, body)

        assert result.exit_code == 17, result.output
        assert _posted_pages(notion) == []

    def test_an_unshared_parent_states_the_api_limit_and_the_whoami_remedy(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path
    ) -> None:
        notion.unshared_blocks.add(notion.page_id)

        result = _create(runner, notion.page_id, body)

        assert result.exit_code == 6, result.output
        assert "workspace-level" in result.output
        assert "share the parent with the integration named by `t3 notion whoami`" in result.output
        assert _posted_pages(notion) == []

    def test_a_dead_parent_is_refused(self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path) -> None:
        notion.page_archived = True

        result = _create(runner, notion.page_id, body)

        assert result.exit_code == 14, result.output
        assert _posted_pages(notion) == []

    def test_a_trashed_database_parent_is_refused(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path
    ) -> None:
        notion.databases.add(_DATABASE)
        notion.trashed_blocks.add(_DATABASE)

        result = _create(runner, _DATABASE, body)

        assert result.exit_code == 14, result.output
        assert _posted_pages(notion) == []

    def test_a_parent_that_is_neither_a_page_nor_a_database_is_refused(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path
    ) -> None:
        notion.blocks[_DATABASE] = {"type": "paragraph"}

        result = _create(runner, _DATABASE, body)

        assert result.exit_code == 2, result.output
        assert "a page can only be created under a page or a database" in result.output
        assert _posted_pages(notion) == []

    @pytest.mark.parametrize("suppressed", ["suppress_page_bodies", "suppress_page_titles"])
    def test_a_create_that_did_not_land_exits_with_the_write_not_landed_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path, suppressed: str
    ) -> None:
        setattr(notion, suppressed, True)

        result = _create(runner, notion.page_id, body)

        assert result.exit_code == 9, result.output
        assert "treat the write as failed" in result.output

    def test_a_blank_title_is_refused_before_notion_is_asked(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, body: Path
    ) -> None:
        result = runner.invoke(notion_app, ["create", notion.page_id, "--title", "  ", "--body-file", str(body)])

        assert result.exit_code == 1
        assert notion.requests == []

    def test_the_help_states_that_a_workspace_level_page_cannot_be_created(
        self, runner: typer.testing.CliRunner
    ) -> None:
        result = runner.invoke(notion_app, ["create", "--help"])

        assert result.exit_code == 0
        assert "workspace-level" in " ".join(result.output.split())


class TestARetryNeverPassesAHalfWrittenPage:
    def test_a_page_a_previous_create_left_short_is_reported_incomplete(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        long_body = tmp_path / "long.md"
        long_body.write_text("\n\n".join(f"paragraph number {n}" for n in range(150)), encoding="utf-8")
        notion.suppress_appends = True
        assert _create(runner, notion.page_id, long_body).exit_code == 9
        notion.suppress_appends = False

        result = _create(runner, notion.page_id, long_body)

        assert result.exit_code == 9, result.output
        assert "100 of 150 block(s)" in result.output
        assert "t3 notion append" in result.output
        assert "stopped part-way" not in result.output
        assert len(_posted_pages(notion)) == 1


class TestInlineBodyAndOverlay:
    def test_body_text_creates_the_page_with_no_file_on_disk(
        self, runner: typer.testing.CliRunner, notion: FakeNotion
    ) -> None:
        result = runner.invoke(
            notion_app,
            [
                "create",
                notion.page_id,
                "--title",
                "CTO brief",
                "--body-text",
                "## Scope\n\nThe factory owns the build.",
            ],
        )

        assert result.exit_code == 0, result.output
        created = json.loads(result.stdout)
        assert notion.body_texts(created["page_id"]) == ["Scope", "The factory owns the build."]

    def test_a_blocks_file_together_with_body_text_is_refused(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, tmp_path: Path
    ) -> None:
        blocks = tmp_path / "blocks.json"
        blocks.write_text("[]", encoding="utf-8")

        result = runner.invoke(
            notion_app,
            ["create", notion.page_id, "--title", "T", "--blocks-file", str(blocks), "--body-text", "x"],
        )

        assert result.exit_code == 1, result.output
        assert notion.requests == []

    def test_the_overlay_option_routes_the_token_and_the_write_roots_like_every_other_verb(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name: "")

        result = runner.invoke(
            notion_app, ["create", notion.page_id, "--title", "T", "--body-text", "x marks", "--overlay", "t3-acme"]
        )

        assert result.exit_code == 0, result.output
        assert "write roots of overlay t3-acme" in result.stderr
