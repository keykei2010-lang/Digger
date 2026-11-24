from pathlib import Path
import tempfile
import unittest

from ntfs_recovery import body_entry
from partition_search import boot_offsets


class PartitionTests(unittest.TestCase):
    def test_search_finds_unlisted_partition_and_skips_impossible_volume(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "raw.img"
            image = bytearray(512 * 80)
            boot = bytearray(512)
            boot[0] = 0xeb
            boot[3:11] = b"NTFS    "
            boot[11:13] = (512).to_bytes(2, "little")
            boot[40:48] = (40).to_bytes(8, "little")
            boot[-2:] = b"\x55\xaa"
            image[8*512:9*512] = boot
            image[72*512:73*512] = boot
            path.write_bytes(image)
            self.assertEqual(list(boot_offsets(path, len(image), {"status": "running"})), [8])

    def test_allocated_lost_partition_file_requires_explicit_discovery(self):
        line = "0|/clip.mp4|42-128-2|r/r|0|0|120|0|0|0|0"
        self.assertIsNone(body_entry(line, "disk", 8, "Lost partition 8", None))
        entry = body_entry(line, "disk", 8, "Lost partition 8", None, include_existing=True)
        self.assertTrue(entry.allocated)
