"""Run the WIP's structural repair engine and publish a separate verified copy."""
from contextlib import ExitStack
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from media_preview import decode_preview
from recovered_format import unique_path
from recovery_output import atomic_output, output_name

REPAIR_FORMATS = {".mp4": "mp4", ".mov": "mp4", ".m4v": "mp4", ".3gp": "mp4",
                  ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".avi": "avi"}


def repair_media(source, folder, reference=None, *, runtime=None):
    source = Path(source)
    suffix = source.suffix.lower()
    kind = REPAIR_FORMATS.get(suffix)
    if not kind:
        raise ValueError("Choose an MP4, MOV, M4V, 3GP, AVI, JPEG, or PNG file.")
    if not source.is_file():
        raise ValueError("Choose an existing media file.")
    if kind != "mp4" and source.stat().st_size > 128 * 1024**2:
        raise ValueError("JPEG, PNG, and AVI repair supports files up to 128 MiB.")
    if reference and reference.stat().st_size > 64 * 1024**2:
        raise ValueError("Choose a reference clip or photo under 64 MiB.")
    root = Path(__file__).resolve().parent
    output = unique_path(Path(folder) / output_name(source.stem + "-repaired" + suffix))
    with ExitStack() as stack:
        if runtime is None:
            # The compressed bundled runtime keeps the portable ZIP under the
            # repository's single-file limit.
            runtime = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="digger-repair-"))) / "node.exe"
            with lzma.open(root / "engines" / "repair-node.exe.xz") as archive, runtime.open("wb") as binary:
                shutil.copyfileobj(archive, binary)
        pending = stack.enter_context(atomic_output(output))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in ("NODE_OPTIONS", "NODE_PATH")}
        result = subprocess.run([str(runtime), str(root / "repair" / "cli.cjs"), kind, str(source),
                                 str(pending), str(reference or "")], env=env, capture_output=True,
                                timeout=1800, creationflags=flags, check=True)
        repaired = json.loads(result.stdout)
        if not repaired["ok"]:
            raise ValueError(repaired.get("note") or "The repair engine could not reconstruct this file.")
        preview, _ = decode_preview(pending, suffix, (256, 192))
        if preview is None:
            raise ValueError("The repaired file did not produce a readable preview.")
    return output, repaired.get("note") or "Structural repair complete."
