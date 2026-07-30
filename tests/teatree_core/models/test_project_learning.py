"""``ProjectLearning`` — durable per-repo knowledge store, DB-placed (#2892)."""

import re

from django.db import connection
from django.test import TestCase

from teatree.core.models.project_learning import ProjectLearning


class ProjectLearningFieldTest(TestCase):
    def test_content_defaults_to_empty_string(self) -> None:
        row = ProjectLearning.objects.create(repo_slug="acme-eng/widgets")
        assert row.content == ""

    def test_content_persists_free_text(self) -> None:
        row = ProjectLearning.objects.create(repo_slug="acme-eng/widgets", content="CI needs FOO=1")
        row.refresh_from_db()
        assert row.content == "CI needs FOO=1"

    def test_repo_slug_is_unique(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/widgets")
        with self.assertRaises(Exception):  # noqa: B017, PT027 — IntegrityError, DB-backend-specific.
            ProjectLearning.objects.create(repo_slug="acme-eng/widgets")

    def test_table_present_in_db_schema(self) -> None:
        with connection.cursor() as cursor:
            tables = {t.name for t in connection.introspection.get_table_list(cursor)}
        assert "teatree_project_learning" in tables

    def test_str_includes_repo_slug(self) -> None:
        row = ProjectLearning.objects.create(repo_slug="acme-eng/widgets")
        assert str(row) == "project-learning<acme-eng/widgets>"


class ProjectLearningAppendTest(TestCase):
    def test_append_learning_prefixes_timestamp_block(self) -> None:
        row = ProjectLearning.objects.create(repo_slug="acme-eng/widgets")
        row.append_learning("de-CH locale only, never it-CH")
        row.refresh_from_db()
        assert re.fullmatch(
            r"\n\n\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] de-CH locale only, never it-CH",
            row.content,
        )

    def test_append_learning_is_additive_and_ordered(self) -> None:
        row = ProjectLearning.objects.create(repo_slug="acme-eng/widgets")
        row.append_learning("first")
        row.append_learning("second")
        row.refresh_from_db()
        first_at = row.content.index("first")
        second_at = row.content.index("second")
        assert first_at < second_at
        assert row.content.count("[") == 2

    def test_append_learning_rejects_blank(self) -> None:
        row = ProjectLearning.objects.create(repo_slug="acme-eng/widgets")
        with self.assertRaises(ValueError):  # noqa: PT027 — TestCase convention in this module.
            row.append_learning("   ")


class ProjectLearningManagerTest(TestCase):
    def test_content_for_slug_returns_empty_when_no_row(self) -> None:
        assert ProjectLearning.objects.content_for_slug("acme-eng/nope") == ""

    def test_content_for_slug_returns_recorded_content(self) -> None:
        ProjectLearning.objects.create(repo_slug="acme-eng/widgets", content="notes")
        assert ProjectLearning.objects.content_for_slug("acme-eng/widgets") == "notes"

    def test_record_for_slug_creates_row_on_first_call(self) -> None:
        row = ProjectLearning.objects.record_for_slug("acme-eng/widgets", "first lesson")
        assert row.repo_slug == "acme-eng/widgets"
        assert "first lesson" in row.content

    def test_record_for_slug_appends_to_existing_row(self) -> None:
        ProjectLearning.objects.record_for_slug("acme-eng/widgets", "first")
        row = ProjectLearning.objects.record_for_slug("acme-eng/widgets", "second")
        assert ProjectLearning.objects.filter(repo_slug="acme-eng/widgets").count() == 1
        assert "first" in row.content
        assert "second" in row.content
