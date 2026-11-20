"""Decode a bounded preview without blocking the desktop UI."""
import io
import os
from pathlib import Path
import subprocess

from PIL import Image

IMAGE_TYPES = {".png", ".gif", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".wmv", ".3gp", ".ts", ".mts", ".m2ts"}
FFMPEG = Path(__file__).resolve().parent / "runtime" / "ffmpeg.exe"


def decode_preview(path, suffix=None):
    suffix = suffix or path.suffix.lower()
    if suffix in IMAGE_TYPES:
        with Image.open(path) as source:
            source.thumbnail((1600, 1600))
            return source.convert("RGBA"), "Image decoded"
    if suffix not in VIDEO_TYPES:
        return None, "Unchecked"

    in_memory = isinstance(path, io.BytesIO)
    command = [str(FFMPEG), "-nostdin", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist", "pipe" if in_memory else "file,pipe", "-threads", "2", "-i", "pipe:0" if in_memory else str(path),
               "-map", "0:v:0", "-frames:v", "1", "-vf",
               "scale=1280:720:force_original_aspect_ratio=decrease", "-an",
               "-threads", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    result = subprocess.run(command, input=path.getvalue() if in_memory else None,
                            stdin=None if in_memory else subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=12, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    with Image.open(io.BytesIO(result.stdout)) as frame:
        return frame.convert("RGBA"), "Video frame decoded"
