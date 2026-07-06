"""Pure path classifier for customer-display impact (#1967).

``classify_paths`` decides whether a changed-file set could impact what is
displayed to the customer, given an overlay's non-impacting glob rules. The
defining property is FAIL-CLOSED: a path matching no non-impacting rule, and the
empty-diff case, resolve to ``True`` — an unanticipated path is presumed
display-impacting so the mandatory-E2E gate cannot be silently skipped by a path
the rules did not anticipate. Only a set whose every path is explicitly
non-impacting resolves to ``False``.
"""

from teatree.core.evidence.customer_display_impact import classify_paths

_NON_IMPACTING = ("*/test_*.py", "*/tests/*", "test_*.py", "*/migrations/*.py", "*.md", "tooling/*")


class TestImpactingPaths:
    def test_serializer_change_impacts(self) -> None:
        assert classify_paths(["app/api/serializers.py"], _NON_IMPACTING) is True

    def test_view_change_impacts(self) -> None:
        assert classify_paths(["app/views.py"], _NON_IMPACTING) is True

    def test_frontend_component_impacts(self) -> None:
        assert classify_paths(["web/src/app/loan.component.ts"], _NON_IMPACTING) is True

    def test_template_impacts(self) -> None:
        assert classify_paths(["app/templates/email.html"], _NON_IMPACTING) is True


class TestNonImpactingPaths:
    def test_test_only_change_does_not_impact(self) -> None:
        assert classify_paths(["app/tests/test_views.py"], _NON_IMPACTING) is False

    def test_migration_only_change_does_not_impact(self) -> None:
        assert classify_paths(["app/migrations/0002_add_field.py"], _NON_IMPACTING) is False

    def test_docs_only_change_does_not_impact(self) -> None:
        assert classify_paths(["README.md", "docs/guide.md"], _NON_IMPACTING) is False

    def test_tooling_only_change_does_not_impact(self) -> None:
        assert classify_paths(["tooling/lint.py"], _NON_IMPACTING) is False


class TestFailClosed:
    def test_unknown_path_presumed_impacting(self) -> None:
        # Matches no non-impacting glob → fail closed to True.
        assert classify_paths(["app/business/pricing.py"], _NON_IMPACTING) is True

    def test_mixed_impacting_and_non_impacting_is_impacting(self) -> None:
        assert classify_paths(["app/tests/test_x.py", "app/views.py"], _NON_IMPACTING) is True

    def test_mixed_unknown_and_non_impacting_is_impacting(self) -> None:
        # One non-impacting + one unknown → the unknown forces fail-closed True.
        assert classify_paths(["README.md", "app/business/pricing.py"], _NON_IMPACTING) is True

    def test_empty_diff_is_impacting(self) -> None:
        # An empty changed-file list is ambiguous, never proof of no impact →
        # fail closed so the gate is not skipped by a diff that failed to
        # enumerate.
        assert classify_paths([], _NON_IMPACTING) is True
