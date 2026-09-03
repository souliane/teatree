"""``t3 notion setup`` — the walkthrough that mints, stores and verifies the token.

The whole command exists to make one secret travel from a browser to the ``pass``
entry the readers resolve, and the security property is that it travels nowhere
else. :class:`TestTheSecretNeverTravels` pins both halves: absent from the
output, and present in the store — an absence assertion alone would pass just as
well against a command that stored nothing.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
import typer.testing

from teatree.cli.notion import notion_app
from teatree.cli.notion_setup import CAPABILITIES, NOTION_INTEGRATIONS_URL
from teatree.utils.secrets import SecretStoreError
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

_DEFAULT_KEY = "notion/integration-token"
_SECRET = "ntn_pasted_integration_secret"
_ALREADY_STORED = "ntn_resolved_from_the_store"


class FakePassStore:
    """The ``pass`` store as a dict, recording every write in order."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def read(self, key: str) -> str:
        return self.entries.get(key, "")

    def write(self, key: str, value: str) -> bool:
        self.writes.append((key, value))
        self.entries[key] = value
        return True


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    return install_fake_notion(monkeypatch)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakePassStore:
    """A dict-backed ``pass`` store patched into the writer, the reader and the resolver."""
    fake = FakePassStore()
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setattr("teatree.utils.secrets.read_pass", fake.read)
    monkeypatch.setattr("teatree.utils.secrets.write_pass", fake.write)
    monkeypatch.setattr("teatree.cli.notion_setup.read_pass", fake.read)
    monkeypatch.setattr("teatree.llm.credentials.read_pass", fake.read)
    monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: "")
    return fake


@pytest.fixture(autouse=True)
def browser() -> Iterator[MagicMock]:
    """Autouse so no test can open a real browser tab on whoever is running the suite."""
    with patch("teatree.cli.notion_setup.webbrowser.open") as opened:
        yield opened


@pytest.fixture
def runner() -> typer.testing.CliRunner:
    return typer.testing.CliRunner()


class TestInstructions:
    def test_opens_the_integrations_page_and_lists_every_capability(
        self, runner: typer.testing.CliRunner, browser: MagicMock, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        browser.assert_called_once_with(NOTION_INTEGRATIONS_URL)
        assert NOTION_INTEGRATIONS_URL in result.output, "a headless box has no browser — print the URL too"
        for capability in CAPABILITIES:
            assert capability in result.output

    def test_the_capability_checklist_precedes_the_first_prompt(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        # The operator must know what to grant BEFORE being asked to overwrite a
        # working token, or the decision is made without the information.
        store.entries[_DEFAULT_KEY] = "previous"

        result = runner.invoke(notion_app, ["setup"], input="n\n")

        assert result.exit_code == 1
        assert 0 <= result.output.find(CAPABILITIES[-1]) < result.output.find("already holds a value")


class TestPassKeyRouting:
    def test_writes_to_the_key_the_active_overlay_routes_to(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: "acme/notion"
        )

        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert store.writes == [("acme/notion", _SECRET)], "setup must write where the overlay's own reader reads"

    def test_falls_back_to_the_default_entry_when_no_overlay_routes_one(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert store.writes == [(_DEFAULT_KEY, _SECRET)]

    def test_a_named_overlay_that_does_not_resolve_stores_nothing(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        # A typo in the `--overlay` the doctor hands out would store the token where the check never reads.
        result = runner.invoke(notion_app, ["setup", "--overlay", "typo"], input=f"{_SECRET}\n")

        assert result.exit_code == 1, result.output
        assert store.writes == []
        assert "typo" in result.output

    def test_a_named_overlay_that_resolves_is_accepted(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup", "--overlay", "t3-teatree"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert store.writes == [(_DEFAULT_KEY, _SECRET)]


class TestTheSecretNeverTravels:
    def test_the_pasted_secret_reaches_the_store_and_not_the_output(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        # Click echoes a VISIBLE prompt's input into `result.output` and a hidden
        # one's not at all, so the absence goes RED the moment `hide_input` is
        # dropped — and the positive half keeps it from passing vacuously.
        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert store.entries[_DEFAULT_KEY] == _SECRET, "the token must reach the store"
        assert _SECRET not in result.output, "the prompt must hide its input"

    def test_there_is_no_token_option_to_put_it_in_argv(self, runner: typer.testing.CliRunner) -> None:
        result = runner.invoke(notion_app, ["setup", "--help"])

        assert result.exit_code == 0, result.output
        assert "--token" not in result.output, "a value on argv lands in the process table and the shell history"


class TestVerifyAfterStore:
    def test_the_bot_identity_is_printed_because_pages_are_shared_with_it(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "bot-1" in result.output
        assert "Factory" in result.output

    def test_the_verification_authenticates_with_the_value_read_back_from_the_store(
        self,
        runner: typer.testing.CliRunner,
        notion: FakeNotion,
        store: FakePassStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The resolver answers something OTHER than the pasted secret, so a client built from that goes red.
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _key: _ALREADY_STORED)

        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert ("GET", "/users/me") in notion.requests
        assert notion.bearer_tokens == [_ALREADY_STORED]

    def test_a_token_notion_rejects_exits_four(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        notion.identity_fail_with = (401, "unauthorized")

        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 4, result.output

    def test_an_empty_paste_stores_nothing_and_exits_nonzero(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup"], input="   \n")

        assert result.exit_code == 1, result.output
        assert store.writes == []


class TestEnvShadow:
    def test_an_exported_token_that_beats_the_store_is_named_in_a_warning(
        self,
        runner: typer.testing.CliRunner,
        notion: FakeNotion,
        store: FakePassStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Env wins over `pass` by design, so the entry just written is inert here
        # until the export goes away — silence would read as a working setup.
        monkeypatch.setenv("NOTION_TOKEN", "ntn_exported_and_different")

        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "WARN" in result.output
        assert "NOTION_TOKEN" in result.output


class TestOverwrite:
    def test_an_existing_value_is_backed_up_before_it_is_overwritten(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        store.entries[_DEFAULT_KEY] = "previous"

        result = runner.invoke(notion_app, ["setup", "--reset"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        backup_key, backed_up = store.writes[0]
        assert backup_key.startswith(f"{_DEFAULT_KEY}.bak-")
        assert backed_up == "previous"
        assert store.writes[1] == (_DEFAULT_KEY, _SECRET)

    def test_default_mode_aborts_on_a_declined_overwrite(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        store.entries[_DEFAULT_KEY] = "previous"

        result = runner.invoke(notion_app, ["setup"], input="n\n")

        assert result.exit_code == 1
        assert store.writes == []

    def test_reset_overwrites_without_asking(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        store.entries[_DEFAULT_KEY] = "previous"

        result = runner.invoke(notion_app, ["setup", "--reset"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "already holds a value" not in result.output


class TestAWedgedStore:
    def test_an_unreadable_entry_fails_with_the_clis_own_line_not_a_traceback(
        self,
        runner: typer.testing.CliRunner,
        notion: FakeNotion,
        store: FakePassStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A stale gpg lock from a dead pid lands on the walkthrough's FIRST real action.
        def wedged(key: str) -> str:
            raise SecretStoreError.timed_out(key, 5.0)

        monkeypatch.setattr("teatree.cli.notion_setup.read_pass", wedged)

        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 1
        assert "timed out" in result.output, "a raw traceback is not the CLI's own FAIL line"
        assert store.writes == []


class TestSharingPass:
    def test_a_shared_page_reports_one_reachable_line(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup", "--page", notion.page_id], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert f"OK    {notion.page_id} — readable and live" in result.output

    def test_an_ungranted_page_names_the_identity_and_the_connections_step(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        notion.fail_with = (404, "object_not_found")

        result = runner.invoke(notion_app, ["setup", "--page", notion.page_id], input=f"{_SECRET}\n")

        assert result.exit_code == 6, result.output
        assert "Connections" in result.output
        assert "bot-1" in result.output

    def test_every_page_is_reported_before_the_run_exits_on_the_first_failure(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        # A 40-page table is the point: stopping at the first miss would leave the
        # operator re-running setup once per page to discover the rest.
        notion.fail_with = (404, "object_not_found")
        second = "22222222-2222-2222-2222-222222222222"

        result = runner.invoke(notion_app, ["setup", "--page", notion.page_id, "--page", second], input=f"{_SECRET}\n")

        assert result.exit_code == 6, result.output
        assert notion.page_id in result.output
        assert second in result.output

    def test_a_reference_carrying_no_object_id_keeps_its_own_exit_code(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup", "--page", "https://example.test/doc"], input=f"{_SECRET}\n")

        assert result.exit_code == 7, result.output
        assert "carries no Notion object id" in result.output

    def test_naming_no_page_probes_no_page_at_all(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert not [path for _method, path in notion.requests if path.startswith("/pages/")]


class TestDeployedContainers:
    def test_the_walkthrough_says_how_the_deployed_stack_picks_the_token_up(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        result = runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "Deploy" in result.output
        assert "teatree.env" in result.output, "the operator must be told there is no file to edit"
        assert store.writes == [(_DEFAULT_KEY, _SECRET)], "the only write is the one pass entry"

    def test_an_unshared_page_still_gets_the_note_and_one_copy_of_the_error(
        self, runner: typer.testing.CliRunner, notion: FakeNotion, store: FakePassStore
    ) -> None:
        # An operator with sharing work left is exactly the one who has not re-deployed yet.
        notion.fail_with = (404, "object_not_found")

        result = runner.invoke(notion_app, ["setup", "--page", notion.page_id], input=f"{_SECRET}\n")

        assert result.exit_code == 6, result.output
        assert "teatree.env" in result.output
        assert result.output.count("is not shared with this integration") == 1
