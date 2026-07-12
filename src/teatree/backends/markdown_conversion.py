"""Convert binary attachments (PDF, XLSX, …) to Markdown for agent ingestion.

Wraps `markitdown <https://github.com/microsoft/markitdown>`_ (MIT) behind a
narrow, typed surface. markitdown is an **optional** dependency: install the
``markdown`` extra (``markitdown[pdf,docx,xlsx,pptx]``) to enable conversion.
When it is absent, :class:`MarkdownConverter` raises
:class:`MarkdownConverterUnavailableError` with an install hint instead of
crashing.

Converted output is treated as **untrusted data**: attachments are
attacker-influenceable, so callers emit the Markdown verbatim and never act on
instructions embedded inside it. markitdown's optional LLM/Azure integrations
are deliberately left unwired — image description is handled by Claude, not by
shipping attachment bytes to a third-party endpoint.
"""

from pathlib import Path
from typing import Protocol

INSTALL_HINT = (
    "markitdown is not installed. Install the optional 'markdown' extra to "
    "enable attachment conversion, e.g. `uv tool install --editable . "
    "--with 'markitdown[pdf,docx,xlsx,pptx]'` or `pip install "
    "'markitdown[pdf,docx,xlsx,pptx]'`."
)


class MarkdownConverterUnavailableError(RuntimeError):
    """Raised when markitdown (the optional 'markdown' extra) is not installed."""


class MarkdownConversionError(RuntimeError):
    """Raised when markitdown fails to convert a supported file."""


class _ConversionResult(Protocol):
    """The narrow slice of markitdown's ``DocumentConverterResult`` we read."""

    @property
    def markdown(self) -> str: ...  # pragma: no branch


class _MarkItDown(Protocol):
    """The narrow slice of markitdown's ``MarkItDown`` surface we call."""

    def convert_local(self, path: str) -> _ConversionResult: ...  # pragma: no branch


class MarkdownConverter:
    """Convert a local file to Markdown via markitdown's ``convert_local``.

    Plugins are disabled: only markitdown's vetted built-in converters run, so
    untrusted attachments cannot trigger third-party plugin code. No LLM client
    is wired, so attachment bytes never leave the machine.
    """

    def convert_file(self, path: Path) -> str:
        """Return the Markdown rendering of *path*.

        Raises :class:`FileNotFoundError` when *path* is missing,
        :class:`MarkdownConverterUnavailableError` when markitdown is not
        installed, and :class:`MarkdownConversionError` on a conversion
        failure (e.g. an unsupported format or a missing format-specific
        extra).
        """
        if not path.is_file():
            msg = f"File not found: {path}"
            raise FileNotFoundError(msg)
        converter = self._build_markitdown()
        try:
            result = converter.convert_local(str(path))
        except Exception as exc:
            msg = f"Could not convert {path.name} to Markdown: {exc}"
            raise MarkdownConversionError(msg) from exc
        return result.markdown

    @staticmethod
    def _build_markitdown() -> _MarkItDown:
        """Instantiate markitdown with plugins disabled and no LLM client."""
        try:
            from markitdown import MarkItDown  # noqa: PLC0415 — deferred: heavy/optional dep at call site
        except ImportError as exc:
            raise MarkdownConverterUnavailableError(INSTALL_HINT) from exc
        return MarkItDown(enable_plugins=False)
