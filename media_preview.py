"""Decode media thumbnails from seekable sources with bounded reads."""
from pathlib import Path

from PIL import Image, UnidentifiedImageError

IMAGE_TYPES = {".png", ".gif", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".ico"}
VIDEO_FORMATS = dict.fromkeys((".mp4", ".mov", ".m4v", ".3gp"), "mov")
VIDEO_FORMATS.update(dict.fromkeys((".mkv", ".webm"), "matroska"))
VIDEO_FORMATS.update(dict.fromkeys((".ts", ".mts", ".m2ts"), "mpegts"))
VIDEO_FORMATS.update({".avi": "avi", ".wmv": "asf", ".mpg": "mpeg", ".mpeg": "mpeg"})
VIDEO_TYPES = set(VIDEO_FORMATS)


def decode_preview(source, suffix, bounds=(1600, 1600)):
    if isinstance(source, Path):
        with source.open("rb") as stream:
            return decode_preview(stream, suffix, bounds)
    if suffix in IMAGE_TYPES:
        try:
            with Image.open(source) as image:
                image.thumbnail(bounds)
                return image.convert("RGBA"), "Image decoded"
        except UnidentifiedImageError as exc:
            raise OSError("The available bytes do not contain a readable image header.") from exc
    if suffix not in VIDEO_FORMATS:
        return None, "Unchecked"
    try:
        import av
    except ImportError as exc:
        raise OSError("The bundled video decoder could not load. Extract the complete Digger ZIP.") from exc
    options = {"protocol_whitelist": "pipe", "enable_drefs": "0",
               "probesize": "4194304", "analyzeduration": "2000000"}
    try:
        with av.open(source, format=VIDEO_FORMATS[suffix], options=options, buffer_size=65536) as container:
            if not container.streams.video:
                raise OSError("No video stream was found.")
            stream = container.streams.video[0]
            stream.thread_count = 2
            for frame in container.decode(stream):
                scale = min(1, bounds[0] / frame.width, bounds[1] / frame.height)
                width, height = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
                return frame.reformat(width=width, height=height, format="rgb24").to_image(), "Video frame decoded"
    except av.FFmpegError as exc:
        raise OSError(f"The video decoder could not read a frame. {exc.strerror or type(exc).__name__}.") from exc
    raise OSError("No complete video frame was found.")
