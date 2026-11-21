import errno
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from mcp_zip_tools import extract_zip


class ZipToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.inbox = self.root / "inbox"
        self.output = self.root / "output"
        self.inbox.mkdir()
        self.output.mkdir()
        self.enterContext(patch.dict("os.environ", {"MCP_ZIP_ROOT": str(self.root)}))
        self.context = SimpleNamespace(grants=frozenset({"zip.extract"}))

    def write_zip(self, *entries):
        with ZipFile(self.inbox / "sample.zip", "w", compression=ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)

    def test_extracts_files_to_a_new_directory_for_each_call(self):
        self.write_zip(
            ("readme.txt", b"readme"),
            ("empty/", b""),
            ("docs/notes.txt", b"notes"),
        )

        first = extract_zip({"archive": "sample.zip"}, self.context)
        second = extract_zip({"archive": "sample.zip"}, self.context)

        self.assertNotEqual(first["directory"], second["directory"])
        for result in (first, second):
            self.assertEqual(result["files"], ["docs/notes.txt", "readme.txt"])
            directory = self.root / result["directory"]
            self.assertEqual(directory.parent.parent, self.output)
            self.assertEqual((directory / "docs/notes.txt").read_bytes(), b"notes")
            self.assertTrue((directory / "empty").is_dir())

    def test_requires_grant_before_opening_archive(self):
        denied = SimpleNamespace(grants=frozenset())
        with self.assertRaises(PermissionError):
            extract_zip({"archive": "missing.zip"}, denied)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_rejects_path_outside_inbox_and_symbolic_link(self):
        self.write_zip(("readme.txt", b"readme"))
        (self.inbox / "link.zip").symlink_to(self.inbox / "sample.zip")
        with self.assertRaises(ValueError):
            extract_zip({"archive": "../sample.zip"}, self.context)
        with self.assertRaises(OSError) as error:
            extract_zip({"archive": "link.zip"}, self.context)
        self.assertEqual(error.exception.errno, errno.ELOOP)

    def test_removes_partial_extraction_after_invalid_member(self):
        link = ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for invalid in ("../outside.txt", "/outside.txt", link):
            with self.subTest(invalid=invalid):
                self.write_zip(("valid.txt", b"valid"), (invalid, b"invalid"))
                with self.assertRaises(ValueError):
                    extract_zip({"archive": "sample.zip"}, self.context)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_rejects_archive_above_entry_and_output_limits(self):
        self.write_zip(("one.txt", b"one"), ("two.txt", b"two"))
        for limit, value in (("MAX_ENTRIES", 1), ("MAX_OUTPUT_BYTES", 5)):
            with self.subTest(limit=limit), patch(f"mcp_zip_tools.{limit}", value):
                with self.assertRaisesRegex(ValueError, "Превышен лимит распаковки"):
                    extract_zip({"archive": "sample.zip"}, self.context)
                self.assertEqual(list(self.output.iterdir()), [])
