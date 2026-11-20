"""Read copies of files retained by the current user's Windows Recycle Bin."""
from datetime import datetime
import ctypes
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import struct
import subprocess

MAX_RECORD = 24 + 4 + 32768 * 2
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SID = re.compile(r"S-1-(?:\d+-)+\d+", re.ASCII)


def current_user_bins():
    if os.name != "nt":
        raise OSError("Recycle Bin scans require Windows.")
    whoami = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
    identity = subprocess.check_output(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=8,
    )
    match = SID.search(identity.decode("ascii", errors="replace"))
    if not match:
        raise OSError("Windows did not report the current account identifier.")
    kernel = ctypes.windll.kernel32
    drives = kernel.GetLogicalDrives()
    for index in range(26):
        root = f"{chr(65 + index)}:\\"
        if drives & (1 << index) and kernel.GetDriveTypeW(ctypes.c_wchar_p(root)) in (2, 3):
            yield Path(root) / "$Recycle.Bin" / match.group()


def bin_metadata(data):
    path = decode_original_path(data)
    if path is None:
        return None, None
    ticks = struct.unpack_from("<Q", data, 16)[0]
    try:
        deleted = datetime.fromtimestamp((ticks - 116444736000000000) // 10000000) if ticks else None
    except (OverflowError, OSError, ValueError):
        deleted = None
    return path, deleted


def decode_original_path(data):
    if len(data) < 26 or len(data) > MAX_RECORD:
        return None
    version, _, _ = struct.unpack_from("<QQQ", data)
    if version == 1:
        encoded = data[24:]
    elif version == 2 and len(data) >= 28:
        units = struct.unpack_from("<I", data, 24)[0]
        if units == 0 or units * 2 > len(data) - 28:
            return None
        encoded = data[28:28 + units * 2]
    else:
        return None
    name = encoded.decode("utf-16-le", errors="strict").rstrip("\0")
    path = PureWindowsPath(name)
    if not re.fullmatch(r"[A-Za-z]:\\", path.anchor) or len(path.parts) < 2:
        return None
    for part in path.parts[1:]:
        if (part.rstrip(" .") != part or not part or re.search(r'[<>:"/\\|?*\x00-\x1f]', part)
                or PureWindowsPath(part).is_reserved()):
            return None
    return path


def ordinary(path):
    info = path.lstat()
    return not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & REPARSE_POINT


def payload_files(path):
    if not ordinary(path):
        return
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for folder, dirs, names in os.walk(path, followlinks=False):
        directory = Path(folder)
        dirs[:] = [name for name in dirs if ordinary(directory / name)]
        for name in names:
            child = directory / name
            if ordinary(child) and child.is_file():
                yield child


def scan_recycle_bin(folder=None, bins=None, cancelled=lambda: False):
    scope = PureWindowsPath(folder) if folder else None
    for bin_path in current_user_bins() if bins is None else bins:
        try:
            records = []
            for record in bin_path.iterdir():
                if cancelled():
                    return
                if record.name.startswith("$I"):
                    records.append(record)
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
        for record in records:
            if cancelled():
                return
            try:
                if not ordinary(record):
                    continue
                with record.open("rb") as stream:
                    original, deleted = bin_metadata(stream.read(MAX_RECORD + 1))
                if original is None:
                    continue
                if scope and not (original.is_relative_to(scope) or scope.is_relative_to(original)):
                    continue
                payload = record.with_name("$R" + record.name[2:])
                for source in payload_files(payload):
                    if cancelled():
                        return
                    target = original.joinpath(*source.relative_to(payload).parts)
                    if scope is None or target.is_relative_to(scope):
                        yield source, target, deleted
            except (OSError, UnicodeError, ValueError):
                # The Recycle Bin can change while a scan is running.
                continue

def emptied_items(bin_path, job, folder=None):
    """Pair a deleted $R payload with surviving $I metadata within one user's bin."""
    from ntfs_recovery import preview_bytes, scan
    from windows_volumes import source_volume

    scope = PureWindowsPath(bin_path)
    names = {}
    pending = {}
    for entry in scan(source_volume(scope), scope, job, include_bin_metadata=True):
        parts = entry.original.relative_to(scope).parts
        if not parts:
            continue
        token = parts[0]
        key = token[2:].casefold()
        if token[:2].upper() == "$I" and len(parts) == 1:
            try:
                with preview_bytes(entry, MAX_RECORD) as data:
                    original, deleted = bin_metadata(data.read())
            except (OSError, UnicodeError, ValueError):
                original = None
            if original and original.drive.casefold() == scope.drive.casefold():
                names[key] = (original, deleted)
                for payload, tail in pending.pop(key, []):
                    location = original.joinpath(*tail)
                    if folder is None or location.is_relative_to(folder):
                        yield payload, location, deleted
        elif token[:2].upper() == "$R":
            if key in names:
                original, deleted = names[key]
                location = original.joinpath(*parts[1:])
                if folder is None or location.is_relative_to(folder):
                    yield entry, location, deleted
            else:
                pending.setdefault(key, []).append((entry, parts[1:]))
    if folder is None:
        for items in pending.values():
            for entry, _ in items:
                yield entry, entry.original, None
