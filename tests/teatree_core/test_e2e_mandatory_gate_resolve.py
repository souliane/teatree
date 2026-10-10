"""Resolver building ``GateInputs`` from a ticket (#1967).

``resolve_gate_inputs`` is the wiring seam the ship-gate and §17.4 CLEAR call:
it asks the active overlay to classify the diff and binds to the reviewed head
SHA. The pure gate decision is tested separately; this verifies the classifier
verdict is attached to the right tree.
"""

from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.core.gates.e2e_mandatory_gate import resolve_gate_inputs
from teatree.core.models import Ticket
from teatree.core.overlay import OverlayReview
from teatree.utils.run import CommandFailedError

_SHA = "1" * 40


def _unreadable_diff() -> list[str]:
    raise CommandFailedError(["git", "diff"], 128, "", "fatal: bad revision 'origin/main...HEAD'")


class _ImpactingReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        return bool(changed_files)


class _ImpactingOverlay:
    review = _ImpactingReview()


class _SafeReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        return False


class _SafeOverlay:
    review = _SafeReview()


class _RepoExemptReview(OverlayReview):
    """Impacting on every path, yet declaring one repo free of display surface."""

    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return True

    def mandatory_e2e_exempt_repo_slugs(self) -> tuple[str, ...]:
        return ("acme-eng/skills",)


class _RepoExemptOverlay:
    review = _RepoExemptReview()


class _RepoScopedReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        return bool(changed_files)

    def mandatory_e2e_repo_slugs(self) -> tuple[str, ...]:
        return ("acme/web",)


class _RepoScopedOverlay:
    review = _RepoScopedReview()


class _OverlappingReview(_RepoScopedReview):
    def mandatory_e2e_exempt_repo_slugs(self) -> tuple[str, ...]:
        return ("acme/web",)


class _OverlappingOverlay:
    review = _OverlappingReview()


class TestResolveGateInputs(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/30", overlay="t3-teatree")

    def test_impacting_overlay_marks_inputs_impacting(self) -> None:
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            inputs = resolve_gate_inputs(self.ticket, read_diff=lambda: ["app/views.py"], head_sha=_SHA)
        assert inputs.display_impacting is True
        assert inputs.head_sha == _SHA

    def test_safe_overlay_marks_inputs_non_impacting(self) -> None:
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_SafeOverlay()):
            inputs = resolve_gate_inputs(self.ticket, read_diff=lambda: ["app/views.py"], head_sha=_SHA)
        assert inputs.display_impacting is False

    def test_unresolvable_overlay_fails_closed_impacting(self) -> None:
        # #1426 posture: a ticket whose overlay cannot be resolved is presumed
        # display-impacting so the gate is never silently skipped on a
        # misconfigured ticket. The non-impacting diff is irrelevant — resolution
        # fails before classification, so the verdict is the fail-closed default.
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", side_effect=ImproperlyConfigured("no overlay")):
            inputs = resolve_gate_inputs(self.ticket, read_diff=lambda: ["app/tests/test_x.py"], head_sha=_SHA)
        assert inputs.display_impacting is True


class TestUnreadableDiff(TestCase):
    """A diff that could not be read is presumed impacting and says so; it is never an empty diff."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/31", overlay="t3-teatree")

    def test_an_unreadable_diff_is_presumed_impacting_even_for_a_safe_overlay(self) -> None:
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_SafeOverlay()):
            inputs = resolve_gate_inputs(self.ticket, read_diff=_unreadable_diff, head_sha=_SHA)

        assert inputs.display_impacting is True
        assert "bad revision" in inputs.unread_diff

    def test_a_repo_without_display_surface_reads_no_diff(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://gitlab.example.com/acme-eng/skills/-/work_items/46")
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_RepoExemptOverlay()):
            inputs = resolve_gate_inputs(ticket, read_diff=_unreadable_diff, head_sha=_SHA)

        assert inputs.display_impacting is False
        assert inputs.unread_diff == ""


class TestRepoLevelExemption(TestCase):
    """A repo with no display surface passes before the per-path classifier runs.

    Every case here uses an overlay whose classifier answers impacting for any
    path, so a ``False`` verdict can only come from the repo declaration — and
    the non-exempt cases prove the declaration is not simply disabling the gate.
    """

    def _verdict(self, issue_url: str) -> bool:
        ticket = Ticket.objects.create(issue_url=issue_url, overlay="t3-teatree")
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_RepoExemptOverlay()):
            return resolve_gate_inputs(ticket, read_diff=lambda: ["evals/x.json"], head_sha=_SHA).display_impacting

    def test_a_ticket_on_the_declared_repo_is_not_impacting(self) -> None:
        assert self._verdict("https://gitlab.example.com/acme-eng/skills/-/work_items/44") is False

    def test_a_merge_request_url_resolves_to_the_same_repo(self) -> None:
        assert self._verdict("https://gitlab.example.com/acme-eng/skills/-/merge_requests/139") is False

    def test_a_sibling_repo_in_the_same_namespace_is_not_exempt(self) -> None:
        assert self._verdict("https://gitlab.example.com/acme-eng/product/-/issues/44") is True

    def test_an_unparsable_issue_url_yields_no_slug_and_stays_impacting(self) -> None:
        assert self._verdict("https://gitlab.example.com/acme-eng/skills") is True

    def test_an_overlay_declaring_nothing_stays_impacting(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://gitlab.example.com/acme-eng/skills/-/work_items/45")
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()):
            inputs = resolve_gate_inputs(ticket, read_diff=lambda: ["evals/x.json"], head_sha=_SHA)

        assert inputs.display_impacting is True


class TestMandatoryE2EScope(TestCase):
    def _resolve(self, issue_url: str, *, unreadable: bool = False) -> bool:
        ticket = Ticket.objects.create(issue_url=issue_url, overlay="t3-teatree")
        read_diff = _unreadable_diff if unreadable else lambda: ["app/views.py"]
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_RepoScopedOverlay()):
            inputs = resolve_gate_inputs(ticket, read_diff=read_diff, head_sha=_SHA)
        return inputs.display_impacting

    def test_repo_outside_declared_scope_skips_unreadable_diff(self) -> None:
        assert self._resolve("https://github.com/acme/api/issues/1", unreadable=True) is False

    def test_repo_inside_declared_scope_uses_path_classifier(self) -> None:
        assert self._resolve("https://github.com/acme/web/issues/1") is True

    def test_gitlab_merge_request_inside_declared_scope_uses_path_classifier(self) -> None:
        assert self._resolve("https://gitlab.acme.example/acme/web/-/merge_requests/9") is True

    def test_unparsable_issue_url_uses_path_classifier(self) -> None:
        assert self._resolve("https://github.com/acme/api") is True

    def test_absent_issue_url_uses_path_classifier(self) -> None:
        assert self._resolve("") is True

    def test_explicit_exemption_still_wins_inside_declared_scope(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/acme/web/issues/2", overlay="t3-teatree")
        with patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=_OverlappingOverlay()):
            inputs = resolve_gate_inputs(ticket, read_diff=_unreadable_diff, head_sha=_SHA)
        assert inputs.display_impacting is False

    def test_missing_slug_never_calls_scope_hook(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://gitlab.acme.example/acme/api", overlay="t3-teatree")
        overlay = _RepoScopedOverlay()
        with (
            patch.object(overlay.review, "mandatory_e2e_repo_slugs", return_value=("acme/web",)) as scope_hook,
            patch("teatree.core.gates.e2e_mandatory_gate.get_overlay", return_value=overlay),
        ):
            inputs = resolve_gate_inputs(ticket, read_diff=lambda: ["app/views.py"], head_sha=_SHA)
        assert inputs.display_impacting is True
        scope_hook.assert_not_called()

    def test_scope_hook_defaults_empty(self) -> None:
        assert OverlayReview().mandatory_e2e_repo_slugs() == ()


class TestCoreRepoWithoutE2EProducer(TestCase):
    def test_real_t3_teatree_overlay_does_not_require_customer_display_e2e(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/1967",
            overlay="t3-teatree",
        )

        inputs = resolve_gate_inputs(ticket, read_diff=lambda: ["src/teatree/core/views/home.py"], head_sha=_SHA)

        assert inputs.display_impacting is False

    def test_a_teatree_ticket_without_a_readable_diff_is_not_impacting(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/issues/4929",
            overlay="t3-teatree",
        )

        inputs = resolve_gate_inputs(ticket, read_diff=_unreadable_diff, head_sha=_SHA)

        assert inputs.display_impacting is False
        assert inputs.unread_diff == ""
