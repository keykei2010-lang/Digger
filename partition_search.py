"""Read-only boot-sector search for missing partition entries on 512-byte media."""
import re

BLOCK = 8 * 1024 * 1024
# FAT type strings are optional. Match a plausible jump and 512-byte BPB.
BOOT_HINT = re.compile(rb"(?:NTFS    |EXFAT   )|(?:\xeb.\x90|\xe9..).{8}\x00\x02", re.DOTALL)


def readable_blocks(disk, start, length, job):
    if job["status"] == "stopping":
        return
    try:
        disk.seek(start)
        data = disk.read(length)
    except OSError:
        data = b""
    if len(data) == length:
        yield start, data
        return
    # Preserve readable regions on both sides of a damaged sector.
    if length <= 512:
        job["unreadable_sectors"] = job.get("unreadable_sectors", 0) + 1
        return
    split = max(512, length // 1024 * 512)
    yield from readable_blocks(disk, start, split, job)
    yield from readable_blocks(disk, start + split, length - split, job)


def boot_offsets(source, size, job):
    with open(source, "rb", buffering=0) as disk:
        for start in range(0, size, BLOCK):
            if job["status"] == "stopping":
                return
            for block_start, data in readable_blocks(disk, start, min(BLOCK, size - start), job):
                for match in BOOT_HINT.finditer(data):
                    position = match.start()
                    if position % 512 not in (0, 3):
                        continue
                    offset = position - position % 512
                    boot = data[offset:offset + 512]
                    if boot[-2:] != b"\x55\xaa" or boot[0] not in (0xeb, 0xe9):
                        continue
                    if boot[3:11] == b"EXFAT   ":
                        expected = int.from_bytes(boot[64:72], "little")
                        if expected and expected != (block_start + offset) // 512:
                            continue
                        sector = 1 << boot[108] if boot[108] <= 12 else 0
                        count = int.from_bytes(boot[72:80], "little")
                    else:
                        sector = int.from_bytes(boot[11:13], "little")
                        if boot[3:11] == b"NTFS    ":
                            count = int.from_bytes(boot[40:48], "little")
                        else:
                            cluster = boot[13]
                            if not cluster or cluster & (cluster - 1) or boot[16] not in (1, 2):
                                continue
                            count = int.from_bytes(boot[19:21], "little") or int.from_bytes(boot[32:36], "little")
                    if sector == 512 and 16 <= count <= (size - block_start - offset) // 512:
                        yield (block_start + offset) // 512
            job["sector_position"] = start + min(BLOCK, size - start)


def check_image_sector_size(source):
    """Reject a recognizable 4Kn GPT disk image before interpreting its offsets."""
    with open(source, "rb") as image:
        header = image.read(4104)
    if header[4096:4104] == b"EFI PART" and header[512:520] != b"EFI PART":
        raise OSError("This image appears to use 4096-byte logical sectors. Indexed partition scans require 512-byte sectors.")
