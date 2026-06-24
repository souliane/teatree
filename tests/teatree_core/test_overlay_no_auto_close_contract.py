"""No-auto-close contract for a human-closed overlay (#2694).

An overlay configured for human-closed tickets must never auto-close its
issue via a closing keyword (``Closes/Fixes/Resolves #N`` or a full-URL
form) that slips into an MR/PR description or a commit body. Three
independent mechanisms enforce this on the ship path; this module pins
each one with an anti-vacuous RED→GREEN flip, so the guard cannot rot
into a no-op without turning a test red.

The coverage is OVERLAY-AGNOSTIC: a synthetic ``test-org/test-repo``
namespace and a synthetic ``HumanClosedOverlay`` stand in for any real
human-closed overlay. No real overlay/namespace appears here.

The three mechanisms and the exact condition flip that re-introduces
the contract violation (RED) for each:

Mechanism 1 — ``sanitize_close_keywords`` / ``should_close_ticket``, gated
by ``overlay.config.mr_close_ticket`` (default ``False`` = human-closed).
TEETH: flip ``mr_close_ticket`` ``False → True`` and the closing keyword
SURVIVES (RED). False → rewritten to ``Relates to`` (GREEN).

Mechanism 2 — ``run_close_keyword_gate``, gated by
``overlay.config.forbid_close_keywords`` (default ``False``). TEETH:
``True`` → the gate REFUSES (``SystemExit``); ``False`` (disabled) → the
keyword ships unrefused (RED for an overlay that wants a hard refusal).
Enabled → blocked (GREEN).

Mechanism 3 — ``apply_publish_gate``, strips closing trailers when the
target repo namespace matches the DB-home ``ban_close_trailers_on_namespaces``.
TEETH: a populated pattern strips (GREEN); the EMPTY default does NOT strip
(RED) — this is the documented unsafe default for this mechanism.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.close_trailer_scanner import apply_publish_gate
from teatree.core.management.commands._close_keyword_gate import run_close_keyword_gate
from teatree.core.models import ConfigSetting, Session, Ticket, Worktree
from teatree.core.overlay import OverlayConfig
from teatree.core.runners.ship import CLOSE_KEYWORD_RE, ShipExecutor, sanitize_close_keywords, should_close_ticket
from tests.teatree_core.management_commands._overlays import FullOverlay

TEST_REPO = "test-org/test-repo"
TEST_NAMESPACE_PATTERN = "test-org/*"
OTHER_REPO = "other-org/other-repo"

CLOSING_BODY_HASH = "Closes #777"
CLOSING_BODY_URL = "Fixes https://example.com/test-org/test-repo/issues/777"


class _HumanClosedOverlay(FullOverlay):
    """Synthetic overlay configured for human-closed tickets.

    ``mr_close_ticket`` and ``forbid_close_keywords`` both stay at the
    human-closed default (``False``). Tests construct toggled variants by
    overriding the config in ``__init__``.
    """

    config = OverlayConfig()

    def __init__(self, *, mr_close_ticket: bool = False, forbid_close_keywords: bool = False) -> None:
        super().__init__()
        self.config.mr_close_ticket = mr_close_ticket
        self.config.forbid_close_keywords = forbid_close_keywords


@contextmanager
def _overlay(*, mr_close_ticket: bool = False, forbid_close_keywords: bool = False) -> Iterator[None]:
    overlay = _HumanClosedOverlay(
        mr_close_ticket=mr_close_ticket,
        forbid_close_keywords=forbid_close_keywords,
    )
    with patch("teatree.core.management.commands._close_keyword_gate.get_overlay", return_value=overlay):
        yield


class _FakeHost:
    def current_user(self) -> str:
        return "tester"

    def create_pr(self, spec: PullRequestSpec) -> dict[str, str]:
        return {"web_url": "https://example.com/pr/1"}


# ── Mechanism 1: mr_close_ticket gate (sanitize_close_keywords) ──────────


class TestMrCloseTicketSanitize(TestCase):
    """A human-closed overlay rewrites a closing keyword to a non-closing ref."""

    def test_green_human_closed_rewrites_keyword(self) -> None:
        # GREEN: mr_close_ticket=False → keyword rewritten, none survives.
        close_ticket = should_close_ticket({}, setting_enabled=False)
        cleaned = sanitize_close_keywords(f"feat: subject\n\n{CLOSING_BODY_HASH}", close_ticket=close_ticket)
        assert "Relates to" in cleaned
        assert not _has_closing_keyword(cleaned)

    def test_green_url_form_rewritten(self) -> None:
        close_ticket = should_close_ticket({}, setting_enabled=False)
        cleaned = sanitize_close_keywords(f"feat: subject\n\n{CLOSING_BODY_URL}", close_ticket=close_ticket)
        assert "Fixes" not in cleaned
        assert "Relates to https://example.com/test-org/test-repo/issues/777" in cleaned

    def test_red_auto_close_overlay_keyword_survives(self) -> None:
        # TEETH: flip mr_close_ticket False → True; the closing keyword now
        # SURVIVES — the contract-violation the human-closed default prevents.
        close_ticket = should_close_ticket({}, setting_enabled=True)
        cleaned = sanitize_close_keywords(f"feat: subject\n\n{CLOSING_BODY_HASH}", close_ticket=close_ticket)
        assert CLOSING_BODY_HASH in cleaned

    def test_should_close_ticket_resolves_human_closed_to_false(self) -> None:
        assert should_close_ticket({}, setting_enabled=False) is False
        assert should_close_ticket({}, setting_enabled=True) is True


# ── Mechanism 1 (integration on the ship path) ───────────────────────────


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestShipPathSanitize:
    """``ShipExecutor._build_pr_spec`` sanitizes per ``mr_close_ticket``."""

    def _ship_spec(self, *, mr_close_ticket: bool) -> PullRequestSpec:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://example.com/test-org/test-repo/issues/777",
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path=TEST_REPO,
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
        )
        overlay = _HumanClosedOverlay(mr_close_ticket=mr_close_ticket)
        with (
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat: subject", CLOSING_BODY_HASH),
            ),
            patch("teatree.core.runners.ship.git.config_value", return_value="tester"),
            patch("teatree.core.runners.ship.get_overlay", return_value=overlay),
            patch("teatree.core.runners.ship.get_overlay_publish_gates", return_value=[]),
        ):
            return ShipExecutor._build_pr_spec(ticket, _FakeHost(), TEST_REPO, "feat-x", {})

    def test_green_human_closed_rewrites_pr_description(self) -> None:
        spec = self._ship_spec(mr_close_ticket=False)
        assert CLOSING_BODY_HASH not in spec.description
        assert "Relates to #777" in spec.description

    def test_red_auto_close_overlay_keeps_pr_description_keyword(self) -> None:
        # TEETH: mr_close_ticket True → the closing keyword survives into the
        # published PR description (the auto-close behaviour).
        spec = self._ship_spec(mr_close_ticket=True)
        assert CLOSING_BODY_HASH in spec.description


# ── Mechanism 2: forbid_close_keywords hard-refusal gate ─────────────────


class TestForbidCloseKeywordsGate(TestCase):
    """``run_close_keyword_gate`` refuses iff ``forbid_close_keywords``."""

    def _worktree(self) -> tuple[Ticket, Worktree]:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://example.com/test-org/test-repo/issues/777",
        )
        session = Session.objects.create(overlay="test", ticket=ticket)
        for phase in ("testing", "reviewing", "retro"):
            session.visit_phase(phase)
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
        )
        return ticket, worktree

    @contextmanager
    def _git_sources(self, *, subject: str) -> Iterator[None]:
        with (
            patch(
                "teatree.core.management.commands._close_keyword_gate.git.last_commit_message",
                return_value=(subject, ""),
            ),
            patch(
                "teatree.core.management.commands._close_keyword_gate.git.default_branch",
                return_value="main",
            ),
            patch(
                "teatree.core.management.commands._close_keyword_gate.git.commit_messages",
                return_value=[],
            ),
        ):
            yield

    def test_green_gate_enabled_refuses(self) -> None:
        # GREEN: forbid_close_keywords=True → the gate raises SystemExit.
        ticket, worktree = self._worktree()
        with (
            self._git_sources(subject=CLOSING_BODY_HASH),
            _overlay(forbid_close_keywords=True),
            pytest.raises(SystemExit) as ctx,
        ):
            run_close_keyword_gate(ticket, worktree)
        assert "Relates to #777" in str(ctx.value)

    def test_green_gate_enabled_refuses_url_form(self) -> None:
        ticket, worktree = self._worktree()
        with (
            self._git_sources(subject=CLOSING_BODY_URL),
            _overlay(forbid_close_keywords=True),
            pytest.raises(SystemExit),
        ):
            run_close_keyword_gate(ticket, worktree)

    def test_red_gate_disabled_keyword_ships_unrefused(self) -> None:
        # TEETH: flip forbid_close_keywords True → False (disable the gate);
        # the closing keyword ships WITHOUT a refusal — the contract violation
        # for an overlay that wants a hard refusal.
        ticket, worktree = self._worktree()
        with self._git_sources(subject=CLOSING_BODY_HASH), _overlay(forbid_close_keywords=False):
            # No SystemExit raised → the gate is a no-op when disabled.
            assert run_close_keyword_gate(ticket, worktree) is None

    def test_relates_to_passes_when_enabled(self) -> None:
        ticket, worktree = self._worktree()
        with self._git_sources(subject="Relates to #777"), _overlay(forbid_close_keywords=True):
            assert run_close_keyword_gate(ticket, worktree) is None


# ── Mechanism 3: ban_close_trailers_on_namespaces publish gate ───────────


class TestPublishGateNamespaceStrip(TestCase):
    """``apply_publish_gate`` strips trailers for a banned namespace only."""

    def test_green_banned_namespace_strips_trailer(self) -> None:
        # GREEN: a populated DB-home pattern strips the closing trailer.
        body = f"feat: subject\n\n{CLOSING_BODY_HASH}"
        cleaned = apply_publish_gate(body, repo=TEST_REPO, patterns=[TEST_NAMESPACE_PATTERN])
        assert "Closes" not in cleaned
        assert cleaned == "feat: subject"

    def test_red_empty_patterns_does_not_strip(self) -> None:
        # TEETH (the documented prod gap): the EMPTY default leaves the
        # closing trailer in place — the trailer is NOT stripped. This is the
        # UNSAFE DEFAULT: a human-closed overlay relying solely on this
        # mechanism with an unset DB value still ships an auto-close trailer.
        body = f"feat: subject\n\n{CLOSING_BODY_HASH}"
        cleaned = apply_publish_gate(body, repo=TEST_REPO, patterns=[])
        assert CLOSING_BODY_HASH in cleaned
        assert cleaned == body

    def test_red_non_banned_namespace_does_not_strip(self) -> None:
        # A repo OUTSIDE the banned namespace keeps the trailer even when the
        # pattern is populated — the strip is scoped to the matched namespace.
        body = f"feat: subject\n\n{CLOSING_BODY_HASH}"
        cleaned = apply_publish_gate(body, repo=OTHER_REPO, patterns=[TEST_NAMESPACE_PATTERN])
        assert CLOSING_BODY_HASH in cleaned

    def test_green_url_trailer_stripped_for_banned_namespace(self) -> None:
        body = f"feat: subject\n\n{CLOSING_BODY_URL}"
        cleaned = apply_publish_gate(body, repo=TEST_REPO, patterns=[TEST_NAMESPACE_PATTERN])
        assert "Fixes" not in cleaned
        assert cleaned == "feat: subject"


class TestPublishGateDbHomeUnsafeDefault(TestCase):
    """The DB-home setting defaults EMPTY — the documented unsafe default."""

    def test_green_db_value_set_strips_via_effective_settings(self) -> None:
        # GREEN: a GLOBAL ConfigSetting row populates the pattern; the
        # effective-settings tier resolves it and the trailer is stripped.
        ConfigSetting.objects.set_value("ban_close_trailers_on_namespaces", [TEST_NAMESPACE_PATTERN])
        patterns = list(get_effective_settings().ban_close_trailers_on_namespaces)
        body = f"feat: subject\n\n{CLOSING_BODY_HASH}"
        cleaned = apply_publish_gate(body, repo=TEST_REPO, patterns=patterns)
        assert "Closes" not in cleaned

    def test_red_db_value_empty_is_unsafe_default(self) -> None:
        # TEETH: with NO ConfigSetting row the resolved pattern list is EMPTY,
        # so the trailer is NOT stripped — the unsafe default this case
        # documents. Setting the DB value (above) flips it GREEN.
        patterns = list(get_effective_settings().ban_close_trailers_on_namespaces)
        assert patterns == []
        body = f"feat: subject\n\n{CLOSING_BODY_HASH}"
        cleaned = apply_publish_gate(body, repo=TEST_REPO, patterns=patterns)
        assert CLOSING_BODY_HASH in cleaned


def _has_closing_keyword(text: str) -> bool:
    return CLOSE_KEYWORD_RE.search(text) is not None
