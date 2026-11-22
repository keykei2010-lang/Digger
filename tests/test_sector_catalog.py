import tempfile
import unittest
import zlib
from pathlib import Path

from sector_catalog import parse_file


class SectorCatalogTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        self.source = self.folder / "disk.img"
        self.source.write_bytes(b"end" + bytes(1021) + b"start" + bytes(507))
        self.data = b"startend"
        self.fields = ["bin", "8", "0", str(zlib.crc32(self.data)), "", "1024,5", "0,3"]

    def entry(self):
        return parse_file(self.fields, str(self.source), None, self.source.stat().st_size, 512)

    def test_preview_and_save_across_nonconsecutive_extents(self):
        item = self.entry()
        with item.open() as preview:
            preview.seek(3)
            self.assertEqual(preview.read(4), b"rten")
            preview.seek(-3, 2)
            self.assertEqual(preview.read(), b"end")
        self.assertEqual(list(self.folder.iterdir()), [self.source])
        output = self.folder / "selected.bin"
        item.save(output)
        self.assertEqual(output.read_bytes(), self.data)

    def test_source_changed_does_not_leave_partial_or_final_file(self):
        item = self.entry()
        with self.source.open("r+b") as source:
            source.write(b"bad")
        with self.assertRaisesRegex(OSError, "changed since discovery"):
            item.save(self.folder / "selected.bin")
        self.assertEqual(list(self.folder.iterdir()), [self.source])

    def test_rejects_extent_outside_source_and_invalid_length(self):
        for invalid in ("1535,3", "0,1"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "outside the source"):
                self.fields[-1] = invalid
                self.entry()
