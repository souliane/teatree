"""Classifier parity across the keystone + four reviewing seams (#1773).

The merge keystone and the four reviewing scanners (``pr_sweep``,
``codex_review``, ``slack_broadcasts``, the mechanical handlers) must reach the
SAME trusted-vs-untrusted verdict for the same ``(slug, author)`` — they all
read the one shared :func:`teatree.core.review.author_trust.classify_author`. These
tests pin that they cannot drift: an untrusted public author is flagged on
every seam, a trusted one on none, and a private repo on none.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.authorization import assert_merge_provenance_trusted
from teatree.core.models import PullRequest, ScannedBroadcast, Ticket, TrustedIdentity
from teatree.core.review import author_trust
from teatree.loop.mechanical import payload_author_untrusted_public
from teatree.loop.scanners import codex_review, pr_sweep, slack_broadcasts
from teatree.loop.scanners.pr_sweep_decision import untrusted_merge_provenance

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_URL = "https://github.com/souliane/teatree/pull/42"


def _pr_sweep_summary(author: str) -> pr_sweep.PrSummary:
    return pr_sweep.PrSummary(
        slug=_SLUG,
        number=42,
        head_sha="a" * 40,
        is_draft=False,
        has_changes_requested=False,
        rollup=(),
        url=_URL,
        author=author,
    )


def _codex_summary(author: str) -> codex_review.PrSummary:
    return codex_review.PrSummary(
        slug=_SLUG,
        number=42,
        head_sha="a" * 40,
        is_draft=False,
        changed_files=("README.md",),
        url=_URL,
        author=author,
    )


def _mr_state(author: str) -> slack_broadcasts.MrState:
    return slack_broadcasts.MrState(url=_URL, merged=False, approved=False, author_username=author)


class TestClassifierParity(TestCase):
    def setUp(self) -> None:
        TrustedIdentity.objects.get_or_create(platform="github", handle="souliane")

    def _flags_for(self, author: str) -> dict[str, bool]:
        """The untrusted/adversarial verdict each seam reaches for one author on a public repo."""
        broadcast = ScannedBroadcast.objects.create(channel="C1", slack_ts="1.1", classification="pending")
        with patch.object(author_trust, "repo_is_internal", return_value=False):
            keystone = author_trust.classify_author(
                _SLUG, author, pr_url=f"https://github.com/{_SLUG}/pull/1"
            ).untrusted
            sweep = untrusted_merge_provenance(_pr_sweep_summary(author))
            codex = codex_review._classify_variant(
                ("README.md",), slug=_SLUG, author=author, pr_url=f"https://github.com/{_SLUG}/pull/1"
            ) == (codex_review.ADVERSARIAL_REVIEW_VARIANT)
            slack_signal = slack_broadcasts._signal_for_pending_mr(_mr_state(author), broadcast, overlay="")
            slack = bool(slack_signal.payload["adversarial"])
            mechanical = payload_author_untrusted_public({"url": _URL, "author": author})
        return {
            "keystone": keystone,
            "pr_sweep": sweep,
            "codex_review": codex,
            "slack_broadcasts": slack,
            "mechanical": mechanical,
        }

    def test_untrusted_author_flagged_on_every_seam(self) -> None:
        flags = self._flags_for("evilhacker")
        assert all(flags.values()), flags

    def test_trusted_author_flagged_on_no_seam(self) -> None:
        flags = self._flags_for("souliane")
        assert not any(flags.values()), flags

    def test_private_repo_flags_no_seam(self) -> None:
        broadcast = ScannedBroadcast.objects.create(channel="C1", slack_ts="2.2", classification="pending")
        with patch.object(author_trust, "repo_is_internal", return_value=True):
            assert (
                author_trust.classify_author(_SLUG, "evilhacker", pr_url=f"https://github.com/{_SLUG}/pull/1").untrusted
                is False
            )
            assert untrusted_merge_provenance(_pr_sweep_summary("evilhacker")) is False
            variant = codex_review._classify_variant(
                ("README.md",), slug=_SLUG, author="evilhacker", pr_url=f"https://github.com/{_SLUG}/pull/1"
            )
            assert variant == codex_review.STANDARD_REVIEW_VARIANT
            signal = slack_broadcasts._signal_for_pending_mr(_mr_state("evilhacker"), broadcast, overlay="")
            assert signal.payload["adversarial"] is False
            assert payload_author_untrusted_public({"url": _URL, "author": "evilhacker"}) is False

    def test_declared_private_repo_agrees_across_sweep_keystone_and_reviewers(self) -> None:
        broadcast = ScannedBroadcast.objects.create(channel="C1", slack_ts="3.3", classification="pending")
        ticket = Ticket.objects.create(overlay="t3-teatree")
        PullRequest.objects.create(ticket=ticket, url=_URL, repo=_SLUG, iid="42")
        with (
            patch(
                "teatree.hooks._private_repo_entries.private_repo_visibility",
                side_effect=lambda slug, **_kwargs: "PRIVATE" if slug == "github.com/souliane/teatree" else "PUBLIC",
            ),
            patch.object(author_trust, "trusted_handles", return_value=set()),
            patch("teatree.core.merge.authorization.CodeHostQuery.for_ref") as query,
        ):
            query.return_value.pr_author.return_value = "colleague"
            query.return_value.pr_same_repo.return_value = True
            assert untrusted_merge_provenance(_pr_sweep_summary("colleague")) is False
            assert assert_merge_provenance_trusted(slug=_SLUG, pr_id=42) is None
            assert (
                codex_review._classify_variant(("README.md",), slug=_SLUG, author="colleague", pr_url=_URL)
                == codex_review.STANDARD_REVIEW_VARIANT
            )
            signal = slack_broadcasts._signal_for_pending_mr(_mr_state("colleague"), broadcast, overlay="")
            assert signal.payload["adversarial"] is False
