from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from ntfs_recovery import body_entry
from partition_search import boot_offsets, check_image_sector_size, readable_blocks


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

    def test_unlabeled_fat_boot_is_found(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "raw.img"
            boot = bytearray(512)
            boot[:3] = b"\xeb\x3c\x90"
            boot[11:13] = (512).to_bytes(2, "little")
            boot[13] = 4
            boot[16] = 2
            boot[19:21] = (32).to_bytes(2, "little")
            boot[-2:] = b"\x55\xaa"
            image = bytes(512) + boot + bytes(512 * 31)
            path.write_bytes(image)
            self.assertEqual(list(boot_offsets(path, len(image), {"status": "running"})), [1])

    def test_read_error_preserves_data_before_and_after_damaged_sector(self):
        class DamagedDisk(BytesIO):
            def read(self, size):
                start = self.tell()
                if start < 1024 and start + size > 512:
                    raise OSError("Fixture read error")
                return super().read(size)

        job = {"status": "running"}
        blocks = list(readable_blocks(DamagedDisk(bytes(2048)), 0, 2048, job))
        self.assertEqual([(offset, len(data)) for offset, data in blocks], [(0, 512), (1024, 1024)])
        self.assertEqual(job["unreadable_sectors"], 1)

    def test_recognizable_4kn_gpt_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "raw.img"
            path.write_bytes(bytes(4096) + b"EFI PART")
            with self.assertRaisesRegex(OSError, "4096-byte logical sectors"):
                check_image_sector_size(path)

    def test_allocated_lost_partition_file_requires_explicit_discovery(self):
        line = "0|/clip.mp4|42-128-2|r/r|0|0|120|0|0|0|0"
        self.assertIsNone(body_entry(line, "disk", 8, "Lost partition 8", None))
        entry = body_entry(line, "disk", 8, "Lost partition 8", None, include_existing=True)
        self.assertTrue(entry.allocated)
        file_with_suffix = body_entry(line.replace("clip.mp4", "clip.mp4 (deleted)"),
                                      "disk", 8, "Lost partition 8", None, include_existing=True)
        self.assertTrue(file_with_suffix.allocated)
        self.assertEqual(file_with_suffix.original.name, "clip.mp4 (deleted)")
