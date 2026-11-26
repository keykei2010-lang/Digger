"""Run with the extracted ZIP's runtime/python.exe, passing its Digger folder."""
import hashlib
import lzma
from pathlib import Path
import shutil
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from ntfs_recovery import extract, scan, seekable_preview

with tempfile.TemporaryDirectory() as name:
    directory = Path(name)
    image = directory / "fixture.img"
    with lzma.open(root / "recovery-check.img.xz") as compressed, image.open("wb") as raw:
        shutil.copyfileobj(compressed, raw)
    items = list(scan(str(image), None, {"status": "running"}))
    candidate = next(entry for entry in items if entry.original.name == "Important.bin")
    assert candidate.st_size == 12032 and candidate.sequence is not None
    with seekable_preview(candidate, 32768) as stream:
        stream.seek(201)
        preview = stream.read(789)
    extracted = directory / "recovered.bin"
    extract(candidate, extracted)
    data = extracted.read_bytes()
    assert preview == data[201:990]
    assert hashlib.sha256(data).hexdigest() == "833d518b36e9b97849398241881ea06f37c70ecb2d7b1c6d2af78c44ea2cb973"
print("Packaged NTFS scan, byte-range preview and SHA-256 recovery passed.")
