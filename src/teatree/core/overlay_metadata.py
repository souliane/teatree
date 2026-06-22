"""Overlay MR/PR metadata hooks — the ``OverlayBase.metadata`` composition unit.

Split out of :mod:`teatree.core.overlay` ("split by concern", #1983) so the
MR/PR-metadata concern is named by its own file. ``OverlayBase`` composes one
``OverlayMetadata`` instance as its ``metadata`` attribute; an overlay
subclasses it to PRODUCE a canonical PR title, validate title/description, and
declare mandatory description sections (#312).

The constant-returning default hooks carry inline ``# noqa: PLR6301`` (and
``ARG002`` where a default ignores a param) for the same reason the former
``overlay.py`` per-file-ignore did: they are instance methods by contract — an
overlay overrides each as an instance method — so ruff's "could be static" /
"unused arg" suggestions conflict with the extension-point contract.
"""

from teatree.types import SkillMetadata, ToolCommand, ValidationResult

__all__ = ["OverlayMetadata"]


class OverlayMetadata:
    def validate_pr(self, title: str, description: str) -> ValidationResult:
        """Reject a non-conforming MR title/description (#1540, #312).

        Title and first line must match the effective ``mr_title_regex``; the
        description must carry a What/Why header plus every section declared in
        :meth:`get_required_description_sections`. A real gate, not a no-op.
        """
        from teatree.config import get_effective_settings  # noqa: PLC0415
        from teatree.core.mr_metadata import validate_mr_metadata  # noqa: PLC0415

        errors = validate_mr_metadata(
            title,
            description,
            get_effective_settings().mr_title_regex,
            required_sections=self.get_required_description_sections(),
        )
        return {"errors": errors, "warnings": []}

    def get_required_description_sections(self) -> list[str]:  # noqa: PLR6301
        """MR-description sections required beyond What/Why (#312); default none.

        An overlay declares mandatory sections (e.g. ``["Configuration"]``); the
        generator emits them and :meth:`validate_pr` flags any missing.
        """
        return []

    def get_description_section_defaults(self) -> dict[str, str]:  # noqa: PLR6301
        """Default body the generator writes under a missing required section (#312).

        Maps a section header to its default text so a thin commit ships a
        meaningful default, not an empty header. Default empty.
        """
        return {}

    def build_pr_title(self, *, branch: str, subject: str, body: str, issue_url: str) -> str:  # noqa: PLR6301, ARG002
        """Produce the PR title from structured data instead of copying the subject.

        Default returns ``subject``. An overlay enforcing a title grammar
        overrides this to assemble a canonical title from ``branch`` /
        ``subject`` / ``issue_url`` so a non-canonical subject never reaches the MR.
        """
        return subject

    def get_followup_repos(self) -> list[str]:  # noqa: PLR6301
        return []

    def get_skill_metadata(self) -> SkillMetadata:  # noqa: PLR6301
        return {}

    def get_ci_project_path(self) -> str:  # noqa: PLR6301
        return ""

    def get_e2e_config(self) -> dict[str, str]:  # noqa: PLR6301
        return {}

    def detect_variant(self) -> str:  # noqa: PLR6301
        return ""

    def get_tool_commands(self) -> list[ToolCommand]:  # noqa: PLR6301
        return []

    def get_issue_title(self, url: str) -> str:  # noqa: PLR6301, ARG002
        return ""
