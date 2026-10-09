"""Attachment-to-Markdown conversion — wraps the optional markitdown extra."""

import importlib.util
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teatree.backends.markdown_conversion import (
    INSTALL_HINT,
    MarkdownConversionError,
    MarkdownConverter,
    MarkdownConverterUnavailableError,
)

MARKITDOWN_INSTALLED = importlib.util.find_spec("markitdown") is not None


def _install_fake_markitdown(monkeypatch: pytest.MonkeyPatch, mock_class: MagicMock) -> None:
    """Inject a fake ``markitdown`` module exposing ``MarkItDown = mock_class``."""
    module = types.ModuleType("markitdown")
    module.MarkItDown = mock_class
    monkeypatch.setitem(sys.modules, "markitdown", module)


def _hide_markitdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import markitdown`` to raise ImportError regardless of install state."""
    monkeypatch.setitem(sys.modules, "markitdown", None)


class TestMarkdownConverter:
    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            MarkdownConverter().convert_file(tmp_path / "absent.pdf")

    def test_absent_markitdown_raises_unavailable_with_install_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sample = tmp_path / "spec.xlsx"
        sample.write_bytes(b"stub")
        _hide_markitdown(monkeypatch)
        with pytest.raises(MarkdownConverterUnavailableError) as exc:
            MarkdownConverter().convert_file(sample)
        assert str(exc.value) == INSTALL_HINT
        assert "markitdown[pdf,docx,xlsx,pptx]" in INSTALL_HINT

    def test_successful_conversion_returns_markdown(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sample = tmp_path / "spec.xlsx"
        sample.write_bytes(b"stub")
        instance = MagicMock()
        instance.convert_local.return_value = MagicMock(markdown="# Sheet1\n\n| A | B |")
        mock_class = MagicMock(return_value=instance)
        _install_fake_markitdown(monkeypatch, mock_class)

        result = MarkdownConverter().convert_file(sample)

        assert result == "# Sheet1\n\n| A | B |"
        instance.convert_local.assert_called_once_with(str(sample))

    def test_plugins_are_disabled_and_no_llm_client_is_wired(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Untrusted input never triggers third-party plugins nor an LLM call."""
        sample = tmp_path / "spec.pdf"
        sample.write_bytes(b"stub")
        instance = MagicMock()
        instance.convert_local.return_value = MagicMock(markdown="text")
        mock_class = MagicMock(return_value=instance)
        _install_fake_markitdown(monkeypatch, mock_class)

        MarkdownConverter().convert_file(sample)

        _, kwargs = mock_class.call_args
        assert kwargs == {"enable_plugins": False}

    def test_conversion_failure_is_wrapped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sample = tmp_path / "spec.bin"
        sample.write_bytes(b"stub")
        instance = MagicMock()
        instance.convert_local.side_effect = RuntimeError("no converter for .bin")
        mock_class = MagicMock(return_value=instance)
        _install_fake_markitdown(monkeypatch, mock_class)

        with pytest.raises(MarkdownConversionError, match=r"Could not convert spec\.bin"):
            MarkdownConverter().convert_file(sample)


def _minimal_xlsx(path: Path) -> None:
    """Write a tiny valid .xlsx (one sheet, a 2x2 grid) without external deps."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Pricing" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Item</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Price</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Widget</t></is></c>'
        '<c r="B2"><v>42</v></c></row>'
        "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


@pytest.mark.skipif(not MARKITDOWN_INSTALLED, reason="markitdown extra not installed")
class TestRealMarkitdownConversion:
    """Exercise the genuine markitdown library when the extra is installed."""

    def test_real_xlsx_renders_a_markdown_table(self, tmp_path: Path) -> None:
        sample = tmp_path / "pricing.xlsx"
        _minimal_xlsx(sample)
        markdown = MarkdownConverter().convert_file(sample)
        assert "Item" in markdown
        assert "Price" in markdown
        assert "Widget" in markdown
