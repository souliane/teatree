"""Page objects + support types for the /dash/ e2e lane.

Locators favour user-visible structure (roles, text, data-state) over brittle CSS
depth, per /t3:e2e conventions. Web-first ``expect`` assertions live in the specs;
these objects only locate. Kept out of ``conftest.py`` so the specs import them
once (no conftest double-import).
"""

from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Download, Locator, Page, Request, Response

from teatree.core.models.ticket import Ticket
from teatree.hooks.term_match import line_matches


@dataclass(frozen=True, slots=True)
class SeededBoard:
    backlog: Ticket
    building: Ticket
    reviewing: Ticket
    landed: Ticket


@dataclass
class ConsoleGuard:
    errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    bad_responses: list[str] = field(default_factory=list)

    def on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.errors.append(message.text)

    def on_page_error(self, error: object) -> None:
        self.errors.append(str(error))

    def on_request_failed(self, request: Request) -> None:
        self.failed_requests.append(request.url)

    def on_response(self, response: Response) -> None:
        # A 404 for a static asset (missing font/css/js) is an HTTP response, not a
        # request failure, so it must be caught here. Redirects (3xx) are fine.
        if response.status >= HTTPStatus.BAD_REQUEST:
            self.bad_responses.append(f"{response.status} {response.url}")

    @property
    def is_clean(self) -> bool:
        return not (self.errors or self.failed_requests or self.bad_responses)

    @property
    def report(self) -> str:
        return f"console errors={self.errors} failed={self.failed_requests} bad_responses={self.bad_responses}"

    def raise_if_dirty(self) -> None:
        if not self.is_clean:
            raise AssertionError(self.report)


@dataclass(frozen=True, slots=True)
class BoardPage:
    page: Page
    base_url: str

    def open(self) -> Response | None:
        return self.page.goto(f"{self.base_url}/dash/board/lifecycle/")

    @property
    def rail_nodes(self) -> Locator:
        return self.page.locator(".fsm-rail .fsm-node")

    @property
    def columns(self) -> Locator:
        return self.page.locator(".kanban-column")

    def expand_column(self, state: str) -> None:
        self.page.locator(f'details[data-state="{state}"] summary').click()

    def column(self, state: str) -> Locator:
        return self.page.locator(f'.kanban-column[data-state="{state}"]')

    @property
    def cards(self) -> Locator:
        return self.page.locator(".card")

    def card(self, number: str) -> Locator:
        return self.page.locator(".card").filter(has_text=f"#{number}")

    def card_by_id(self, ticket_id: int) -> Locator:
        return self.page.locator(f'.card[data-ticket="{ticket_id}"]')

    def card_in_column(self, state: str, ticket_id: int) -> Locator:
        return self.column(state).locator(f'.card[data-ticket="{ticket_id}"]')

    def failure_chip_on(self, ticket_id: int) -> Locator:
        return self.card_by_id(ticket_id).locator(".chip.failure")

    def enqueue_buttons_on(self, ticket_id: int) -> Locator:
        return self.card_by_id(ticket_id).locator('form[action*="/enqueue/"] button')

    def open_drawer_for(self, ticket_id: int) -> "DrawerPanel":
        # The card's description, not the card: a card carries enqueue buttons, and a
        # centre-point click on the article itself can land on one of them.
        self.card_by_id(ticket_id).locator(".card-desc").click()
        return DrawerPanel(self.page)


@dataclass(frozen=True, slots=True)
class PrBoardPage:
    page: Page
    base_url: str

    def open(self) -> Response | None:
        return self.page.goto(f"{self.base_url}/dash/board/")

    def group(self, name: str) -> Locator:
        return self.page.locator(f'[data-pr-group="{name}"]')

    def card(self, ref: str) -> Locator:
        return self.page.locator(f'[data-pr-card="{ref}"]')


@dataclass(frozen=True, slots=True)
class DrawerPanel:
    page: Page

    @property
    def root(self) -> Locator:
        return self.page.locator("#drawer")

    @property
    def close_button(self) -> Locator:
        return self.page.locator("[data-drawer-close]")

    @property
    def transition_buttons(self) -> Locator:
        return self.page.locator('form[action*="/transition/"] button')

    @property
    def enqueue_buttons(self) -> Locator:
        # Scoped to the panel: every board card behind it carries the same form.
        return self.page.locator('#drawer form[action*="/enqueue/"] button')

    @property
    def mermaid_svg(self) -> Locator:
        return self.page.locator("#drawer .mermaid svg")

    @property
    def attempt_errors(self) -> Locator:
        return self.page.locator("#drawer .attempt-error")

    @property
    def history_rows(self) -> Locator:
        return self.page.locator("#drawer table.dash tbody tr")


@dataclass(frozen=True, slots=True)
class TermScan:
    """The placeholder terms a spec proves absent from what the browser received."""

    terms: tuple[str, ...]

    def raise_if_carried(self, text: str) -> None:
        if line_matches(text, self.terms):
            msg = "the page carries a placeholder banned term"
            raise AssertionError(msg)


@dataclass
class InterchangePage:
    page: Page
    base_url: str
    downloads: list[Download] = field(default_factory=list)

    def open(self) -> Response | None:
        self.page.on("download", self.on_download)
        return self.page.goto(f"{self.base_url}/dash/import-export/")

    def on_download(self, download: Download) -> None:
        self.downloads.append(download)

    def preview(self, upload: Path) -> None:
        self.page.get_by_label("TOML file to import").set_input_files(upload)
        self.page.get_by_role("button", name="Preview (dry-run)").click()

    def export(self) -> None:
        self.page.get_by_role("button", name="Export (secrets withheld)").click()

    def banner(self, text: str) -> Locator:
        return self.page.locator(".banner-red", has_text=text)

    def raise_if_downloaded(self) -> None:
        if self.downloads:
            msg = f"the export downloaded {len(self.downloads)} file(s)"
            raise AssertionError(msg)
