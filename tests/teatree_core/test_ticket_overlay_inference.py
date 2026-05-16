"""Overlay attribution for tickets must follow the overlay's own repo list.

``Ticket._infer_overlay`` previously read ``config.workspace_repos``
directly. Overlays that compute their workspace repos dynamically (the
GitLab overlay derives them from ``get_repos()``) leave that attribute
empty, so inference could never attribute a ticket to them — every such
ticket leaked into the first overlay whose static slug happened to match
(or none, ending up mis-tagged). The fix routes inference through
``overlay.get_workspace_repos()`` and adds a reconcile path so rows
already persisted with the wrong overlay can be corrected.
"""

from contextlib import AbstractContextManager
from typing import ClassVar, cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket

type _Reattribute = dict[str, object]


class _DynamicRepoOverlay:
    """An overlay whose repos come from the method, not config.workspace_repos."""

    def __init__(self, repos: list[str]) -> None:
        self._repos = repos

        class _Cfg:
            workspace_repos: ClassVar[list[str]] = []

        self.config = _Cfg()

    def get_workspace_repos(self) -> list[str]:
        return self._repos


class TestInferOverlayUsesGetWorkspaceRepos(TestCase):
    """Inference must consult ``get_workspace_repos()``, not the raw attribute."""

    def test_matches_overlay_whose_repos_are_method_computed(self) -> None:
        ticket = Ticket(issue_url="https://gitlab.com/acme/widgets/-/issues/42")
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"gitlab-overlay": overlay},
        ):
            assert ticket._infer_overlay() == "gitlab-overlay"

    def test_no_match_returns_empty(self) -> None:
        ticket = Ticket(issue_url="https://gitlab.com/acme/widgets/-/issues/42")
        overlay = _DynamicRepoOverlay(["other/repo"])
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"x": overlay},
        ):
            assert ticket._infer_overlay() == ""

    def test_overlay_without_get_workspace_repos_is_skipped(self) -> None:
        """A registered entry that is not a full overlay must not crash inference."""

        class _Bare:
            config = None

        ticket = Ticket(issue_url="https://github.com/example/widgets/issues/3")
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"bare": _Bare()},
        ):
            assert ticket._infer_overlay() == ""

    def test_method_raising_does_not_break_other_overlays(self) -> None:
        class _Broken:
            config = None

            def get_workspace_repos(self) -> list[str]:
                msg = "overlay config unavailable"
                raise RuntimeError(msg)

        good = _DynamicRepoOverlay(["acme/widgets"])
        ticket = Ticket(issue_url="https://gitlab.com/acme/widgets/-/issues/42")
        with patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"broken": _Broken(), "good": good},
        ):
            assert ticket._infer_overlay() == "good"


class TestReconcileOverlay(TestCase):
    """``Ticket.reconcile_overlay`` re-infers and persists a corrected overlay."""

    def _patch_overlays(self, overlay: object) -> AbstractContextManager[object]:
        return patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"gitlab-overlay": overlay},
        )

    def test_corrects_a_mis_attributed_row(self) -> None:
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with self._patch_overlays(overlay):
            ticket = Ticket.objects.create(
                overlay="wrong-overlay",
                issue_url="https://gitlab.com/acme/widgets/-/issues/42",
            )
            changed = ticket.reconcile_overlay()
        assert changed is True
        assert ticket.overlay == "gitlab-overlay"
        ticket.refresh_from_db()
        assert ticket.overlay == "gitlab-overlay"

    def test_leaves_correct_row_untouched(self) -> None:
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with self._patch_overlays(overlay):
            ticket = Ticket.objects.create(
                overlay="gitlab-overlay",
                issue_url="https://gitlab.com/acme/widgets/-/issues/42",
            )
            changed = ticket.reconcile_overlay()
        assert changed is False
        assert ticket.overlay == "gitlab-overlay"

    def test_keeps_existing_overlay_when_inference_is_inconclusive(self) -> None:
        """An empty inference must not blank out a row's existing overlay."""
        overlay = _DynamicRepoOverlay(["unrelated/repo"])
        with self._patch_overlays(overlay):
            ticket = Ticket.objects.create(
                overlay="manually-set",
                issue_url="https://gitlab.com/acme/widgets/-/issues/42",
            )
            changed = ticket.reconcile_overlay()
        assert changed is False
        assert ticket.overlay == "manually-set"

    def test_no_issue_url_is_a_noop(self) -> None:
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with self._patch_overlays(overlay):
            ticket = Ticket.objects.create(overlay="manual", issue_url="")
            changed = ticket.reconcile_overlay()
        assert changed is False
        assert ticket.overlay == "manual"


class TestReconcileOverlayCommand(TestCase):
    """``manage.py ticket reconcile-overlay`` backfills mis-attributed rows."""

    def _patch(self, overlay: object) -> AbstractContextManager[object]:
        return patch(
            "teatree.core.overlay_loader.get_all_overlays",
            return_value={"gitlab-overlay": overlay},
        )

    def test_dry_run_reports_without_persisting(self) -> None:
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with self._patch(overlay):
            wrong = Ticket.objects.create(
                overlay="wrong",
                issue_url="https://gitlab.com/acme/widgets/-/issues/1",
            )
            results = cast("list[_Reattribute]", call_command("ticket", "reconcile-overlay", "--dry-run"))
        assert any(r["ticket_id"] == wrong.pk and r["action"] == "would_reattribute" for r in results)
        wrong.refresh_from_db()
        assert wrong.overlay == "wrong"

    def test_reports_nothing_when_all_rows_already_consistent(self) -> None:
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with self._patch(overlay):
            Ticket.objects.create(
                overlay="gitlab-overlay",
                issue_url="https://gitlab.com/acme/widgets/-/issues/9",
            )
            results = cast("list[_Reattribute]", call_command("ticket", "reconcile-overlay"))
        assert results == []

    def test_persists_corrected_overlay(self) -> None:
        overlay = _DynamicRepoOverlay(["acme/widgets"])
        with self._patch(overlay):
            wrong = Ticket.objects.create(
                overlay="wrong",
                issue_url="https://gitlab.com/acme/widgets/-/issues/1",
            )
            right = Ticket.objects.create(
                overlay="gitlab-overlay",
                issue_url="https://gitlab.com/acme/widgets/-/issues/2",
            )
            results = cast("list[_Reattribute]", call_command("ticket", "reconcile-overlay"))
        wrong.refresh_from_db()
        right.refresh_from_db()
        assert wrong.overlay == "gitlab-overlay"
        assert right.overlay == "gitlab-overlay"
        reattributed = [r for r in results if r["action"] == "reattributed"]
        assert [r["ticket_id"] for r in reattributed] == [wrong.pk]
