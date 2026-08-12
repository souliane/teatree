"""The diff-scope gate: a blocking finding must cite a file the PR touches (#4251).

A cold reviewer that probes the branch checkout instead of the merge result
reads whatever ``main`` did to a file since the branch was cut and reports it
as a regression of the branch. These pin the three-valued changed-file set
(known / unavailable), the blocking-severity classifier, the path matching,
and the refusal text's escape hatch.
"""

from dataclasses import dataclass

from teatree.core.backend_protocols import CHANGED_PATHS_UNAVAILABLE
from teatree.core.modelkit.diff_scope import (
    ChangedFileSet,
    is_blocking_severity,
    out_of_scope_blocking_findings,
    out_of_scope_refusal,
)


@dataclass(frozen=True, slots=True)
class _Finding:
    """Duck-typed stand-in for ``teatree.core.models.Finding`` (severity + file)."""

    severity: str
    file: str
    summary: str = "s"


class TestBlockingSeverity:
    def test_recognises_the_severity_words_reviewers_actually_write(self) -> None:
        assert is_blocking_severity("blocker")
        assert is_blocking_severity("HIGH")
        assert is_blocking_severity(" Critical ")
        assert is_blocking_severity("major")

    def test_a_non_blocking_or_unknown_severity_never_blocks(self) -> None:
        assert not is_blocking_severity("nit")
        assert not is_blocking_severity("minor")
        assert not is_blocking_severity("")
        assert not is_blocking_severity("suggestion")


class TestChangedFileSet:
    def test_an_empty_fetch_is_unavailable_not_a_no_op_diff(self) -> None:
        assert not ChangedFileSet.known([]).available

    def test_the_unavailable_sentinel_path_is_unavailable(self) -> None:
        assert not ChangedFileSet.known([CHANGED_PATHS_UNAVAILABLE]).available

    def test_a_real_list_is_available_and_normalised(self) -> None:
        changed = ChangedFileSet.known(["./docs/a.md", "b/src/x.py"])
        assert changed.available
        assert changed.paths == ("docs/a.md", "src/x.py")


class TestOutOfScopeDetection:
    def test_a_blocking_finding_on_an_untouched_file_is_out_of_scope(self) -> None:
        findings = [_Finding("high", "src/teatree/core/modelkit/phase_tools.py")]
        changed = ChangedFileSet.known(["skills/review/SKILL.md"])
        assert out_of_scope_blocking_findings(findings, changed) == tuple(findings)

    def test_a_blocking_finding_on_a_changed_file_is_in_scope(self) -> None:
        findings = [_Finding("blocker", "skills/review/SKILL.md")]
        changed = ChangedFileSet.known(["skills/review/SKILL.md"])
        assert out_of_scope_blocking_findings(findings, changed) == ()

    def test_a_non_blocking_finding_on_an_untouched_file_is_left_alone(self) -> None:
        findings = [_Finding("nit", "src/teatree/paths.py")]
        changed = ChangedFileSet.known(["docs/a.md"])
        assert out_of_scope_blocking_findings(findings, changed) == ()

    def test_a_pr_level_finding_cites_no_file_so_it_is_never_out_of_scope(self) -> None:
        findings = [_Finding("blocker", "")]
        changed = ChangedFileSet.known(["docs/a.md"])
        assert out_of_scope_blocking_findings(findings, changed) == ()

    def test_a_repo_relative_citation_matches_a_longer_changed_path(self) -> None:
        findings = [_Finding("high", "core/modelkit/phase_tools.py")]
        changed = ChangedFileSet.known(["src/teatree/core/modelkit/phase_tools.py"])
        assert out_of_scope_blocking_findings(findings, changed) == ()

    def test_a_line_suffixed_citation_still_matches_its_changed_path(self) -> None:
        findings = [_Finding("high", "src/teatree/paths.py:286")]
        changed = ChangedFileSet.known(["src/teatree/paths.py"])
        assert out_of_scope_blocking_findings(findings, changed) == ()

    def test_a_partial_component_is_not_a_match(self) -> None:
        findings = [_Finding("high", "src/other_paths.py")]
        changed = ChangedFileSet.known(["src/paths.py"])
        assert len(out_of_scope_blocking_findings(findings, changed)) == 1

    def test_an_unavailable_changed_set_never_fires_the_gate(self) -> None:
        findings = [_Finding("blocker", "src/teatree/paths.py")]
        assert out_of_scope_blocking_findings(findings, ChangedFileSet.unavailable()) == ()


class TestRefusalText:
    def test_the_refusal_names_the_finding_and_the_merge_tree_escape(self) -> None:
        findings = [_Finding("high", "src/teatree/core/modelkit/phase_tools.py")]
        changed = ChangedFileSet.known(["skills/review/SKILL.md"])
        message = out_of_scope_refusal(findings, changed, merge_result_retake=False)
        assert "src/teatree/core/modelkit/phase_tools.py" in message
        assert "t3 review merge-tree" in message

    def test_an_attested_merge_result_retake_clears_the_refusal(self) -> None:
        findings = [_Finding("high", "src/teatree/paths.py")]
        changed = ChangedFileSet.known(["docs/a.md"])
        assert out_of_scope_refusal(findings, changed, merge_result_retake=True) == ""

    def test_an_in_scope_finding_set_produces_no_refusal(self) -> None:
        findings = [_Finding("blocker", "docs/a.md")]
        changed = ChangedFileSet.known(["docs/a.md"])
        assert out_of_scope_refusal(findings, changed, merge_result_retake=False) == ""
