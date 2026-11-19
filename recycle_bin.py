"""Read copies of files retained by the current user's Windows Recycle Bin."""
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


def original_path(record):
    with record.open("rb") as stream:
        data = stream.read(MAX_RECORD + 1)
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


def scan_recycle_bin(folder=None, bins=None):
    scope = PureWindowsPath(folder) if folder else None
    for bin_path in current_user_bins() if bins is None else bins:
        try:
            records = [record for record in bin_path.iterdir() if record.name.startswith("$I")]
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
        for record in records:
            try:
                if not ordinary(record):
                    continue
                original = original_path(record)
                if original is None:
                    continue
                if scope and not (original.is_relative_to(scope) or scope.is_relative_to(original)):
                    continue
                payload = record.with_name("$R" + record.name[2:])
                for source in payload_files(payload):
                    target = original.joinpath(*source.relative_to(payload).parts)
                    if scope is None or target.is_relative_to(scope):
                        yield source, target
            except (OSError, UnicodeError, ValueError):
                # The Recycle Bin can change while a scan is running.
                continue
