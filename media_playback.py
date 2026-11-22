"""Serve a selected sector stream to the Windows media player on loopback."""
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import subprocess
import threading


def play_sector_video(entry):
    suffix = Path(entry.name).suffix
    route = "/" + secrets.token_urlsafe(32) + "/preview" + suffix

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, send):
            if self.path != route:
                self.send_error(404)
                return
            size = entry.st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested)
                if not match or not any(match.groups()):
                    self.send_error(416)
                    return
                left, right = match.groups()
                start = int(left) if left else max(0, size - int(right))
                end = min(end, int(right)) if left and right else end
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            try:
                with entry.open() as source:
                    source.seek(start)
                    self.send_response(206 if requested else 200)
                    self.send_header("Content-Type", mimetypes.guess_type(entry.name)[0] or "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(end - start + 1))
                    if requested:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    remaining = end - start + 1
                    while send and remaining:
                        data = source.read(min(remaining, 256 * 1024))
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
            except OSError:
                self.close_connection = True

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}{route}"
            binary = Path(__file__).with_name("engines") / "preview.exe"
            result = subprocess.run([str(binary), url, entry.name], check=False)
            if result.returncode:
                raise OSError("Windows Media Foundation could not play this format. You can still view its thumbnail or recover it for another player.")
        finally:
            server.shutdown()
