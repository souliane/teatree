"""The test plan's home: a file in the e2e repo, one per ticket.

Covers the four properties the file store owes its callers: the path is
derived (never configured) from what the overlay already declares, a second
write updates that one file in place, every run's evidence carries the
env/commit/timestamp triple, and an unresolvable location fails loud.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.management.commands._test_plan import file_store
from teatree.core.management.commands._test_plan.render import empty_state, merge_state, parse_manifest, render_body
from teatree.core.management.commands._test_plan.state import PlanState
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayMetadata
from tests.teatree_core.conftest import CommandOverlay

_ISSUE_URL = "https://gitlab.com/org/client/-/work_items/7311"
_E2E_REPO = "client-workspace"


class _E2eRepoMetadata(OverlayMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {
            "runner": "external",
            "project_path": f"org/{_E2E_REPO}",
            "url": f"git@gitlab.com:org/{_E2E_REPO}.git",
            "ref": "master",
            "e2e_dir": "e2e",
        }


class _NestedE2eMetadata(_E2eRepoMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {**super().get_e2e_config(), "e2e_dir": "tests/e2e"}


class _E2eRepoOverlay(CommandOverlay):
    metadata = _E2eRepoMetadata()

    def get_repos(self) -> list[str]:
        return ["product", _E2E_REPO]


class _NestedE2eOverlay(_E2eRepoOverlay):
    metadata = _NestedE2eMetadata()


class _NoE2eRepoOverlay(CommandOverlay):
    """An overlay that declares no e2e repo — the plan has nowhere to live."""


class _PlanFileTestBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.checkout = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ticket = Ticket.objects.create(issue_url=_ISSUE_URL, overlay="test")

    def _worktree(self, repo: str, path: Path | None) -> Worktree:
        return Worktree.objects.create(
            ticket=self.ticket,
            overlay="test",
            repo_path=repo,
            branch="7311-feat-thing",
            extra={"worktree_path": str(path)} if path is not None else {},
        )

    def _resolve(self, overlay: CommandOverlay | None = None) -> Path:
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": overlay or _E2eRepoOverlay()},
        ):
            return file_store.plan_path_for_ticket(self.ticket)


class TestPlanPath(_PlanFileTestBase):
    """The path is derived from the overlay's declared e2e repo + the ticket number."""

    def test_lands_in_test_plans_beside_e2e_named_after_the_gitlab_ticket(self) -> None:
        self._worktree(_E2E_REPO, self.checkout)
        assert self._resolve() == self.checkout / "test-plans" / "client-7311.md"

    def test_ignores_worktrees_of_other_repos(self) -> None:
        self._worktree("product", self.checkout / "backend")
        self._worktree(_E2E_REPO, self.checkout)
        assert self._resolve() == self.checkout / "test-plans" / "client-7311.md"

    def test_nested_e2e_dir_puts_test_plans_beside_it(self) -> None:
        self._worktree(_E2E_REPO, self.checkout)
        assert self._resolve(_NestedE2eOverlay()) == self.checkout / "tests" / "test-plans" / "client-7311.md"


class TestPlanPathFailsLoud(_PlanFileTestBase):
    """An unresolvable location is an error, never a silently-skipped write."""

    def test_no_worktree_for_the_e2e_repo(self) -> None:
        with pytest.raises(file_store.TestPlanLocationError, match=_E2E_REPO):
            self._resolve()

    def test_overlay_declares_no_e2e_repo(self) -> None:
        with pytest.raises(file_store.TestPlanLocationError, match="e2e repo"):
            self._resolve(_NoE2eRepoOverlay())

    def test_worktree_row_carries_no_on_disk_path(self) -> None:
        self._worktree(_E2E_REPO, None)
        with pytest.raises(file_store.TestPlanLocationError, match="not provisioned"):
            self._resolve()


def _local_state(*, ran_at: str = "2026-08-13T09:00:00Z") -> PlanState:
    state = empty_state(ticket="7311", title="My feature")
    state["local"] = {"commits": {"client": "aabb"}, "workflows": {}, "env": "local", "ran_at": ran_at}
    return state


class TestReadWriteRoundTrip(TestCase):
    """The file is the record: what is written is what the next run recovers."""

    def setUp(self) -> None:
        super().setUp()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "test-plans" / "client-7311.md"

    def test_write_creates_the_file_and_its_parent_directory(self) -> None:
        file_store.write_plan(self.path, render_body(_local_state()))

        assert self.path.is_file()
        assert "## Test Plan — My feature" in self.path.read_text(encoding="utf-8")

    def test_read_recovers_the_state_written(self) -> None:
        file_store.write_plan(self.path, render_body(_local_state()))

        recovered = file_store.read_plan_state(self.path)

        assert recovered["ticket"] == "7311"
        assert recovered["local"]["commits"] == {"client": "aabb"}
        assert recovered["local"]["ran_at"] == "2026-08-13T09:00:00Z"

    def test_read_of_an_absent_file_is_an_empty_state(self) -> None:
        recovered = file_store.read_plan_state(self.path)

        assert recovered["ticket"] == ""
        assert recovered["local"]["workflows"] == {}


class TestSecondRunUpdatesInPlace(TestCase):
    """A dev run after a local run edits the one file — it never appends a second copy."""

    @staticmethod
    def _manifest(env: str, sha: str, ran_at: str) -> str:
        return json.dumps(
            {
                "ticket": "7311",
                "mrs": ["https://gitlab.com/org/client/-/merge_requests/6331"],
                env: {"commits": {"client": sha}, "ran_at": ran_at},
                "workflows": [{"workflow": "Login", "steps": ["Open the app"]}],
            }
        )

    def test_one_file_carrying_both_sides_and_one_heading(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / "test-plans" / "client-7311.md"

        for env, sha, ran_at in (("local", "aabb", "2026-08-13T09:00:00Z"), ("dev", "ccdd", "2026-08-14T11:30:00Z")):
            prior = file_store.read_plan_state(path)
            manifest = parse_manifest(self._manifest(env, sha, ran_at))
            state = merge_state(prior, manifest=manifest, title="My feature", embeds={"dev": {}, "local": {}})
            state["ticket"] = "7311"
            file_store.write_plan(path, render_body(state))

        body = path.read_text(encoding="utf-8")
        assert body.count("## Test Plan — My feature") == 1
        assert list((root / "test-plans").iterdir()) == [path]
        assert "aabb" in body
        assert "ccdd" in body


class TestLegacyUnprefixedPlanIsUpdatedInPlace(_PlanFileTestBase):
    """A plan written before the repo prefix is rewritten, never forked into a second file."""

    def setUp(self) -> None:
        super().setUp()
        self._worktree(_E2E_REPO, self.checkout)
        self.plan_dir = self.checkout / "test-plans"
        self.legacy = self.plan_dir / f"{self.ticket.ticket_number}.md"

    def test_resolves_to_the_existing_unprefixed_file(self) -> None:
        file_store.write_plan(self.legacy, render_body(_local_state()))

        assert self._resolve() == self.legacy

    def test_the_side_the_legacy_file_recorded_survives_a_rerun(self) -> None:
        file_store.write_plan(self.legacy, render_body(_local_state()))

        recovered = file_store.read_plan_state(self._resolve())

        assert recovered["local"]["commits"] == {"client": "aabb"}
        assert list(self.plan_dir.iterdir()) == [self.legacy]

    def test_the_prefixed_name_wins_once_that_file_exists(self) -> None:
        file_store.write_plan(self.legacy, render_body(_local_state()))
        prefixed = self.plan_dir / "client-7311.md"
        file_store.write_plan(prefixed, render_body(_local_state()))

        assert self._resolve() == prefixed


class TestEvidenceTriple(TestCase):
    """Every run's evidence carries where it ran, which commit, and when."""

    _MANIFEST = json.dumps(
        {
            "ticket": "7311",
            "local": {"commits": {"client": "aabb"}, "ran_at": "2026-08-13T09:00:00Z"},
            "workflows": [{"workflow": "Login", "steps": ["Open the app"]}],
        }
    )

    def _merged(self, manifest_json: str = _MANIFEST) -> PlanState:
        return merge_state(
            empty_state(ticket="7311", title="t"),
            manifest=parse_manifest(manifest_json),
            title="t",
            embeds={"dev": {}, "local": {}},
        )

    def test_env_commit_and_timestamp_are_fields_on_the_recovered_state(self) -> None:
        state = self._merged()

        assert state["local"]["env"] == "local"
        assert state["local"]["commits"] == {"client": "aabb"}
        assert state["local"]["ran_at"] == "2026-08-13T09:00:00Z"

    def test_a_run_that_names_no_timestamp_leaves_the_prior_date_standing(self) -> None:
        """An unstamped run records no instant, so the merge keeps the side's prior date.

        Stamping here would date the capture at parse time — a moment nothing was
        captured at — and would mean a later one-sided run always overwrote the
        other side's genuine date.
        """
        manifest = parse_manifest(
            json.dumps(
                {
                    "ticket": "7311",
                    "local": {"commits": {"client": "aabb"}},
                    "workflows": [{"workflow": "Login", "steps": ["Open the app"]}],
                }
            )
        )

        assert manifest.local.ran_at == ""

    def test_the_rendered_body_states_the_triple(self) -> None:
        state = self._merged()
        state["ticket"] = "7311"

        body = render_body(state)

        assert "Local tested" in body
        assert "aabb" in body
        assert "2026-08-13T09:00:00Z" in body
