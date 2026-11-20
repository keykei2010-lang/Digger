"""Publish a completed file atomically, without replacing an existing path."""
from contextlib import contextmanager
import os
import shutil
import stat
from pathlib import Path, PureWindowsPath
import tempfile


def output_name(name):
    """Make one recovered path component safe to create on Windows.

    NTFS can hold names Windows itself cannot create (trailing dots or spaces,
    device names such as CON). Recover them under a harmless name instead of
    skipping the file.
    """
    clean = name.rstrip(" .") or "_"
    if PureWindowsPath(clean).is_reserved():
        clean = "_" + clean
    return clean


@contextmanager
def atomic_output(destination, metadata_from=None):
    handle, name = tempfile.mkstemp(prefix=".digger-", suffix=".partial", dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("r+b") as complete:
            os.fsync(complete.fileno())
        if metadata_from is not None:
            shutil.copystat(metadata_from, temporary)
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        if os.name == "nt" and temporary.exists() and metadata_from is not None:
            temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
        temporary.unlink(missing_ok=True)
