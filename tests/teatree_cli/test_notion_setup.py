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
from django.test import TestCase

from teatree.cli.notion import notion_app
from teatree.cli.notion_setup import CAPABILITIES, NOTION_INTEGRATIONS_URL
from teatree.utils.secrets import SecretStoreError
from tests.factories import TicketFactory
from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion

_ROUTED_KEY = "acme/notion"
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
    monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: _ROUTED_KEY)
    return fake


@pytest.fixture(autouse=True)
def browser() -> Iterator[MagicMock]:
    """Autouse so no test can open a real browser tab on whoever is running the suite."""
    with patch("teatree.cli.notion_setup.webbrowser.open") as opened:
        yield opened


@pytest.fixture
def runner() -> typer.testing.CliRunner:
    return typer.testing.CliRunner()


class NotionSetupCase(TestCase):
    """Setup reads the ticket table for tracked pages, so every case runs against the test DB."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(
        self,
        runner: typer.testing.CliRunner,
        notion: FakeNotion,
        store: FakePassStore,
        browser: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.runner = runner
        self.notion = notion
        self.store = store
        self.browser = browser
        self.monkeypatch = monkeypatch


class TestInstructions(NotionSetupCase):
    def test_opens_the_integrations_page_and_lists_every_capability(self) -> None:
        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        self.browser.assert_called_once_with(NOTION_INTEGRATIONS_URL)
        assert NOTION_INTEGRATIONS_URL in result.output, "a headless box has no browser — print the URL too"
        for capability in CAPABILITIES:
            assert capability in result.output

    def test_the_capability_checklist_precedes_the_first_prompt(self) -> None:
        # The operator must know what to grant BEFORE being asked to overwrite a
        # working token, or the decision is made without the information.
        self.store.entries[_ROUTED_KEY] = "previous"

        result = self.runner.invoke(notion_app, ["setup"], input="n\n")

        assert result.exit_code == 1
        assert 0 <= result.output.find(CAPABILITIES[-1]) < result.output.find("already holds a value")


class TestPassKeyRouting(NotionSetupCase):
    def test_writes_to_the_key_the_active_overlay_routes_to(self) -> None:
        self.monkeypatch.setattr(
            "teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: "venue/notion"
        )

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert self.store.writes == [("venue/notion", _SECRET)], "setup must write where the overlay's own reader reads"

    def test_an_unrouted_credential_stores_nothing_and_names_the_setting(self) -> None:
        self.monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _name=None: "")

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 1, result.output
        assert self.store.writes == []
        assert "config_setting set notion_token_pass_key" in result.output

    def test_a_named_overlay_that_does_not_resolve_stores_nothing(self) -> None:
        # A typo in the `--overlay` the doctor hands out would store the token where the check never reads.
        result = self.runner.invoke(notion_app, ["setup", "--overlay", "typo"], input=f"{_SECRET}\n")

        assert result.exit_code == 1, result.output
        assert self.store.writes == []
        assert "typo" in result.output

    def test_a_named_overlay_that_resolves_is_accepted(self) -> None:
        result = self.runner.invoke(notion_app, ["setup", "--overlay", "t3-teatree"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert self.store.writes == [(_ROUTED_KEY, _SECRET)]


class TestTheSecretNeverTravels(NotionSetupCase):
    def test_the_pasted_secret_reaches_the_store_and_not_the_output(self) -> None:
        # Click echoes a VISIBLE prompt's input into `result.output` and a hidden
        # one's not at all, so the absence goes RED the moment `hide_input` is
        # dropped — and the positive half keeps it from passing vacuously.
        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert self.store.entries[_ROUTED_KEY] == _SECRET, "the token must reach the store"
        assert _SECRET not in result.output, "the prompt must hide its input"

    def test_there_is_no_token_option_to_put_it_in_argv(self) -> None:
        result = self.runner.invoke(notion_app, ["setup", "--help"])

        assert result.exit_code == 0, result.output
        assert "--token" not in result.output, "a value on argv lands in the process table and the shell history"


class TestVerifyAfterStore(NotionSetupCase):
    def test_the_bot_identity_is_printed_because_pages_are_shared_with_it(self) -> None:
        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "bot-1" in result.output
        assert "Factory" in result.output

    def test_the_verification_authenticates_with_the_value_read_back_from_the_store(self) -> None:
        # The resolver answers something OTHER than the pasted secret, so a client built from that goes red.
        self.monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _key: _ALREADY_STORED)

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert ("GET", "/users/me") in self.notion.requests
        assert self.notion.bearer_tokens == [_ALREADY_STORED]

    def test_a_token_notion_rejects_exits_four(self) -> None:
        self.notion.identity_fail_with = (401, "unauthorized")

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 4, result.output

    def test_an_empty_paste_stores_nothing_and_exits_nonzero(self) -> None:
        result = self.runner.invoke(notion_app, ["setup"], input="   \n")

        assert result.exit_code == 1, result.output
        assert self.store.writes == []


class TestEnvShadow(NotionSetupCase):
    def test_an_exported_token_that_beats_the_store_is_named_in_a_warning(self) -> None:
        # Env wins over `pass` by design, so the entry just written is inert here
        # until the export goes away — silence would read as a working setup.
        self.monkeypatch.setenv("NOTION_TOKEN", "ntn_exported_and_different")

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "WARN" in result.output
        assert "NOTION_TOKEN" in result.output


class TestOverwrite(NotionSetupCase):
    def test_an_existing_value_is_backed_up_before_it_is_overwritten(self) -> None:
        self.store.entries[_ROUTED_KEY] = "previous"

        result = self.runner.invoke(notion_app, ["setup", "--reset"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        backup_key, backed_up = self.store.writes[0]
        assert backup_key.startswith(f"{_ROUTED_KEY}.bak-")
        assert backed_up == "previous"
        assert self.store.writes[1] == (_ROUTED_KEY, _SECRET)

    def test_default_mode_aborts_on_a_declined_overwrite(self) -> None:
        self.store.entries[_ROUTED_KEY] = "previous"

        result = self.runner.invoke(notion_app, ["setup"], input="n\n")

        assert result.exit_code == 1
        assert self.store.writes == []

    def test_reset_overwrites_without_asking(self) -> None:
        self.store.entries[_ROUTED_KEY] = "previous"

        result = self.runner.invoke(notion_app, ["setup", "--reset"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "already holds a value" not in result.output


class TestAWedgedStore(NotionSetupCase):
    def test_an_unreadable_entry_fails_with_the_clis_own_line_not_a_traceback(self) -> None:
        # A stale gpg lock from a dead pid lands on the walkthrough's FIRST real action.
        def wedged(key: str) -> str:
            raise SecretStoreError.timed_out(key, 5.0)

        self.monkeypatch.setattr("teatree.cli.notion_setup.read_pass", wedged)

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 1
        assert "timed out" in result.output, "a raw traceback is not the CLI's own FAIL line"
        assert self.store.writes == []


class TestSharingPass(NotionSetupCase):
    def test_a_shared_page_reports_one_reachable_line(self) -> None:
        result = self.runner.invoke(notion_app, ["setup", "--page", self.notion.page_id], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert f"OK    {self.notion.page_id} — readable and live" in result.output

    def test_an_ungranted_page_names_the_identity_and_the_connections_step(self) -> None:
        self.notion.fail_with = (404, "object_not_found")

        result = self.runner.invoke(notion_app, ["setup", "--page", self.notion.page_id], input=f"{_SECRET}\n")

        assert result.exit_code == 6, result.output
        assert "Connections" in result.output
        assert "bot-1" in result.output

    def test_every_page_is_reported_before_the_run_exits_on_the_first_failure(self) -> None:
        # A 40-page table is the point: stopping at the first miss would leave the
        # operator re-running setup once per page to discover the rest.
        self.notion.fail_with = (404, "object_not_found")
        second = "22222222-2222-2222-2222-222222222222"

        result = self.runner.invoke(
            notion_app, ["setup", "--page", self.notion.page_id, "--page", second], input=f"{_SECRET}\n"
        )

        assert result.exit_code == 6, result.output
        assert self.notion.page_id in result.output
        assert second in result.output

    def test_a_reference_carrying_no_object_id_keeps_its_own_exit_code(self) -> None:
        result = self.runner.invoke(notion_app, ["setup", "--page", "https://example.test/doc"], input=f"{_SECRET}\n")

        assert result.exit_code == 7, result.output
        assert "carries no Notion object id" in result.output

    def test_no_named_and_no_tracked_page_probes_no_page_at_all(self) -> None:
        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert not [path for _method, path in self.notion.requests if path.startswith("/pages/")]

    def test_a_page_an_in_flight_ticket_tracks_is_checked_without_being_named(self) -> None:
        TicketFactory(extra={"notion_url": f"https://www.notion.so/Spec-{self.notion.page_id.replace('-', '')}"})

        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert f"OK    {self.notion.page_id} — readable and live" in result.output


class TestDeployedContainers(NotionSetupCase):
    def test_the_walkthrough_says_how_the_deployed_stack_picks_the_token_up(self) -> None:
        result = self.runner.invoke(notion_app, ["setup"], input=f"{_SECRET}\n")

        assert result.exit_code == 0, result.output
        assert "its own `pass` store" in result.output
        assert "t3 setup" in result.output, "the in-container check is the command that proves it"
        assert "teatree.env" in result.output, "the operator must be told there is no file to edit"
        assert self.store.writes == [(_ROUTED_KEY, _SECRET)], "the only write is the one pass entry"

    def test_an_unshared_page_still_gets_the_note_and_one_copy_of_the_error(self) -> None:
        # An operator with sharing work left is exactly the one who has not re-deployed yet.
        self.notion.fail_with = (404, "object_not_found")

        result = self.runner.invoke(notion_app, ["setup", "--page", self.notion.page_id], input=f"{_SECRET}\n")

        assert result.exit_code == 6, result.output
        assert "teatree.env" in result.output
        assert result.output.count("is not shared with this integration") == 1
