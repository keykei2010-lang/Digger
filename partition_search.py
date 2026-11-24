"""Read-only boot-sector search for missing partition entries on 512-byte media."""
import re

# Check recognizable primary boot sectors and let Sleuth Kit validate the filesystem.
SIGNATURE = re.compile(rb"NTFS    |EXFAT   |FAT(?:12|16|32)   ")
BLOCK = 8 * 1024 * 1024


def boot_offsets(source, size, job):
    with open(source, "rb", buffering=0) as disk:
        for start in range(0, size, BLOCK):
            if job["status"] == "stopping":
                return
            data = disk.read(min(BLOCK, size - start))
            if len(data) != min(BLOCK, size - start):
                raise OSError("A disk read stopped before the end of the partition search.")
            for match in SIGNATURE.finditer(data):
                position = match.start()
                if position % 512 not in (3, 54, 82):
                    continue
                offset = position - position % 512
                boot = data[offset:offset + 512]
                if boot[-2:] != b"\x55\xaa" or boot[0] not in (0xeb, 0xe9):
                    continue
                if boot[3:11] == b"EXFAT   ":
                    # The main and backup VBR share a partition offset. Check it
                    # where available for physical media and partitioned images.
                    expected = int.from_bytes(boot[64:72], "little")
                    if expected and expected != (start + offset) // 512:
                        continue
                    sector = 1 << boot[108] if boot[108] <= 12 else 0
                    count = int.from_bytes(boot[72:80], "little")
                else:
                    sector = int.from_bytes(boot[11:13], "little")
                    count = (int.from_bytes(boot[40:48], "little") if boot[3:11] == b"NTFS    "
                             else int.from_bytes(boot[19:21], "little") or int.from_bytes(boot[32:36], "little"))
                if sector == 512 and 16 <= count <= (size - start - offset) // 512:
                    yield (start + offset) // 512
            job["sector_position"] = start + len(data)
