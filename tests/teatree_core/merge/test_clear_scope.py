"""``clear_scope_predicate`` — which overlay owns a ``MergeClear`` (#4250).

``MergeClear.ticket`` is nullable and null is the production NORM (599 of 622 rows, and
87 of 87 unconsumed ones, when the ticket was filed), so a ``ticket__overlay`` join
matched nothing and every overlay-scoped merge reader went blind. The predicate here is
the one answer all of them share.
"""

from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.merge.clear_scope import clear_scope_predicate, overlay_repo_slugs
from tests.factories import MergeClearFactory, TicketFactory

OVERLAY = "t3-teatree"
SLUG = "souliane/teatree"


class TestOverlayRepoSlugs(django.test.TestCase):
    def test_it_resolves_the_registered_overlays_declared_repos(self) -> None:
        assert SLUG in overlay_repo_slugs(OVERLAY)

    def test_a_bare_directory_token_is_not_matched_by_stripping_the_other_side(self) -> None:
        # The teatree overlay declares ``teatree`` as a workspace repo. A bare token is
        # not an owner/repo identity, so it canonicalizes to nothing rather than being
        # matched against ``souliane/teatree`` by dropping that slug's owner.
        assert "teatree" not in overlay_repo_slugs(OVERLAY)

    def test_an_unregistered_overlay_declares_nothing(self) -> None:
        assert overlay_repo_slugs("no-such-overlay") == frozenset()

    def test_the_global_scope_declares_nothing(self) -> None:
        assert overlay_repo_slugs("") == frozenset()

    def test_a_raising_registry_degrades_to_empty(self) -> None:
        with patch("teatree.core.merge.clear_scope.get_all_overlays", side_effect=RuntimeError("registry down")):
            assert overlay_repo_slugs(OVERLAY) == frozenset()


class TestClearScopePredicate(django.test.TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()

    def _clear(self, **kwargs):
        kwargs.setdefault("issued_at", self.now - timedelta(hours=1))
        return MergeClearFactory(**kwargs)

    def test_the_global_scope_matches_every_clear(self) -> None:
        in_scope = clear_scope_predicate("")
        assert in_scope(self._clear(ticket=None, slug="anyone/anything")) is True
        assert in_scope(self._clear(ticket=TicketFactory(overlay="other-overlay"))) is True

    def test_a_ticketed_clear_keeps_the_exact_overlay_join(self) -> None:
        # Behaviour preservation: the widening must not change what a ticketed row means.
        in_scope = clear_scope_predicate(OVERLAY)
        assert in_scope(self._clear(ticket=TicketFactory(overlay=OVERLAY))) is True
        assert in_scope(self._clear(ticket=TicketFactory(overlay="other-overlay"))) is False

    def test_a_ticketless_clear_in_a_declared_repo_is_in_scope(self) -> None:
        in_scope = clear_scope_predicate(OVERLAY)
        assert in_scope(self._clear(ticket=None, slug=SLUG)) is True

    def test_a_ticketless_clear_in_a_foreign_repo_is_out_of_scope(self) -> None:
        in_scope = clear_scope_predicate(OVERLAY)
        assert in_scope(self._clear(ticket=None, slug="someone-else/other-repo")) is False

    def test_a_workstream_slug_resolves_to_the_running_clones_repo(self) -> None:
        # A CLEAR slug is a WORKSTREAM name, not a repo — the same resolution order the
        # merge transport uses falls through to the clone origin.
        with patch("teatree.core.merge.clear_scope._project_repo_slug", return_value=SLUG):
            in_scope = clear_scope_predicate(OVERLAY)
            assert in_scope(self._clear(ticket=None, slug="4250-stale-clears")) is True

    def test_a_workstream_slug_is_out_when_the_clone_is_another_overlays(self) -> None:
        with patch("teatree.core.merge.clear_scope._project_repo_slug", return_value="someone-else/other-repo"):
            in_scope = clear_scope_predicate(OVERLAY)
            assert in_scope(self._clear(ticket=None, slug="4250-stale-clears")) is False

    def test_the_clone_origin_is_probed_once_per_scope_not_once_per_row(self) -> None:
        with patch("teatree.core.merge.clear_scope._project_repo_slug", return_value=SLUG) as probe:
            in_scope = clear_scope_predicate(OVERLAY)
            for i in range(4):
                in_scope(self._clear(ticket=None, pr_id=7000 + i, slug=f"{i}-workstream"))
        assert probe.call_count == 1

    def test_an_overlay_declaring_no_repo_reports_rather_than_hides(self) -> None:
        # Cannot attribute the row either way. A stalled merge surfaced under the wrong
        # overlay still gets read; one surfaced under none does not.
        with patch("teatree.core.merge.clear_scope.overlay_repo_slugs", return_value=frozenset()):
            in_scope = clear_scope_predicate(OVERLAY)
            assert in_scope(self._clear(ticket=None, slug="someone-else/other-repo")) is True

    def test_a_raising_clone_probe_degrades_to_out_of_scope_not_a_crash(self) -> None:
        with patch("teatree.core.merge.clear_scope._project_repo_slug", side_effect=RuntimeError("no git")):
            in_scope = clear_scope_predicate(OVERLAY)
            assert in_scope(self._clear(ticket=None, slug="4250-stale-clears")) is False
