"""``t3 <overlay> learnings show|add|edit`` — durable per-repo knowledge store CLI (#2892)."""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.learnings import Command, LearningsResult
from teatree.core.models.project_learning import ProjectLearning


class LearningsCommandWiringTest(TestCase):
    def test_command_exposes_show_add_edit(self) -> None:
        assert hasattr(Command, "show")
        assert hasattr(Command, "add")
        assert hasattr(Command, "edit")


class LearningsShowTest(TestCase):
    def test_show_prints_empty_placeholder_when_no_row(self) -> None:
        result = cast("LearningsResult", call_command("learnings", "show", "acme-eng/widgets"))
        assert result == {"repo_slug": "acme-eng/widgets", "content": ""}

    def test_show_returns_recorded_content(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/widgets", content="CI needs FOO=1")
        result = cast("LearningsResult", call_command("learnings", "show", "acme-eng/widgets"))
        assert result == {"repo_slug": "acme-eng/widgets", "content": "CI needs FOO=1"}

    def test_show_resolves_by_issue_url(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/widgets", content="notes")
        result = cast(
            "LearningsResult",
            call_command("learnings", "show", "https://github.com/acme-eng/widgets/issues/42"),
        )
        assert result == {"repo_slug": "acme-eng/widgets", "content": "notes"}

    def test_show_never_confuses_two_repos_sharing_content(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/bugs", content="bugs-repo notes")
        ProjectLearning.objects.create(repo_slug="acme-product/repo", content="product-repo notes")

        bugs_result = cast("LearningsResult", call_command("learnings", "show", "acme-eng/bugs"))
        product_result = cast("LearningsResult", call_command("learnings", "show", "acme-product/repo"))

        assert bugs_result["content"] == "bugs-repo notes"
        assert product_result["content"] == "product-repo notes"

    def test_show_unresolvable_ref_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("learnings", "show", "https://example.com/not-a-forge-path")


class LearningsAddTest(TestCase):
    def test_add_appends_timestamped_block(self) -> None:
        result = cast(
            "LearningsResult",
            call_command("learnings", "add", "acme-eng/widgets", "de-CH locale only"),
        )
        assert result["repo_slug"] == "acme-eng/widgets"
        assert "de-CH locale only" in result["content"]
        row = ProjectLearning.objects.get(repo_slug="acme-eng/widgets")
        assert row.content.startswith("\n\n[")

    def test_add_creates_the_row_on_first_call(self) -> None:
        assert not ProjectLearning.objects.filter(repo_slug="acme-eng/widgets").exists()
        call_command("learnings", "add", "acme-eng/widgets", "first lesson")
        assert ProjectLearning.objects.filter(repo_slug="acme-eng/widgets").exists()

    def test_add_blank_entry_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("learnings", "add", "acme-eng/widgets", "   ")

    def test_add_unresolvable_ref_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("learnings", "add", "https://example.com/not-a-forge-path", "x")

    def test_add_resolves_by_pull_request_url(self) -> None:
        result = cast(
            "LearningsResult",
            call_command("learnings", "add", "https://github.com/acme-eng/widgets/pull/7", "lesson"),
        )
        assert result["repo_slug"] == "acme-eng/widgets"


class LearningsEditTest(TestCase):
    def test_edit_replaces_full_field_via_editor(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/widgets", content="old")
        with patch("teatree.core.management.commands.learnings.click.edit", return_value="new full body"):
            result = cast("LearningsResult", call_command("learnings", "edit", "acme-eng/widgets"))
        row = ProjectLearning.objects.get(repo_slug="acme-eng/widgets")
        assert row.content == "new full body"
        assert result["content"] == "new full body"

    def test_edit_aborted_leaves_content_untouched(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/widgets", content="keep me")
        with patch("teatree.core.management.commands.learnings.click.edit", return_value=None):
            call_command("learnings", "edit", "acme-eng/widgets")
        row = ProjectLearning.objects.get(repo_slug="acme-eng/widgets")
        assert row.content == "keep me"

    def test_edit_creates_row_when_absent(self) -> None:
        with patch("teatree.core.management.commands.learnings.click.edit", return_value="seeded"):
            call_command("learnings", "edit", "acme-eng/widgets")
        assert ProjectLearning.objects.get(repo_slug="acme-eng/widgets").content == "seeded"

    def test_edit_unresolvable_ref_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("learnings", "edit", "https://example.com/not-a-forge-path")
