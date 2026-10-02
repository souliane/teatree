"""Under ``afk`` the factory starts no colleague work at all (B14).

Refused at SELECTION, never at the post. Running a review agent and discarding its output
at the egress gate is the shape souliane/teatree#4626 already paid for — sixteen completed
reviews thrown away after the work was done — and under the token constraint these postures
exist for, it is exactly the work not worth starting.

Each assertion carries its own control on the SAME backend under ``present``: without one, a
"nothing is selected" reading would satisfy the afk half for the uninteresting reason.
"""

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import django.test

from teatree.core.models import Loop, Mode, ModeOverride
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends


def _backend() -> "OverlayBackends":
    """A stub overlay backend carrying both a code host and messaging."""
    backend = MagicMock()
    backend.name = "t3-teatree"
    backend.overlay = MagicMock()
    backend.overlay.metadata.get_followup_repos.return_value = ["souliane/teatree"]
    backend.overlay.config.get_github_token.return_value = ""
    backend.hosts = (MagicMock(),)
    return backend


@django.test.override_settings(USE_TZ=True)
class TestAfkSelectsNoColleagueWork(django.test.TestCase):
    def setUp(self) -> None:
        loops = list(Loop.objects.values_list("name", flat=True))
        for name, egress in (("afk", "forbid"), ("present", "allow")):
            Mode.objects.update_or_create(name=name, defaults={"entries": dict.fromkeys(loops, True), "egress": egress})

    def _scanner_names(self, domain: Domain, posture: str) -> set[str]:
        ModeOverride.objects.set_override(posture, reason="test posture")
        backend = _backend()
        return {job.scanner.name for job in jobs_for_domain(domain, backend, all_backends=(backend,))}

    def test_the_colleague_review_arm_is_selected_present_and_not_afk(self) -> None:
        present = self._scanner_names(Domain.REVIEW, "present")
        afk = self._scanner_names(Domain.REVIEW, "afk")

        assert "reviewer_prs" in present, present
        assert "reviewer_prs" not in afk, afk

    def test_the_own_pr_arm_survives_afk(self) -> None:
        """The review loop still does useful work — it just never picks up a colleague's MR."""
        assert self._scanner_names(Domain.REVIEW, "afk"), "afk selected no review work at all"

    def test_the_inbound_reply_posts_are_selected_present_and_not_afk(self) -> None:
        present = self._scanner_names(Domain.FOLLOWUP, "present")
        afk = self._scanner_names(Domain.FOLLOWUP, "afk")

        assert present, "the control selected nothing"
        assert not afk & present, afk & present
