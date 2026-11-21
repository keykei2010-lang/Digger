"""Correct only extensions that disagree with a recognizable file header."""
from pathlib import Path

HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
EXTENSION_FAMILIES = {
    ".jpg": {".jpeg", ".jpe", ".jfif"},
    ".tif": {".tiff", ".dng", ".cr2", ".nef", ".nrw", ".arw", ".sr2", ".srf", ".pef",
             ".orf", ".rw2", ".3fr", ".iiq"},
    ".png": {".apng"},
    ".wav": {".wave"},
    ".aif": {".aiff", ".aifc"},
    ".avi": {".divx"},
    ".m4a": {".m4b", ".m4p"},
    ".heif": {".heic", ".hif"},
    ".heic": {".heif", ".hif"},
    ".jp2": {".jpx", ".jpf"},
    ".ogg": {".oga", ".ogv", ".ogx", ".opus", ".spx"},
    ".gz": {".tgz", ".svgz", ".gzip"},
    ".rar": {".cbr"},
    ".zip": {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm", ".dotx", ".xltx", ".potx",
             ".odt", ".ods", ".odp", ".jar", ".war", ".epub", ".apk", ".ipa", ".kmz", ".xpi",
             ".appx", ".msix", ".cbz", ".pages", ".numbers", ".key", ".whl", ".vsdx", ".xps", ".3mf"},
}


def bmff_extension(data):
    position = 0
    while position + 8 <= len(data):
        length = int.from_bytes(data[position:position+4], "big")
        box = data[position+4:position+8]
        header = 8
        if length == 1 and position + 16 <= len(data):
            length = int.from_bytes(data[position+8:position+16], "big")
            header = 16
        end = position + length
        if length < header or end > len(data):
            return None
        if box == b"ftyp":
            payload = data[position+header:end]
            if len(payload) < 8 or len(payload) % 4:
                return None
            major = payload[:4]
            compatible = {payload[i:i+4] for i in range(8, len(payload), 4)}
            if major in {b"avif", b"avis"}:
                return ".avif"
            if major in HEIC_BRANDS:
                return ".heic"
            if major in {b"mif1", b"msf1"}:
                if compatible & {b"avif", b"avis"}:
                    return ".avif"
                return ".heic" if compatible & HEIC_BRANDS else ".heif"
            # QuickTime and MP4 share a container family. Keep their existing names
            # when their brands alone cannot establish an incompatible media type.
            return {b"CRX ": ".cr3", b"jp2 ": ".jp2", b"M4A ": ".m4a"}.get(major)
        if box not in (b"free", b"skip", b"wide"):
            return None
        position = end
    return None


def detected_extension(data):
    if data.startswith(b"\xff\xd8\xff") and len(data) >= 4 and 0xc0 <= data[3] <= 0xfe:
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 13:
        return ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12:
        return {b"WEBP": ".webp", b"WAVE": ".wav", b"AVI ": ".avi"}.get(data[8:12])
    if data.startswith((b"II*\x00", b"MM\x00*")) and len(data) >= 8:
        return ".tif"
    if data.startswith(b"BM") and len(data) >= 26 and int.from_bytes(data[14:18], "little") in {12, 40, 52, 56, 64, 108, 124}:
        return ".bmp"
    if data.startswith((b"%PDF-1.", b"%PDF-2.")):
        return ".pdf"
    for signature, suffix in (
        (b"PK\x03\x04", ".zip"), (b"7z\xbc\xaf'\x1c", ".7z"), (b"Rar!\x1a\x07", ".rar"),
        (b"\x1f\x8b\x08", ".gz"), (b"fLaC", ".flac"), (b"OggS\x00", ".ogg"),
        (b"\x00\x00\x00\x0cjP  \r\n\x87\n", ".jp2"),
    ):
        if data.startswith(signature):
            return suffix
    if data.startswith(b"FORM") and data[8:12] in (b"AIFF", b"AIFC"):
        return ".aif"
    return bmff_extension(data)


def unique_path(path):
    candidate = path
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        index += 1
    return candidate


def preferred_extension(data, current):
    detected = detected_extension(data)
    if detected and current not in EXTENSION_FAMILIES.get(detected, ()):
        return detected
    return current


def correct_extension(path):
    """Rename complete output in its own directory, preserving aliases and bytes."""
    try:
        current = path.suffix.lower()
        with path.open("rb") as stream:
            extension = preferred_extension(stream.read(8192), current)
        if current != extension:
            path = path.rename(unique_path(path.with_suffix(extension)))
    except OSError:
        # A blocked rename must leave the original file available.
        pass
    return path
