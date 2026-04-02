"""E2E test fixtures — start a live Django dev server for Playwright.

Supports parallel execution via pytest-xdist: each worker gets its own SQLite
DB and Django dev server, so tests run with zero shared state.

Run with:
    t3 teatree run e2e-local
"""

import os
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "e2e.settings")
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Exempt fixture setup/teardown from pytest-timeout for E2E tests.

    Under xdist each worker starts its own Playwright browser + Django server
    inside session-scoped fixtures.  The global ``timeout = 10`` (from
    pyproject.toml) would kill setup before Playwright finishes launching.
    Adding ``func_only=True`` keeps the timeout on the test body only.
    """
    mark = pytest.mark.timeout(func_only=True)
    for item in items:
        item.add_marker(mark)


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    """Prevent pytest-django from renaming the DB to a test_ prefix."""


@pytest.fixture(scope="session")
def django_db_setup(django_db_modify_db_settings, django_db_blocker):
    """Run migrations against the per-worker E2E SQLite file.

    Deletes any stale DB file first so reused TEATREE_E2E_DB_DIR dirs
    (from killed processes or inherited env vars) start clean.
    """
    from e2e.settings import E2E_DB_PATH

    E2E_DB_PATH.unlink(missing_ok=True)

    with django_db_blocker.unblock():
        import django

        django.setup()

        from django.core.management import call_command

        call_command("migrate", "--run-syncdb", "--no-input", verbosity=0)


@pytest.fixture(scope="session")
def e2e_server(django_db_setup) -> Iterator[str]:
    """Start Django dev server on a free port, yield URL."""
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "django",
            "runserver",
            "--noreload",
            "--settings",
            "e2e.settings",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    _wait_for_server(url)
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _seed_data(e2e_server: str, django_db_blocker) -> Iterator[None]:
    """Seed DB before each test, flush after."""
    from teatree.core.models import Session, Task, Ticket, Worktree

    with django_db_blocker.unblock():
        # Ticket with MR data
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/42",
            variant="demo",
            state="started",
            extra={
                "issue_title": "Fix the login bug",
                "labels": ["bug", "priority::high"],
                "mrs": {
                    "backend!100": {
                        "url": "https://gitlab.example.com/org/backend/-/merge_requests/100",
                        "repo": "backend",
                        "iid": 100,
                        "title": "fix(auth): resolve login timeout",
                        "draft": False,
                        "pipeline_status": "success",
                        "pipeline_url": "https://gitlab.example.com/org/backend/-/pipelines/999",
                        "approvals": {"count": 1, "required": 1},
                    },
                    "frontend!200": {
                        "url": "https://gitlab.example.com/org/frontend/-/merge_requests/200",
                        "repo": "frontend",
                        "iid": 200,
                        "title": "fix(auth): update login form",
                        "draft": True,
                        "pipeline_status": "failed",
                        "pipeline_url": "https://gitlab.example.com/org/frontend/-/pipelines/998",
                        "approvals": {"count": 0, "required": 1},
                    },
                },
            },
        )
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/demo-backend",
            branch="feat-42",
            state="provisioned",
            ports={"backend": 8001, "frontend": 4201, "postgres": 5433},
            db_name="wt_42_demo",
        )
        session = Session.objects.create(ticket=ticket, agent_id="e2e-agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target="headless",
            execution_reason="Automated code review",
            phase="reviewing",
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target="interactive",
            execution_reason="Needs manual verification",
            phase="testing",
        )

        # Ticket without MRs (covers {% empty %} branch)
        ticket2 = Ticket.objects.create(
            issue_url="https://example.com/issues/99",
            variant="acme",
            state="scoped",
        )
        Session.objects.create(ticket=ticket2, agent_id="e2e-agent-2")

    yield

    with django_db_blocker.unblock():
        Ticket.objects.all().delete()


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, *, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.3)
    msg = f"Server at {url} did not start within {timeout}s"
    raise TimeoutError(msg)
