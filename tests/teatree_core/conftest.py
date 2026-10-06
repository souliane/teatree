"""Shared fixtures for teatree.core test modules."""

import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.backend_protocols import DraftState, PrMergeState
from teatree.core.gates.merge_evidence_gate import record_confirmed_forge_merge
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.models import PullRequest, ReviewEvidence, Ticket, Worktree
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.repro_evidence import HarnessRun, ReproEvidence
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.overlay import OverlayBase, OverlayE2E, OverlayReview, OverlayRuntime, ProvisionStep, RunCommands
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from tests._git_repo import make_git_repo, run_git, run_git_captured
from tests.db_alias import RouteAllToAlias, register_migrated_sqlite_alias, teardown_sqlite_alias


def seed_merge_safe_verdict(
    *,
    slug: str,
    pr_id: int,
    sha: str,
    reviewer: str = "cold-reviewer",
) -> ReviewVerdict:
    """Record the non-author MERGE_SAFE verdict the #2829 merge-verdict gate requires.

    ``execute_bound_merge`` now refuses any merge that lacks a non-stale
    independent ``merge_safe`` :class:`ReviewVerdict` at the live head. The
    production ``t3 <overlay> ticket clear`` path records exactly this verdict
    as a by-product of issuing the CLEAR; tests that build the CLEAR via
    ``MergeClear.issue`` / ``.objects.create`` (bypassing that command) seed it
    here so they still exercise the merge. Seeding is NOT a weakening — it
    reproduces what the real clear path records, with a non-author reviewer.
    """
    return ReviewVerdict.record(
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=sha,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity=reviewer,
    )


def record_maker_review_for_test(ticket: Ticket, head_sha: str) -> None:
    """Record the maker proof that precedes review and merge in unrelated tests.

    These fixtures add no regression tests to the production diff they model.
    Gate-specific refusal tests leave this evidence absent or stale themselves.
    """
    ticket.record_anti_vacuity_attestation(
        head_sha,
        "Acceptance criteria checked against the reviewed test fixture diff",
        [],
        no_new_tests=True,
    )


def record_merge_prerequisites_for_test(
    ticket: Ticket | None, head_sha: str, *, slug: str = "", pr_id: int = 0
) -> None:
    """Record maker and critic artifacts on the PR's owning ticket when one resolves."""
    if ticket is None:
        ticket = resolve_gated_ticket(slug=slug, pr_id=pr_id) if slug and pr_id else None
        if ticket is None:
            return
    record_maker_review_for_test(ticket, head_sha)
    CriticVerdict.record_from_envelope(
        ticket=ticket,
        transition="merge",
        head_sha=head_sha,
        envelope={
            "grader_identity": "fixture-independent-critic",
            "items": [
                {
                    "slug": slug,
                    "status": "pass",
                    "citation": "Reviewed fixture diff and acceptance criteria",
                }
                for slug in ("test_value", "cleanliness")
            ],
        },
    )


def record_review_context_for_test(ticket: Ticket) -> None:
    """Use the real evidence writer for a completed review of fixture documents."""
    ticket.record_review_context(
        ticket.issue_url or "https://example.test/work-item/1",
        ["https://example.test/specification/1"],
        "Checked the fixture's requirements and referenced document against the diff",
    )


def record_review_request_prerequisites_for_test(head_sha: str) -> Ticket:
    """Record the maker and independent cold-review evidence required before posting."""
    ticket = Ticket.objects.create(state=Ticket.State.SELF_REVIEWED)
    record_maker_review_for_test(ticket, head_sha)
    ReviewEvidence.record(
        ticket=ticket,
        kind=ReviewEvidence.Kind.COLD_REVIEW,
        reviewer_identity="fixture-independent-reviewer",
        verdict="merge_safe",
        head_sha=head_sha,
    )
    return ticket


def record_confirmed_merge_for_test(ticket: Ticket) -> None:
    """Persist a forge-confirmed SHA with the production merge-evidence recorder.

    The fixture supplies only the external forge observation. The recorder writes
    the ticket's durable evidence and settles its PR row before the FSM advances.
    """
    if not PullRequest.objects.filter(ticket=ticket).exists():
        pr_id = 100_000 + int(ticket.pk)
        PullRequest.objects.create(
            ticket=ticket,
            overlay=ticket.overlay,
            url=f"https://github.com/souliane/teatree/pull/{pr_id}",
            repo="souliane/teatree",
            iid=str(pr_id),
        )
    with patch(
        "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state",
        return_value=PrMergeState(state="MERGED", merge_commit_oid="f" * 40),
    ):
        assert record_confirmed_forge_merge(ticket)


def record_owned_pr_for_test(*, slug: str, pr_id: int, head_sha: str, host_kind: str = "github") -> Ticket:
    """Give a ticketless CLEAR the PR ledger owner every factory PR has in production."""
    from tests.factories import waive_rubric  # noqa: PLC0415 — test factory imports the app registry

    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
    waive_rubric(ticket)
    record_merge_prerequisites_for_test(ticket, head_sha)
    url = (
        f"https://gitlab.com/{slug}/-/merge_requests/{pr_id}"
        if host_kind == "gitlab"
        else f"https://github.com/{slug}/pull/{pr_id}"
    )
    PullRequest.objects.create(ticket=ticket, overlay=ticket.overlay, url=url, repo=slug, iid=str(pr_id))
    return ticket


@contextmanager
def ready_review_batch_for_test(mr_url: str) -> Iterator[None]:
    """Supply a forge listing whose only work-group member has passed CI.

    The batch gate and its readiness checks run unchanged; only the external
    forge observations are scripted for tests of downstream posting behavior.
    """

    class _ReadyHost:
        def current_user(self) -> str:
            return "fixture-owner"

        def list_my_prs(self, *, author: str) -> list[dict[str, object]]:
            assert author == "fixture-owner"
            return [{"web_url": mr_url, "title": "Ready fixture MR", "head_pipeline": {"status": "success"}}]

        def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
            _ = (slug, pr_id)
            return DraftState.NOT_DRAFT

    host = _ReadyHost()
    with (
        patch("teatree.core.backend_factory.code_host_from_overlay", return_value=host),
        patch("teatree.core.gates.review_request_batch_gate.code_host_from_overlay", return_value=host),
    ):
        yield


def record_executed_repro_for_test(ticket: Ticket) -> None:
    """Execute a RED command and its GREEN replay on descendant fixture commits."""
    command = "test -f fixed.flag"
    # The POSIX shell every host has; zsh is absent from the CI image and the deploy image.
    shell = shutil.which("sh") or "/bin/sh"
    with tempfile.TemporaryDirectory(prefix="teatree-repro-fixture-") as temp_dir:
        repo = make_git_repo(Path(temp_dir) / "repro")
        red_sha = run_git(repo, "rev-parse", "HEAD")
        red = subprocess.run([shell, "-c", command], cwd=repo, capture_output=True, text=True, check=False)
        assert red.returncode != 0
        ReproEvidence.record_red(
            ticket=ticket,
            command=command,
            run=HarnessRun(head_sha=red_sha, exit_code=red.returncode, output=red.stdout + red.stderr),
        )

        (repo / "fixed.flag").write_text("fixed\n", encoding="utf-8")
        run_git(repo, "add", "fixed.flag")
        run_git(repo, "commit", "-q", "-m", "satisfy repro")
        green_sha = run_git(repo, "rev-parse", "HEAD")
        green = subprocess.run([shell, "-c", command], cwd=repo, capture_output=True, text=True, check=False)
        ancestor = run_git_captured(repo, "merge-base", "--is-ancestor", red_sha, green_sha)
        ReproEvidence.record_green(
            ticket=ticket,
            command=command,
            run=HarnessRun(head_sha=green_sha, exit_code=green.returncode, output=green.stdout + green.stderr),
            red_is_ancestor=ancestor.returncode == 0,
        )


pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class _CommandReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        # Test double with no customer surface — the mandatory-E2E gate (#1967)
        # is inert here (matches the dogfood overlay's posture).
        _ = changed_files
        return False


class _CommandRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["run-backend", worktree.repo_path],
            "frontend": ["run-frontend", worktree.repo_path],
        }

    def pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def remember_pre_run() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra[f"pre_run_{service}"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"pre-run-{service}", callable=remember_pre_run)]


class _CommandE2E(OverlayE2E):
    def env_extras(self, env_cache: dict[str, str], **_: object) -> dict[str, str]:
        variant = env_cache.get("WT_VARIANT", "")
        return {"CUSTOMER": variant} if variant else {}


class CommandOverlay(OverlayBase):
    """Minimal overlay for management command tests."""

    review = _CommandReview()
    # Annotated with the BASE type (as OverlayBase does) so a subclass may supply
    # its own runtime; a bare assignment pins the attribute to this one class.
    runtime: OverlayRuntime = _CommandRuntime()
    e2e = _CommandE2E()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def remember_setup() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra["setup_hook"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="remember-setup", callable=remember_setup)]


COMMAND_OVERLAY = "tests.teatree_core.conftest.CommandOverlay"
IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
            "QUEUES": ["default", "loops", "cheap"],
        },
    },
}
HEALTHY_MACHINE_SIGNAL = MachineSignal(cores=8, load1=0.25, ram_available_gb=20.0)
HEALTHY_QUOTA_SIGNAL = QuotaSignal(
    fresh=True,
    all_accounts_exhausted=False,
    weekly_utilization=0.1,
    short_utilization=0.1,
    seconds_to_weekly_reset=7 * 24 * 3600 * 0.5,
)


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


@pytest.fixture(autouse=True)
def _routed_merge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the merge transport off the host's ``pass`` store: every test stubs the gh runner anyway."""
    monkeypatch.setattr(
        "teatree.core.merge.ci_rollup.resolve_slug_token",
        lambda *_args, **_kwargs: ForgeTokenResolution(
            "github_token", "t3-teatree", ForgeTokenState.TOKEN, token="routed-test-token"
        ),
    )


@pytest.fixture
def mock_command_overlay() -> Iterator[None]:
    """Patch _discover_overlays to return a CommandOverlay instance."""
    with patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={"test": CommandOverlay()},
    ):
        yield


@dataclass(frozen=True)
class SchemaGuardAlias:
    """Factory for private, file-backed SQLite connections used by schema_guard tests (#2915)."""

    register_current: Callable[[], str]
    make_stale: Callable[[], str]


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard for a test that never touches ``default``."""
    with django_db_blocker.unblock():
        yield


@pytest.fixture
def schema_guard_alias(
    tmp_path: Path, _unblocked_db: None, migrated_db_template: Path | None
) -> Iterator[SchemaGuardAlias]:
    """Private, throwaway SQLite connections for schema_guard tests (#2915).

    Every alias this factory creates is registered against its own file under
    ``tmp_path`` and torn down automatically — a crashed reverse-migrate/
    restore cycle can corrupt only that one throwaway file, never the shared,
    xdist-worker-lifetime-reused ``default`` test database every other test in
    the worker relies on.

    The ``RouteAllToAlias`` router is installed for the alias's whole
    remaining lifetime, not just this factory's own migrate calls: the
    schema-guard functions under test (``migrate_self_db``,
    ``require_current_schema``) run their own ``migrate --database=<alias>``
    internally, and without the router active for *those* calls too, their
    RunPython seed/backfill operations resolve back onto the shared
    ``default`` connection they were built to avoid.
    """
    stack = ExitStack()
    created: list[str] = []

    def _register_current() -> str:
        """Register a private, file-backed SQLite connection at HEAD.

        The DB is the product of a real ``migrate`` from zero (not a hand-rolled
        table), so the full app graph is current — the schema-guard functions under
        test read the migration ledger via Django's own ``MigrationExecutor``, which
        needs every app's history. It is a copy of the session's migrated template
        when there is one, so no test pays that from-zero migrate again.
        """
        alias = f"sg_{uuid.uuid4().hex}"
        register_migrated_sqlite_alias(alias, tmp_path / f"{alias}.sqlite3", migrated_db_template)
        stack.enter_context(override_settings(DATABASE_ROUTERS=[RouteAllToAlias(alias)]))
        created.append(alias)
        return alias

    def _make_stale() -> str:
        """A private alias migrated to HEAD, then reverse-migrated ``core`` to ``zero``."""
        alias = _register_current()
        call_command("migrate", "core", "zero", "--no-input", database=alias, verbosity=0)
        return alias

    with stack:
        yield SchemaGuardAlias(register_current=_register_current, make_stale=_make_stale)

    for alias in created:
        teardown_sqlite_alias(alias)
