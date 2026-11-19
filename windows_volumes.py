"""Resolve volume identities and guard live recovery destinations."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path, PureWindowsPath


@lru_cache(maxsize=1)
def kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.GetVolumeNameForVolumeMountPointW.argtypes = api.GetVolumePathNameW.argtypes
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    ctypes.c_void_p]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    return api


def volume_path(path):
    buffer = ctypes.create_unicode_buffer(32768)
    if not kernel().GetVolumePathNameW(str(path), buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def source_volume(path):
    folder = selected_scope(str(path))
    parent = next((Path(item) for item in (folder, *folder.parents) if Path(item).is_dir()), None)
    if parent is None:
        raise OSError(f"The drive {folder.drive} is unavailable.")
    resolved = parent.resolve(strict=True)
    root = PureWindowsPath(volume_path(resolved))
    if root != PureWindowsPath(folder.anchor) or resolved.drive.casefold() != folder.drive.casefold():
        raise OSError("Choose a folder on a local volume with a drive letter. Mounted folders are unsupported.")
    return "\\\\.\\" + folder.drive


def physical_disks(path):
    class Extent(ctypes.Structure):
        _fields_ = [("disk", wintypes.DWORD), ("start", ctypes.c_longlong), ("length", ctypes.c_longlong)]

    class Extents(ctypes.Structure):
        _fields_ = [("count", wintypes.DWORD), ("items", Extent * 256)]

    api = kernel()
    name = ctypes.create_unicode_buffer(1024)
    if not api.GetVolumeNameForVolumeMountPointW(volume_path(path), name, len(name)):
        raise ctypes.WinError(ctypes.get_last_error())
    handle = api.CreateFileW(name.value.rstrip("\\"), 0, 7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    extents, count = Extents(), wintypes.DWORD()
    try:
        if not api.DeviceIoControl(handle, 0x560000, None, 0, ctypes.byref(extents),
                                   ctypes.sizeof(extents), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        length = extents.count
        if not 1 <= length <= len(extents.items) or count.value < Extents.items.offset + length * ctypes.sizeof(Extent):
            raise OSError("Windows did not return complete physical disk information.")
        return {extent.disk for extent in extents.items[:length]}
    finally:
        api.CloseHandle(handle)


def require_separate_disk(source, destination):
    root = source[-2:] + "\\"
    if physical_disks(root) & physical_disks(destination):
        raise OSError("Choose a recovery destination on a different physical disk.")


def local_volumes():
    """Return mounted fixed and removable volumes for the source picker."""
    api = kernel()
    api.GetLogicalDrives.restype = wintypes.DWORD
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    mask = api.GetLogicalDrives()
    for index in range(26):
        root = chr(65 + index) + ":\\"
        if not mask & (1 << index):
            continue
        kind = api.GetDriveTypeW(root)
        if kind not in (2, 3):
            continue
        label, filesystem = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(32)
        if api.GetVolumeInformationW(root, label, len(label), None, None, None,
                                     filesystem, len(filesystem)):
            yield root, label.value or ("Removable drive" if kind == 2 else "Local drive"), filesystem.value


def selected_scope(value):
    """Normalize a drive letter or an absolute DOS folder path."""
    text = value.strip().strip('"')
    if len(text) == 2 and text[1] == ":":
        text += "\\"
    folder = PureWindowsPath(text)
    if (len(folder.drive) != 2 or not folder.drive[0].isascii()
            or not folder.drive[0].isalpha() or not folder.is_absolute()
            or any(part in ("..", ".") or ":" in part for part in folder.parts[1:])):
        raise ValueError("Enter a Windows drive or absolute folder path, such as C:\\ or D:\\Photos.")
    return folder


def is_elevated():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())
