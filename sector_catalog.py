"""Discover sector extents and read selected candidates directly from the source."""
from bisect import bisect_right
from contextlib import ExitStack
from dataclasses import dataclass
from io import BufferedReader, RawIOBase
import os
from pathlib import Path
import re
import subprocess
import zlib

from ntfs_recovery import identity, tool
from recovered_format import preferred_extension
from recovery_output import atomic_output
from server import ENGINES, LOCK


@dataclass(frozen=True)
class SectorFile:
    source: str
    fingerprint: tuple | None
    name: str
    st_size: int
    st_mtime: int
    checksum: int
    runs: tuple
    alignment: int

    def open(self, budget=None):
        return BufferedReader(ExtentReader(self, budget), buffer_size=65536)

    def save(self, target):
        checksum = 0
        with self.open() as stream, atomic_output(target) as stage:
            with stage.open("wb") as out:
                while chunk := stream.read(1024 * 1024):
                    out.write(chunk)
                    checksum = zlib.crc32(chunk, checksum)
                if out.tell() != self.st_size or checksum != self.checksum:
                    raise OSError("The source bytes changed since discovery. Run a fresh scan.")
        return "Expected bytes read"


class ExtentReader(RawIOBase):
    def __init__(self, entry, budget):
        super().__init__()
        self.stream = None
        if entry.fingerprint is not None and identity(entry.source) != entry.fingerprint:
            raise OSError("The source image changed since the scan.")
        self.entry = entry
        self.budget = budget
        self.position = 0
        self.ends = []
        total = 0
        for _, length in entry.runs:
            total += length
            self.ends.append(total)
        self.stream = open(entry.source, "rb", buffering=0)

    def close(self):
        if self.stream:
            self.stream.close()
        super().close()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2):
            raise ValueError("Invalid seek origin.")
        pos = (0, self.position, self.entry.st_size)[whence] + offset
        if pos < 0:
            raise ValueError("Invalid negative seek.")
        self.position = pos
        return pos

    def readinto(self, buffer):
        entry = self.entry
        length = min(len(buffer), max(0, entry.st_size - self.position))
        if self.budget is not None:
            if length > self.budget:
                raise OSError("This preview exceeds the read limit. Recover the selected file for full playback.")
            self.budget -= length
        view = memoryview(buffer)
        copied = 0
        while copied < length:
            index = bisect_right(self.ends, self.position)
            start, run_length = entry.runs[index]
            within = self.position - (self.ends[index] - run_length)
            amount = min(length - copied, run_length - within)
            physical = start + within
            align = entry.alignment
            skip = physical % align
            self.stream.seek(physical - skip)
            block = self.stream.read(((skip + amount + align - 1) // align) * align)
            if len(block) < skip + amount:
                raise OSError("A sector belonging to this file could no longer be read.")
            view[copied:copied + amount] = memoryview(block)[skip:skip + amount]
            copied += amount
            self.position += amount
        return copied


def parse_file(fields, source, fingerprint, size, sector):
    ext, length, timestamp, crc, header, *pairs = fields
    if not pairs or not re.fullmatch(r"[a-zA-Z0-9_+.\-]{1,48}", ext):
        raise ValueError("Invalid file entry in scan results.")
    runs = tuple(tuple(map(int, pair.split(","))) for pair in pairs)
    length = int(length)
    if (length <= 0 or any(start < 0 or count <= 0 or start + count > size for start, count in runs)
            or sum(count for _, count in runs) != length):
        raise ValueError("A scan result refers to bytes outside the source.")
    suffix = preferred_extension(bytes.fromhex(header), "." + ext.lower())
    name = f"f{runs[0][0] // sector:07d}{suffix}"
    return SectorFile(source, fingerprint, name, length, int(timestamp), int(crc), runs, sector)


def scan_sectors(job, source):
    source = str(source)
    live = source.startswith("\\\\.\\")
    fingerprint = None if live else identity(source)
    signatures = "fileopt,everything,disable,mov,enable," if job.get("movies_only") else ""
    area = "freespace" if job.get("only_free") else "wholespace"
    # Check every logical sector in a whole-volume scan, including surviving
    # files aligned to a previous filesystem's allocation units.
    args = [str(ENGINES / "catalog.exe"), "/cmd", "source.dd",
            f"partition_none,{signatures}{area},search"]
    size = sector = 0
    complete = False
    seen = set()
    with ExitStack() as stack:
        process = None
        bitmap = None
        if job.get("only_free"):
            bitmap = stack.enter_context(tool("icat", ["-i", "raw", "-f", "ntfs", source, "6"],
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
        try:
            process = subprocess.Popen(args, stdin=bitmap.stdout if bitmap else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env={**os.environ, "DIGGER_CATALOG_SOURCE": source},
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                       cwd=job["folder"])
            if bitmap:
                bitmap.stdout.close()
            with process.stdout:
                with LOCK:
                    job["process"] = process
                    if job["status"] == "stopping":
                        process.terminate()
                for line in process.stdout:
                    if job["status"] == "stopping":
                        break
                    if not line.startswith(b"DG"):
                        continue
                    tag, *fields = line.rstrip(b"\r\n").decode("ascii").split("\t")
                    if tag == "DGBEGIN":
                        size, sector = map(int, fields)
                        if size <= 0 or sector not in (512, 1024, 2048, 4096):
                            raise ValueError("The scan engine could not read the source geometry.")
                        job["volume_bytes"] = size
                    elif tag == "DGPOS":
                        job["sector_position"] = int(fields[0])
                    elif tag == "DGREADERR":
                        job["read_errors"] = True
                    elif tag == "DGFILE" and size:
                        item = parse_file(fields, source, fingerprint, size, sector)
                        if item.runs[0][0] not in seen:
                            seen.add(item.runs[0][0])
                            job["matches"].append((item, None))
                    elif tag == "DGDONE":
                        complete = True
            code = process.wait()
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if bitmap and (job["status"] == "stopping" or not complete) and bitmap.poll() is None:
                bitmap.kill()
            with LOCK:
                job.pop("process", None)
        if job["status"] == "stopping":
            return
        if bitmap and (bitmap.wait() or code == 7):
            raise OSError("The NTFS allocation bitmap could not be read. Choose Whole volume to search all sectors.")
        if code or not complete or not size:
            raise OSError(f"The sector scan stopped without confirming full coverage (engine exit {code}).")
        job["sector_matches"] = len(seen)
