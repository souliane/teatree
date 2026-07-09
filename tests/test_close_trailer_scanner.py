"""Pre-publish close-trailer scanner (#1398).

User-configured ``[teatree.publish_gates] ban_close_trailers_on_namespaces``
silently strips ``Closes|Fixes|Resolves`` trailers from MR/PR descriptions
when the target repo matches one of the banned namespace patterns. Distinct
from the overlay-scoped ``forbid_close_keywords`` gate (#1012) which refuses
the publish; this scanner cleans the body and lets the publish proceed.

Default config (absent setting / empty list) is a no-op — body unchanged.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.intake.close_trailer_scanner import apply_publish_gate, namespace_is_banned, strip_close_trailers
from teatree.core.models import ConfigSetting, Ticket, Worktree
from teatree.core.runners.ship import ShipExecutor


class TestStripCloseTrailers:
    """Pure regex stripping, no namespace logic."""

    def test_strips_closes_hash_form(self) -> None:
        body = "Implements the feature.\n\nCloses #1234"
        assert strip_close_trailers(body) == "Implements the feature."

    def test_strips_fixes_hash_form(self) -> None:
        body = "Subject line\n\nFixes #42"
        assert strip_close_trailers(body) == "Subject line"

    def test_strips_resolves_hash_form(self) -> None:
        body = "Subject\n\nResolves #99"
        assert strip_close_trailers(body) == "Subject"

    def test_case_insensitive(self) -> None:
        for keyword in ("closes", "CLOSES", "Closes", "FIXES", "resolves"):
            body = f"Body line\n\n{keyword} #1"
            assert strip_close_trailers(body) == "Body line"

    def test_strips_closes_part_of(self) -> None:
        body = "Subject\n\nCloses part of #8521"
        assert strip_close_trailers(body) == "Subject"

    def test_strips_full_url_form(self) -> None:
        body = "Subject\n\nFixes https://gitlab.com/group/proj/-/issues/9"
        assert strip_close_trailers(body) == "Subject"

    def test_strips_multiple_consecutive_trailers(self) -> None:
        body = "Subject\n\nCloses #1\nFixes #2\nResolves #3"
        assert strip_close_trailers(body) == "Subject"

    def test_no_trailer_returns_unchanged(self) -> None:
        body = "Subject\n\nRelates to #42\n\nSome body content."
        assert strip_close_trailers(body) == body

    def test_relates_to_not_stripped(self) -> None:
        body = "Subject\n\nRelates to #42"
        assert strip_close_trailers(body) == body

    def test_keyword_mid_line_not_stripped(self) -> None:
        body = "Subject mentioning closes #42 inline"
        assert strip_close_trailers(body) == body

    def test_empty_body(self) -> None:
        assert strip_close_trailers("") == ""

    def test_strips_only_trailing_blank_after_removal(self) -> None:
        body = "Subject\n\nMore content\n\nCloses #1\n"
        assert strip_close_trailers(body) == "Subject\n\nMore content"


class TestNamespaceIsBanned:
    """fnmatch-based pattern matching over ``namespace/repo``."""

    def test_exact_namespace_match(self) -> None:
        assert namespace_is_banned("eng-group/product", ["eng-group/*"]) is True

    def test_no_match_returns_false(self) -> None:
        assert namespace_is_banned("souliane/teatree", ["eng-group/*"]) is False

    def test_empty_patterns_returns_false(self) -> None:
        assert namespace_is_banned("eng-group/product", []) is False

    def test_strips_to_namespace_segment_only(self) -> None:
        # A nested project path matches on the leading namespace segment.
        assert namespace_is_banned("eng-group/sub/product", ["eng-group/*"]) is True

    def test_multiple_patterns_any_match(self) -> None:
        patterns = ["acme/*", "eng-group/*"]
        assert namespace_is_banned("eng-group/product", patterns) is True
        assert namespace_is_banned("acme/widget", patterns) is True
        assert namespace_is_banned("souliane/teatree", patterns) is False

    def test_exact_repo_pattern(self) -> None:
        assert namespace_is_banned("eng-group/product", ["eng-group/product"]) is True
        assert namespace_is_banned("eng-group/other", ["eng-group/product"]) is False

    def test_repo_url_form_matches(self) -> None:
        # ``ShipExecutor`` may pass a full URL or path-style repo identifier.
        # The scanner normalises to ``namespace/repo``.
        url = "https://gitlab.com/eng-group/product"
        assert namespace_is_banned(url, ["eng-group/*"]) is True


class TestApplyPublishGate:
    """Body-shape contract under ``apply_publish_gate``."""

    def test_banned_namespace_strips_close_trailer(self) -> None:
        body = "Subject\n\nCloses #1234"
        cleaned = apply_publish_gate(body, repo="eng-group/product", patterns=["eng-group/*"])
        assert "Closes" not in cleaned
        assert cleaned == "Subject"

    def test_banned_namespace_no_trailer_unchanged(self) -> None:
        body = "Subject\n\nRelates to #1234\n\nBody."
        cleaned = apply_publish_gate(body, repo="eng-group/product", patterns=["eng-group/*"])
        assert cleaned == body

    def test_non_banned_namespace_close_trailer_unchanged(self) -> None:
        body = "Subject\n\nCloses #1234"
        cleaned = apply_publish_gate(body, repo="souliane/teatree", patterns=["eng-group/*"])
        assert cleaned == body
        assert "Closes #1234" in cleaned

    def test_empty_patterns_unchanged(self) -> None:
        body = "Subject\n\nCloses #1234"
        cleaned = apply_publish_gate(body, repo="eng-group/product", patterns=[])
        assert cleaned == body

    def test_multiple_trailers_all_stripped(self) -> None:
        body = "Subject\n\nCloses #1\nFixes #2\nResolves #3"
        cleaned = apply_publish_gate(body, repo="eng-group/product", patterns=["eng-group/*"])
        assert "Closes" not in cleaned
        assert "Fixes" not in cleaned
        assert "Resolves" not in cleaned

    def test_case_insensitive_strip(self) -> None:
        body = "Subject\n\ncloses #1\nCLOSES #2"
        cleaned = apply_publish_gate(body, repo="eng-group/product", patterns=["eng-group/*"])
        assert "closes" not in cleaned.lower()

    def test_closes_part_of_stripped(self) -> None:
        body = "Subject\n\nCloses part of #8521"
        cleaned = apply_publish_gate(body, repo="eng-group/product", patterns=["eng-group/*"])
        assert cleaned == "Subject"


class TestConfigLoaderDefaults(TestCase):
    """``ban_close_trailers_on_namespaces`` is DB-home (#1775): absent -> empty, malformed -> loud."""

    def test_default_empty_list(self) -> None:
        assert get_effective_settings().ban_close_trailers_on_namespaces == []

    def test_unrelated_setting_leaves_it_empty(self) -> None:
        ConfigSetting.objects.set_value("branch_prefix", "ac")
        assert get_effective_settings().ban_close_trailers_on_namespaces == []

    def test_non_list_value_raises_loud(self) -> None:
        # A stored scalar for a list-typed setting is surfaced LOUD with the key
        # named (the strict parser), never silently degraded to an empty list.
        ConfigSetting.objects.set_value("ban_close_trailers_on_namespaces", "not-a-list")
        with pytest.raises(ValueError, match="ban_close_trailers_on_namespaces"):
            get_effective_settings()


class TestConfigLoaderDbHome(TestCase):
    """``ban_close_trailers_on_namespaces`` is DB-home (#1775): set in the store."""

    def test_resolves_namespace_patterns_from_db_store(self) -> None:
        # A GLOBAL ``ConfigSetting`` row supplies the patterns (a
        # ``[teatree.publish_gates]`` value would be ignored on read).
        ConfigSetting.objects.set_value("ban_close_trailers_on_namespaces", ["eng-group/*", "acme/*"])
        assert get_effective_settings().ban_close_trailers_on_namespaces == ["eng-group/*", "acme/*"]


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestShipExecutorIntegration:
    """``ShipExecutor._build_pr_spec`` applies the scanner before opening the PR."""

    def test_banned_namespace_strips_trailer_from_pr_description(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://example.com/issues/1",
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="eng-group/product",
            branch="feat-x",
            extra={"worktree_path": "/tmp/wt"},
        )

        captured: dict[str, PullRequestSpec] = {}

        class FakeHost:
            def current_user(self) -> str:
                return "tester"

            def create_pr(self, spec: PullRequestSpec) -> dict[str, str]:
                captured["spec"] = spec
                return {"web_url": "https://example.com/pr/1"}

        with (
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat: subject", "Closes #1234"),
            ),
            patch(
                "teatree.core.runners.ship.git.config_value",
                return_value="tester",
            ),
            patch(
                "teatree.core.runners.ship.get_overlay_publish_gates",
                return_value=["eng-group/*"],
            ),
        ):
            spec = ShipExecutor._build_pr_spec(
                ticket,
                FakeHost(),
                "eng-group/product",
                "feat-x",
                {},
            )

        assert "Closes #1234" not in spec.description
        assert "Closes" not in spec.description

    def test_non_banned_namespace_keeps_trailer(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://example.com/issues/2",
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="souliane/teatree",
            branch="feat-y",
            extra={"worktree_path": "/tmp/wt"},
        )

        class FakeHost:
            def current_user(self) -> str:
                return "tester"

            def create_pr(self, spec: PullRequestSpec) -> dict[str, str]:
                return {"web_url": "https://example.com/pr/2"}

        with (
            patch(
                "teatree.core.runners.ship.git.last_commit_message",
                return_value=("feat: subject", "Closes #1234"),
            ),
            patch(
                "teatree.core.runners.ship.git.config_value",
                return_value="tester",
            ),
            patch(
                "teatree.core.runners.ship.get_overlay_publish_gates",
                return_value=["eng-group/*"],
            ),
        ):
            spec = ShipExecutor._build_pr_spec(
                ticket,
                FakeHost(),
                "souliane/teatree",
                "feat-y",
                {},
            )

        assert "Closes #1234" in spec.description
