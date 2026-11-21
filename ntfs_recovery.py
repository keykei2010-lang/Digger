"""Indexed, read-only undelete using the bundled Sleuth Kit command-line tools."""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from io import BufferedReader, BytesIO, RawIOBase
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
from tempfile import TemporaryFile
from threading import Thread, Timer
from recovery_output import atomic_output
from server import LOCK

TOOLS = Path(__file__).resolve().parent / "runtime" / "sleuthkit"
BODY_SUFFIX = " (deleted)"
ENTRY_ID = re.compile(r"[0-9]+-128-[0-9]+")


@dataclass(frozen=True)
class Candidate:
    source: str
    offset: int
    inode: str
    original: PureWindowsPath
    st_size: int
    st_mtime: int
    identity: tuple | None
    filesystem: str = "ntfs"
    sequence: int | None = None


def tool(name, arguments, **kwargs):
    executable = TOOLS / (name + ".exe") if os.name == "nt" else name
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen([str(executable), *arguments],
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                            **kwargs)


def identity(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def safe_name(text):
    # Trailing dots/spaces and device names are legal on NTFS, so they are kept
    # here and made safe when the file is written (recovery_output.output_name).
    return text not in {"", ".", ".."} and not re.search(r'[<>:"/\\|?*\x00-\x1f]', text)


def body_entry(line, source, offset, volume, fingerprint, filesystem="ntfs"):
    fields = line.rstrip("\r\n").rsplit("|", 9)
    if len(fields) != 10:
        return None
    header, inode, mode, _, _, size, _, modified, _, _ = fields
    marker, separator, name = header.partition("|/")
    has_sequence = filesystem == "ntfs" and re.fullmatch(r"seq:[0-9]{1,5}", marker)
    supported_inode = ENTRY_ID.fullmatch(inode) if filesystem == "ntfs" else re.fullmatch(r"[0-9]+", inode)
    if (not separator or (marker != "0" and not has_sequence) or not name.endswith(BODY_SUFFIX)
            or not supported_inode or not mode.startswith(("r/r", "-/r"))):
        return None
    parts = name[:-len(BODY_SUFFIX)].split("/")
    if not all(safe_name(part) for part in parts):
        return None
    try:
        size, modified = int(size), int(modified)
    except ValueError:
        return None
    if size < 0:
        return None
    prefix = volume + "\\" if volume.endswith(":") else volume
    sequence = int(marker[4:]) if has_sequence else None
    return Candidate(source, offset, inode, PureWindowsPath(prefix, *parts), size, modified, fingerprint, filesystem,
                     sequence)


def partition_offsets(source, job):
    with tool("mmls", ["-i", "raw", source], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        with LOCK:
            job["process"] = process
            if job["status"] == "stopping":
                process.terminate()
        try:
            output, _ = process.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        finally:
            with LOCK:
                job.pop("process", None)
    return sorted({int(match[1]) for line in output.decode("utf-8", "replace").splitlines()
                   if (match := re.match(r"^\s*\d+:\s+[0-9]+(?::[0-9]+)?\s+(\d+)\s+\d+\s+\d+", line))
                   and int(match[1]) > 0})


def filesystem_at(source, offset):
    with open(source, "rb") as image:
        image.seek(offset * 512)
        boot = image.read(512)
    if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
        return None
    if boot[3:11] == b"NTFS    ":
        return "ntfs"
    if boot[3:11] == b"EXFAT   ":
        return "exfat"
    # Some FAT formatters omit the informal FAT label in the boot sector.
    sector = int.from_bytes(boot[11:13], "little")
    cluster = boot[13]
    if (boot[0] in (0xeb, 0xe9) and sector in (512, 1024, 2048, 4096)
            and cluster and cluster & (cluster - 1) == 0 and boot[16] in (1, 2)):
        return "fat"
    return None


def scan(source, scope, job, include_bin_metadata=False):
    fingerprint = None if scope is not None else identity(source)
    offsets = [0] if scope is not None else [0, *partition_offsets(source, job)]
    readable = False
    messages = []
    seen = set()
    for offset in offsets:
        if job["status"] == "stopping":
            return
        filesystem = "ntfs" if scope is not None else filesystem_at(source, offset)
        if filesystem is None:
            continue
        errors = bytearray()
        arguments = ["-i", "raw", "-f", filesystem, "-o", str(offset), "-r", "-d", "-F", "-m", "/", source]
        if os.name == "nt" and filesystem == "ntfs":
            arguments.insert(0, "-Q")
        with tool("fls", arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            def drain_errors():
                for chunk in iter(lambda: process.stderr.read(4096), b""):
                    errors.extend(chunk[:max(0, 2048 - len(errors))])

            reader = Thread(target=drain_errors)
            reader.start()
            with LOCK:
                job["process"] = process
                if job["status"] == "stopping":
                    process.terminate()
            volume = scope.drive if scope is not None else (f"Partition-{offset}" if offset else "Image")
            for raw in process.stdout:
                if raw.startswith(b"MFT "):
                    current, total = map(int, raw.split()[1:])
                    job["records"] = current, total
                    continue
                row = body_entry(raw.decode("utf-8", "replace"), source, offset, volume, fingerprint, filesystem)
                if row is None:
                    continue
                if scope is not None and not row.original.is_relative_to(scope):
                    continue
                if not include_bin_metadata and scope is not None and row.original.parts[1:2] and row.original.parts[1].casefold() == "$recycle.bin":
                    # Cleared-bin payload records can survive under their Windows $R name.
                    if not any(part.casefold().startswith("$r") for part in row.original.parts[2:]):
                        continue
                key = row.offset, row.inode, row.original
                if key not in seen:
                    seen.add(key)
                    yield row
            code = process.wait()
            reader.join()
            with LOCK:
                job.pop("process", None)
        if not code:
            readable = True
        else:
            messages.append(errors.decode("utf-8", "replace").strip())
    if job["status"] == "stopping":
        return
    if readable and any(messages):
        # Results from the readable partitions are already delivered; report the rest.
        job.setdefault("warnings", []).append(
            "Some partitions could not be read and were skipped.\n" + next(m for m in messages if m))
    elif not readable:
        detail = next((message for message in messages if message), "")
        hint = ("The NTFS reader could not finish reading the selected volume."
                if scope is not None else "Choose a raw disk or volume image containing NTFS, FAT, or exFAT.")
        raise OSError("The filesystem scan did not finish. " + hint + ("\n" + detail if detail else ""))


def check_identity(entry):
    if entry.identity is not None and identity(entry.source) != entry.identity:
        raise OSError("The disk image changed after the scan. Scan it again.")


def ntfs_record(entry):
    inode, _, attribute_id = entry.inode.split("-")
    structured = os.name == "nt"
    arguments = ["-J" if structured else "-r", "-i", "raw", "-f", "ntfs", "-o", str(entry.offset),
                 entry.source, inode]
    with tool("istat", arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            output, errors = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            raise OSError("Timed out checking the deleted NTFS record.") from None
    if process.returncode:
        detail = errors[:2048].decode("utf-8", "replace").strip()
        raise OSError("The NTFS engine could not read the deleted record." + (f" {detail}" if detail else ""))
    virtual_name = ("$OrphanFiles" in entry.original.parts and
                    entry.original.name == f"OrphanFile-{inode}")
    if structured:
        return read_ntfs_record_status(entry, output, inode, attribute_id, virtual_name)
    fields = [line.strip() for line in output.splitlines()]
    if b"Allocated File" in fields or b"Allocated Directory" in fields:
        raise OSError("NTFS reports this record is in use. Rescan the volume to refresh the results.")
    if b"Not Allocated File" not in fields:
        raise OSError("The NTFS reader could not verify this is a deleted file.")
    if not virtual_name and not any(
        line.strip().decode("utf-8", "replace").casefold() == f"name: {entry.original.name}".strip().casefold()
        for line in output.splitlines()
    ):
        raise OSError("The deleted NTFS entry name changed. Scan the drive again.")
    attribute = f"128-{attribute_id}".encode()
    pattern = rb"^Type: \$DATA \(" + attribute + rb"\).*\bsize:\s*" + str(entry.st_size).encode() + rb"\b"
    index = next((i for i, line in enumerate(fields) if re.search(pattern, line)), None)
    if index is None:
        raise OSError("The deleted NTFS entry changed. Scan the drive again.")
    header = fields[index]
    if b" Non-Resident" not in header:
        return output, "Resident NTFS data"
    if any(flag in header for flag in (b"Compressed", b"Encrypted", b"Sparse")):
        return output, "Expected bytes read"
    runs = []
    for line in fields[index + 1:]:
        match = re.fullmatch(rb"\s*Starting address: (\d+), length: (\d+)\s*", line)
        if not match:
            if line.startswith(b"Type: "):
                break
            if line.strip():
                return output, "Expected bytes read"
            continue
        cluster, length = map(int, match.groups())
        if not cluster or not length:
            return output, "Expected bytes read"
        runs.append((cluster, length))
    return output, runs or "Expected bytes read"


def read_ntfs_record_status(entry, output, inode, attribute_id, virtual_name):
    fields = output.splitlines()
    header = re.fullmatch(rb"RECORD (\d+) (\d+) (allocated|deleted-file|other|unknown)",
                          fields[0] if fields else b"")
    if not header or int(header[1]) != int(inode):
        raise OSError("The NTFS reader could not verify this file's record status.")
    sequence = int(header[2])
    if entry.sequence is not None and sequence != entry.sequence:
        raise OSError(f"NTFS reused this record after the scan (sequence {entry.sequence} → {sequence}). "
                      "The current record cannot restore the listed file.")
    if header[3] == b"allocated":
        raise OSError("NTFS reports this record is in use. Rescan the volume to refresh the results.")
    if header[3] != b"deleted-file":
        raise OSError("The NTFS reader could not verify this is a deleted file.")
    if not virtual_name and not any(
        line[5:].decode("utf-8", "replace").casefold() == entry.original.name.casefold()
        for line in fields if line.startswith(b"NAME ")
    ):
        raise OSError("The deleted NTFS entry name changed. Scan the drive again.")
    attribute = next((line.split() for line in fields if line.startswith(b"DATA ")), ())
    if len(attribute) != 4 or attribute[1:3] != [attribute_id.encode(), str(entry.st_size).encode()]:
        raise OSError("The deleted NTFS data attribute changed. Scan the drive again.")
    if attribute[3] == b"resident":
        return output, "Resident NTFS data"
    if attribute[3] == b"unchecked":
        return output, "Expected bytes read"
    if attribute[3] == b"runs":
        runs = []
        for line in fields:
            if line.startswith(b"RUN "):
                run = re.fullmatch(rb"RUN ([1-9]\d*) ([1-9]\d*)", line)
                if not run:
                    raise OSError("The NTFS reader returned an invalid data run.")
                runs.append(tuple(map(int, run.groups())))
        if runs:
            return output, runs
    raise OSError("The NTFS reader could not verify the file's data layout.")


def bitmap_prefix(source, offset, limit):
    with tool("icat", ["-i", "raw", "-f", "ntfs", "-o", str(offset), source, "6"],
              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            return process.stdout.read(limit)
        finally:
            if process.poll() is None:
                process.kill()


@lru_cache(maxsize=1)
def image_bitmap(source, offset, fingerprint):
    return bitmap_prefix(source, offset, 32 * 1024 * 1024)


def contains_allocated(bitmap, first, count):
    end = first + count - 1
    left, right = first // 8, end // 8
    start_mask = (255 << (first % 8)) & 255
    end_mask = (1 << (end % 8 + 1)) - 1
    if left == right:
        return bool(bitmap[left] & start_mask & end_mask)
    return bool(bitmap[left] & start_mask or bitmap[right] & end_mask or any(bitmap[left + 1:right]))


def recovery_evidence(entry):
    check_identity(entry)
    if entry.filesystem != "ntfs":
        return None, "Expected bytes read"
    record, runs = ntfs_record(entry)
    if isinstance(runs, str):
        return record, runs
    limit = max((first + count + 7) // 8 for first, count in runs)
    if limit > 32 * 1024 * 1024:
        return record, "Expected bytes read"
    bitmap = (bitmap_prefix(entry.source, entry.offset, limit) if entry.identity is None
              else image_bitmap(entry.source, entry.offset, entry.identity))
    if len(bitmap) < limit:
        return record, "Expected bytes read"
    if any(contains_allocated(bitmap, first, count) for first, count in runs):
        raise OSError("NTFS marks one or more data clusters as allocated. The file may have been overwritten.")
    return record, "NTFS clusters free"


def ensure_current(entry):
    check_identity(entry)
    if entry.identity is None:
        ntfs_record(entry)


def stream_recovery(entry, target):
    before, evidence = recovery_evidence(entry)
    arguments = ["-r", "-i", "raw", "-f", entry.filesystem, "-o", str(entry.offset), entry.source, entry.inode]
    with TemporaryFile() as errors, tool("icat", arguments, stdout=subprocess.PIPE, stderr=errors) as process:
        remaining = entry.st_size
        try:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                if len(chunk) > remaining:
                    raise OSError("The recovered data exceeded the expected size.")
                target.write(chunk)
                remaining -= len(chunk)
            result = process.wait()
            if result or remaining:
                errors.seek(0)
                detail = errors.read(2048).decode("utf-8", "replace").strip()
                summary = f"The engine read {entry.st_size - remaining:,} of {entry.st_size:,} expected bytes."
                raise OSError(summary + (f" {detail}" if detail else f" Engine exit code: {result}."))
        finally:
            if process.poll() is None:
                process.kill()
    if entry.identity is None:
        after, evidence = recovery_evidence(entry)
        if before != after:
            raise OSError("The deleted record changed during recovery. Scan the drive again.")
    else:
        check_identity(entry)
    return evidence


class NtfsPreviewReader(RawIOBase):
    def __init__(self, process, size, budget):
        super().__init__()
        self.process = process
        self.size = size
        self.budget = budget
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        position = {os.SEEK_SET: 0, os.SEEK_CUR: self.position, os.SEEK_END: self.size}[whence] + offset
        if position < 0:
            raise OSError("Invalid media seek.")
        self.position = position
        return position

    def readinto(self, buffer):
        length = min(len(buffer), max(0, self.size - self.position), 512 * 1024)
        if length == 0:
            return 0
        if length > self.budget:
            raise OSError("Preview read limit reached. Recover the file to open it.")
        try:
            self.process.stdin.write(f"{self.position} {length}\n".encode("ascii"))
            self.process.stdin.flush()
            contents = self.process.stdout.read(length)
        except BrokenPipeError as exc:
            raise OSError("The NTFS reader could not read the preview data.") from exc
        if len(contents) != length:
            raise OSError("The NTFS reader could not read the preview data.")
        buffer[:length] = contents
        self.position += length
        self.budget -= length
        return length


@contextmanager
def seekable_preview(entry, budget):
    if os.name != "nt" or entry.filesystem != "ntfs":
        with preview_bytes(entry, budget) as view:
            yield view
        return
    before, _ = recovery_evidence(entry)
    arguments = ["-J", "-i", "raw", "-f", "ntfs", "-o", str(entry.offset), entry.source, entry.inode]
    with tool("icat", arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        def stop_reader():
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass
        timeout = Timer(18, stop_reader)
        timeout.daemon = True
        timeout.start()
        try:
            with BufferedReader(NtfsPreviewReader(process, entry.st_size, budget), 256 * 1024) as view:
                yield view
        finally:
            timeout.cancel()
            stop_reader()
    if entry.identity is None:
        after, _ = recovery_evidence(entry)
        if before != after:
            raise OSError("The NTFS record changed during preview. Scan the drive again.")
    else:
        check_identity(entry)


def preview_bytes(entry, limit):
    if entry.st_size > limit:
        raise OSError("This image is too large for an in-memory preview.")
    output = BytesIO()
    stream_recovery(entry, output)
    output.seek(0)
    return output


def extract(entry, output):
    with atomic_output(output) as staging:
        with staging.open("wb") as target:
            evidence = stream_recovery(entry, target)
        if entry.st_mtime > 0:
            try:
                os.utime(staging, (entry.st_mtime, entry.st_mtime))
            except (OSError, OverflowError, ValueError):
                pass
    return evidence
