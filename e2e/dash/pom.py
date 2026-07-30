"""Page objects + support types for the /dash/ e2e lane.

Locators favour user-visible structure (roles, text, data-state) over brittle CSS
depth, per /t3:e2e conventions. Web-first ``expect`` assertions live in the specs;
these objects only locate. Kept out of ``conftest.py`` so the specs import them
once (no conftest double-import).
"""

from dataclasses import dataclass, field
from http import HTTPStatus

from playwright.sync_api import ConsoleMessage, Locator, Page, Request, Response

from teatree.core.models.ticket import Ticket


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

    def open(self) -> None:
        self.page.goto(f"{self.base_url}/dash/board/")

    @property
    def rail_nodes(self) -> Locator:
        return self.page.locator(".fsm-rail .fsm-node")

    @property
    def columns(self) -> Locator:
        return self.page.locator(".kanban-column")

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

    def open_drawer_for(self, ticket_id: int) -> "DrawerPanel":
        self.card_by_id(ticket_id).click()
        return DrawerPanel(self.page)


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
    def mermaid_svg(self) -> Locator:
        return self.page.locator("#drawer .mermaid svg")

    @property
    def history_rows(self) -> Locator:
        return self.page.locator("#drawer table.dash tbody tr")
