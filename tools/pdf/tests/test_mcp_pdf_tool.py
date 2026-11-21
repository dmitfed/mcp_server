import errno
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mcp_pdf_tool import read_pdf


class PdfToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {"MCP_PDF_ROOT": str(self.root)}))
        self.context = SimpleNamespace(grants=frozenset({"pdf.read"}))

    def write_pdf(self, *page_texts, password=None):
        writer = PdfWriter()
        for text in page_texts:
            page = writer.add_blank_page(width=300, height=200)
            if not text:
                continue
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/F1"): DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }),
                }),
            })
            content = DecodedStreamObject()
            content.set_data(b"BT /F1 14 Tf 20 150 Td (" + text + b") Tj ET")
            page.replace_contents(content)
        if password is not None:
            writer.encrypt(password)
        writer.write(self.root / "sample.pdf")

    def test_returns_page_numbers_and_continuation(self):
        self.write_pdf(b"First page", b"Second page")

        first = read_pdf({"file": "sample.pdf", "max_pages": 1}, self.context)
        self.assertEqual(first, {
            "file": "sample.pdf",
            "page_count": 2,
            "pages": [{"page": 1, "text": "First page", "truncated": False}],
            "next_page": 2,
        })
        # JSON Schema также принимает числа вроде 2.0, если они целые по значению.
        last = read_pdf({"file": "sample.pdf", "start_page": 2.0}, self.context)
        self.assertEqual(last["pages"], [{"page": 2, "text": "Second page", "truncated": False}])
        self.assertIsNone(last["next_page"])

    def test_returns_empty_text_and_marks_truncation(self):
        self.write_pdf(b"", b"12345678")
        with patch("mcp_pdf_tool.MAX_CHARS_PER_PAGE", 5):
            result = read_pdf({"file": "sample.pdf"}, self.context)
        self.assertEqual(result["pages"], [
            {"page": 1, "text": "", "truncated": False},
            {"page": 2, "text": "12345", "truncated": True},
        ])

    def test_requires_grant_before_opening_file(self):
        denied = SimpleNamespace(grants=frozenset())
        with self.assertRaises(PermissionError):
            read_pdf({"file": "missing.pdf"}, denied)

    def test_rejects_path_outside_documents_and_symbolic_link(self):
        self.write_pdf(b"First page")
        (self.root / "link.pdf").symlink_to(self.root / "sample.pdf")
        with self.assertRaises(ValueError):
            read_pdf({"file": "../sample.pdf"}, self.context)
        with self.assertRaises(OSError) as error:
            read_pdf({"file": "link.pdf"}, self.context)
        self.assertEqual(error.exception.errno, errno.ELOOP)

    def test_rejects_encrypted_pdf(self):
        self.write_pdf(b"Protected text", password="test")
        with self.assertRaisesRegex(ValueError, "PDF без шифрования"):
            read_pdf({"file": "sample.pdf"}, self.context)

    def test_rejects_oversized_pdf_and_missing_page(self):
        self.write_pdf(b"First page")
        with patch("mcp_pdf_tool.MAX_PDF_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "размером до 8 МиБ"):
                read_pdf({"file": "sample.pdf"}, self.context)
        with self.assertRaisesRegex(ValueError, "страница находится за пределами PDF"):
            read_pdf({"file": "sample.pdf", "start_page": 2}, self.context)
