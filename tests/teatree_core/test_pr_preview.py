"""Tests for the PR ship-preview / metadata helpers (mirrors ``_pr_preview``).

Split out of ``test_pr_command`` alongside the ``_pr_preview`` module split:
test files mirror the production module path.
"""

from typing import TypedDict
from unittest.mock import patch

from django.test import TestCase

from teatree.core.management.commands import _pr_preview
from teatree.core.management.commands._pr_preview import ship_preview, validate_pr_metadata
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayMetadata
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


class _GeneratingMetadata(OverlayMetadata):
    """An overlay metadata that REPLACES a non-canonical subject with a fixed title."""

    def build_pr_title(self, *, branch: str, subject: str, body: str, issue_url: str) -> str:
        return "fix(corridor): canonical generated title [none] (proj#119)"


class _GenOverlay(CommandOverlay):
    metadata = _GeneratingMetadata()


class _ValidationResult(TypedDict):
    errors: list[str]
    warnings: list[str]


class _IssueUrlFirstLineMetadata(OverlayMetadata):
    """Customer-MR grammar: the title MUST reference a GitLab issue URL.

    Mirrors a downstream customer overlay's ``validate_pr``, which rejects any
    title that does not carry a ``…/-/issues/<n>`` reference. A
    tooling/non-customer PR with an explicit ``--title`` / ``pr_title_override``
    must validate the title that will actually ship — not the regenerated
    commit subject — so the override clears (or, for a tooling title, fails)
    the preflight against the title it will actually use.
    """

    def validate_pr(self, title: str, description: str) -> _ValidationResult:
        _ = description
        if "/-/issues/" not in title:
            return _ValidationResult(
                errors=[f"PR title must reference a GitLab issue URL (got: {title!r})"],
                warnings=[],
            )
        return _ValidationResult(errors=[], warnings=[])


class _CustomerGrammarOverlay(CommandOverlay):
    metadata = _IssueUrlFirstLineMetadata()


class _RequiredSectionMetadata(OverlayMetadata):
    """An overlay declaring ``Configuration`` as a mandatory description section."""

    def get_required_description_sections(self) -> list[str]:
        return ["Configuration"]


class _RequiredSectionOverlay(CommandOverlay):
    metadata = _RequiredSectionMetadata()


class TestShipPreviewTitleDescriptionInvariant(TestCase):
    """The description's first line must always equal the title.

    A diverged title vs. description-first-line is what blocks the
    release-notes pipeline. ``ship_preview`` must build them so they can
    never differ by construction, regardless of body presence, the
    fallback title path, or close-keyword sanitization.
    """

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/119",
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def _first_line(self, description: str) -> str:
        return description.split("\n", 1)[0]

    def test_first_line_equals_title_with_body(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("feat: add X [FLAG] (proj#119)", "Body paragraph.\n"),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert self._first_line(description) == title

    def test_first_line_equals_title_without_body(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(_pr_preview.git, "last_commit_message", return_value=("fix: y (proj#119)", "")),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert self._first_line(description) == title
        assert title == "fix: y (proj#119)"

    def test_first_line_equals_title_on_fallback_title(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(_pr_preview.git, "last_commit_message", return_value=("", "")),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        # Invariant holds even when the fallback title carries a close
        # keyword ("Resolve") that close-keyword sanitization rewrites.
        assert self._first_line(description) == title
        assert ticket.issue_url in title

    def test_first_line_equals_title_when_subject_has_close_keyword(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("fix: resolves #119 the corridor bug (proj#119)", "Body."),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        # Title and first line are both the *sanitized* string -> still equal.
        assert self._first_line(description) == title


class TestShipPreviewUsesOverlayGeneratedTitle(TestCase):
    """The title is PRODUCED by the overlay, not copied from the subject.

    An overlay enforcing a title grammar must be able to REPLACE a
    non-canonical commit subject (e.g. ``test(insurance): …``) with a
    compliant generated title — and the description first line must follow it
    so the two never diverge.
    """

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="gen",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/119",
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="gen",
            repo_path="/tmp/backend",
            branch="119-fix-corridor-margin",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def test_overlay_generated_title_replaces_subject_and_first_line_follows(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"gen": _GenOverlay()}),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("test(insurance): add coverage", "Body paragraph.\n"),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert title == "fix(corridor): canonical generated title [none] (proj#119)"
        assert description.split("\n", 1)[0] == title


class TestShipPreviewHonorsTitleOverride(TestCase):
    """``ship_preview`` must use the pinned title, not the regenerated subject.

    Parity with ``ShipExecutor._build_pr_spec`` (``runners/ship.py``): a title
    pinned via ``ticket.extra['pr_title_override']`` (or an explicit ``--title``)
    is the title that will actually ship, so the preview — and therefore the
    preflight validation built on it — must reflect it. Regenerating the title
    from the last commit subject and ignoring the override makes the preflight
    validate a title that is NOT the one that ships.
    """

    def _ticket_with_worktree(self, *, extra: dict[str, str] | None = None) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/298",
            extra=extra or {},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="298-fix-thing",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def test_pr_title_override_replaces_regenerated_subject(self) -> None:
        ticket = self._ticket_with_worktree(extra={"pr_title_override": "fix(scope): pinned title (#298)"})
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("chore: some unrelated subject (#298)", "Body."),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert title == "fix(scope): pinned title (#298)"
        assert description.split("\n", 1)[0] == title

    def test_explicit_title_argument_replaces_regenerated_subject(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("chore: some unrelated subject (#298)", "Body."),
            ),
        ):
            _, title, description = ship_preview(
                ticket, ticket.worktrees.first(), title="fix(scope): explicit flag title (#298)"
            )
        assert title == "fix(scope): explicit flag title (#298)"
        assert description.split("\n", 1)[0] == title

    def test_explicit_title_argument_wins_over_stored_override(self) -> None:
        ticket = self._ticket_with_worktree(extra={"pr_title_override": "fix(scope): stored title (#298)"})
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("chore: subject (#298)", "Body."),
            ),
        ):
            _, title, _ = ship_preview(ticket, ticket.worktrees.first(), title="fix(scope): flag wins (#298)")
        assert title == "fix(scope): flag wins (#298)"


class TestValidatePrMetadataHonorsTitleOverride(TestCase):
    """The preflight must validate the title that will actually ship.

    The customer-MR grammar (``_IssueUrlFirstLineMetadata``) rejects any title
    that does not reference a GitLab issue URL. A tooling PR's commit subject
    (``fix(scope): …``) lacks that reference, so regenerating the title from the
    subject false-fails the preflight — the bug a downstream customer PR hit.
    With an explicit ``--title`` (or ``pr_title_override``) carrying the issue
    URL, the preflight must validate THAT title and pass.
    """

    def _ticket_with_worktree(self, *, extra: dict[str, str] | None = None) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://gitlab.example.com/group/repo/-/issues/298",
            extra=extra or {},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="298-fix-thing",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def test_regenerated_subject_false_fails_customer_grammar(self) -> None:
        # Documents the bug: with NO override, the title is the commit subject,
        # which the customer grammar rejects.
        ticket = self._ticket_with_worktree()
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"test": _CustomerGrammarOverlay()},
            ),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("fix(scope): tooling change with no issue url (#298)", "Body."),
            ),
        ):
            error = validate_pr_metadata(ticket, ticket.worktrees.first())
        assert error is not None

    def test_explicit_title_with_issue_url_passes_preflight(self) -> None:
        ticket = self._ticket_with_worktree()
        pinned = "https://gitlab.example.com/group/repo/-/issues/298 fix(scope): tooling change"
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"test": _CustomerGrammarOverlay()},
            ),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("fix(scope): tooling change with no issue url (#298)", "Body."),
            ),
        ):
            error = validate_pr_metadata(ticket, ticket.worktrees.first(), title=pinned)
        assert error is None

    def test_stored_override_with_issue_url_passes_preflight(self) -> None:
        pinned = "https://gitlab.example.com/group/repo/-/issues/298 fix(scope): tooling change"
        ticket = self._ticket_with_worktree(extra={"pr_title_override": pinned})
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"test": _CustomerGrammarOverlay()},
            ),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("fix(scope): tooling change with no issue url (#298)", "Body."),
            ),
        ):
            error = validate_pr_metadata(ticket, ticket.worktrees.first())
        assert error is None


class TestShipPreviewEmitsRequiredSections(TestCase):
    """The generated description carries the overlay's required sections (#312).

    An overlay declaring ``Configuration`` mandatory via
    ``get_required_description_sections`` gets a ``## Configuration`` header
    emitted by default — even when the commit body omits it — so a reviewer
    can always tell "no config needed" from "the author forgot".
    """

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="reqsec",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/312",
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="reqsec",
            repo_path="/tmp/backend",
            branch="312-feat-config-section",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def test_required_section_emitted_when_body_omits_it(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"reqsec": _RequiredSectionOverlay()},
            ),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("feat: add X [FLAG] (proj#312)", "## What\nDid X.\n\n## Why\nNeeded X."),
            ),
        ):
            _, _, description = ship_preview(ticket, ticket.worktrees.first())
        assert "## Configuration" in description

    def test_no_required_sections_leaves_description_unchanged(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("feat: add X (proj#312)", "## What\nDid X.\n\n## Why\nNeeded X."),
            ),
        ):
            ticket.overlay = "test"
            ticket.save(update_fields=["overlay"])
            ticket.worktrees.update(overlay="test")
            _, _, description = ship_preview(ticket, ticket.worktrees.first())
        # Standard What/Why already present; no overlay required section to add.
        assert "## Configuration" not in description
