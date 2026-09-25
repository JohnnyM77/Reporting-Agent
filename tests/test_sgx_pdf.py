"""Unit tests for sgx_pdf.py -- pure-function coverage (filename
sanitisation, PDF-magic sniffing, HTML link extraction). The actual
network fetch is exercised by the sgx_daily workflow run."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgx_pdf import (  # noqa: E402
    _safe_name,
    _extract_pdf_links,
    _is_pdf_response,
    PDF_MAGIC,
)


class TestSafeName:
    def test_basic_pdf_name_kept(self):
        assert _safe_name("2Q26_performance_summary.pdf") == "2Q26_performance_summary.pdf"

    def test_spaces_and_specials_become_underscores(self):
        assert _safe_name("DBS Half-Year Report 2026.pdf") == "DBS_Half-Year_Report_2026.pdf"

    def test_missing_pdf_extension_added(self):
        assert _safe_name("performance_summary").endswith(".pdf")

    def test_empty_falls_back(self):
        assert _safe_name("") == "attachment.pdf"

    def test_length_capped(self):
        long = "a" * 300 + ".pdf"
        assert len(_safe_name(long)) <= 200


class TestIsPdfResponse:
    class _Resp:
        def __init__(self, ct: str, content: bytes = b""):
            self.headers = {"Content-Type": ct}
            self.content = content

    def test_content_type_pdf(self):
        assert _is_pdf_response(self._Resp("application/pdf")) is True

    def test_magic_bytes_wins_even_if_ct_wrong(self):
        assert _is_pdf_response(
            self._Resp("application/octet-stream", content=PDF_MAGIC + b"anything")
        ) is True

    def test_html_is_not_pdf(self):
        assert _is_pdf_response(
            self._Resp("text/html; charset=utf-8", content=b"<html>")
        ) is False


class TestExtractPdfLinks:
    def test_direct_pdf_anchor(self):
        html = """
        <html><body>
            <a href="attachments/report.pdf">Half-Year Report</a>
        </body></html>
        """
        links = _extract_pdf_links(html, base_url="https://links.sgx.com/1.0.0/annc/x/")
        assert len(links) == 1
        assert links[0][0] == "https://links.sgx.com/1.0.0/annc/x/attachments/report.pdf"
        assert links[0][1] == "Half-Year Report"

    def test_multiple_pdfs_in_order(self):
        """The DBS half-year screenshot had 4 attached PDFs -- verify they
        come back in document order for the results card to pick the
        top-priority one."""
        html = """
        <div class="attachments">
            <a href="2Q26_performance_summary.pdf">Performance Summary</a>
            <a href="2Q26_CFO_presentation.pdf">CFO Presentation</a>
            <a href="2Q26_CEO_presentation.pdf">CEO Presentation</a>
            <a href="2Q26_press_statement.pdf">Press Statement</a>
        </div>
        """
        links = _extract_pdf_links(html, base_url="https://links.sgx.com/x/")
        names = [n for _, n in links]
        assert names == [
            "Performance Summary", "CFO Presentation",
            "CEO Presentation", "Press Statement",
        ]

    def test_fileopen_ashx_recognised(self):
        """SGX sometimes wraps PDFs behind a FileOpen.ashx proxy URL --
        should still be recognised."""
        html = '<a href="/FileOpen/report.ashx?FileID=abc">Report</a>'
        links = _extract_pdf_links(html, base_url="https://links.sgx.com/")
        assert len(links) == 1
        assert "FileOpen" in links[0][0]

    def test_non_pdf_links_ignored(self):
        html = """
        <a href="https://sgx.com/news">News page</a>
        <a href="mailto:ir@dbs.com">Email IR</a>
        <a href="#top">Back to top</a>
        <a href="javascript:void(0)">JS link</a>
        <a href="attachments/report.pdf">Report</a>
        """
        links = _extract_pdf_links(html, base_url="https://x/")
        assert len(links) == 1
        assert links[0][1] == "Report"

    def test_deduplicates_same_url(self):
        html = """
        <a href="report.pdf">A</a>
        <a href="report.pdf">B</a>
        """
        links = _extract_pdf_links(html, base_url="https://x/")
        assert len(links) == 1
