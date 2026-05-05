"""E2E coverage for dashboard flows: modal, history endpoint, SSE, launches.

The pre-existing flow coverage in ``test_dashboard.py`` was either
gated on ``if visible`` (effectively a no-op) or stopped at the click
boundary without asserting downstream state. These tests close those
gaps. See [#19](https://github.com/souliane/teatree/issues/19).
"""

import json
from urllib.parse import quote

import httpx
from playwright.sync_api import Page, Route, expect


def test_task_detail_modal_opens(e2e_server: str, page: Page, django_db_blocker) -> None:
    """Click a Task button in the unified-sessions panel; modal populates with task fields.

    Replaces ``test_task_detail_modal`` which was gated on
    ``if task_link.is_visible():`` and therefore became a silent no-op.
    """
    from teatree.core.models import Task

    page.goto(e2e_server)
    # Wait for the unified sessions HTMX panel to load (renders Task buttons).
    page.locator("#unified-sessions-grid").wait_for(state="visible")

    with django_db_blocker.unblock():
        task = Task.objects.filter(execution_target="interactive").first()
    assert task is not None, "seed should provide an interactive task"

    page.get_by_role("button", name=f"Task {task.pk}").first.click()

    modal_body = page.locator("#task-modal-body")
    expect(modal_body).to_be_visible()
    # Task fields rendered in ``task_detail_popup.html``: task_id, status,
    # execution_target, execution_reason. We verify the seed-specific
    # reason ("Needs manual verification") to prove the right task loaded.
    expect(modal_body).to_contain_text(f"Task {task.pk}")
    expect(modal_body).to_contain_text("Needs manual verification")
    expect(modal_body).to_contain_text("Interactive")


def test_session_history_endpoint(e2e_server: str) -> None:
    """``/sessions/<id>/history/?cwd=<path>`` returns 200 and a rendered partial.

    Direct-hit test (no Playwright) — the view doesn't require an HTMX
    header; it just renders the transcript template. Missing/invalid
    ``cwd`` gives 404 (regex guard).
    """
    cwd = quote("/tmp/demo-backend", safe="")
    # Use a session_id short enough to survive the template's
    # ``truncatechars:14`` filter on "Session " + id.
    session_id = "sess-42"
    response = httpx.get(
        f"{e2e_server}/sessions/{session_id}/history/?cwd={cwd}",
        timeout=5.0,
    )
    assert response.status_code == 200, response.text[:200]  # noqa: PLR2004
    body = response.text
    assert session_id in body
    # No transcript file exists for this synthetic session — the template
    # branches to the empty-state message.
    assert "No messages found." in body

    # Sanity: missing ``cwd`` 404s — guards against regressions in the
    # path validator.
    bad = httpx.get(f"{e2e_server}/sessions/abc-1234-session/history/", timeout=5.0)
    assert bad.status_code == 404  # noqa: PLR2004


def test_sse_connection(e2e_server: str) -> None:
    """SSE endpoint streams an initial ``connected`` event on subscribe.

    ``DashboardSSEView`` always emits ``event: connected`` as its first
    frame so HTMX clients can confirm the channel is alive. We read raw
    bytes with httpx and assert the framing is well-formed within a
    timeout. Cancelled by closing the response — the async generator
    catches ``CancelledError`` and returns cleanly.
    """
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    with httpx.stream("GET", f"{e2e_server}/dashboard/events/", timeout=timeout) as response:
        assert response.status_code == 200  # noqa: PLR2004
        assert "text/event-stream" in response.headers["content-type"]
        # Read just enough to confirm the first event arrived.
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            if "event: connected" in buffer:
                break
        assert "event: connected" in buffer, f"first chunk: {buffer[:200]}"
        assert '"status": "ok"' in buffer


def test_launch_terminal_flow(e2e_server: str, page: Page) -> None:
    """Clicking the Terminal button POSTs to ``/dashboard/launch-terminal/``.

    We never want to actually spawn ttyd in CI. Intercept the request via
    ``page.route`` and return a deterministic ``launch_url`` payload, then
    verify the dashboard's ``handleActionResponse`` handler tries to open
    that URL via ``window.open`` (which we shim to record the target).
    """
    fake_url = "http://terminal.example.test/abc"
    captured: dict[str, str] = {}

    def handle_launch(route: Route) -> None:
        captured["request"] = route.request.url
        route.fulfill(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"launch_url": fake_url}),
        )

    page.route("**/dashboard/launch-terminal/", handle_launch)
    page.goto(e2e_server)
    # Capture window.open calls — handleActionResponse opens the URL in a
    # new tab on success. Returning ``null`` keeps Playwright from waiting
    # on a popup that we don't actually need.
    page.add_init_script(
        "window.__opens = []; window.open = (u, t) => { window.__opens.push(u); return null; };",
    )
    page.reload()  # re-run init script after route is set
    page.locator("button.split-main", has_text="Terminal").first.click()
    # Wait for the captured POST to land.
    page.wait_for_function("window.__opens && window.__opens.length > 0", timeout=5000)
    opens = page.evaluate("window.__opens")
    assert fake_url in opens, f"expected window.open({fake_url!r}); got {opens!r}"
    assert "/dashboard/launch-terminal/" in captured.get("request", "")


def test_launch_agent_headless(e2e_server: str, page: Page, django_db_blocker) -> None:
    """Clicking Headless creates a Task and a TaskAttempt under ImmediateBackend.

    Extends the original ``test_create_headless_task`` (which only
    asserted that the panel re-rendered) with downstream assertions:
    the new Task exists in the DB and has at least one TaskAttempt
    (because ``e2e.settings`` configures ImmediateBackend).
    """
    from teatree.core.models import Task, TaskAttempt

    with django_db_blocker.unblock():
        before_tasks = Task.objects.filter(execution_target="headless").count()
        before_attempts = TaskAttempt.objects.count()

    page.goto(e2e_server)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("button", has_text="Headless").first.click()
    # Give the synchronous task backend time to record the attempt.
    page.wait_for_timeout(1500)

    with django_db_blocker.unblock():
        after_tasks = Task.objects.filter(execution_target="headless").count()
        after_attempts = TaskAttempt.objects.count()

    assert after_tasks > before_tasks, "click should have created a new headless Task"
    assert after_attempts > before_attempts, "ImmediateBackend should have recorded a TaskAttempt for the new task"
