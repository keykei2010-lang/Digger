"""Local UI for inspecting and recovering files from raw disk images."""
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_STORED

HERE = Path(__file__).resolve().parent
ENGINES = Path(os.environ.get("RECOVERY_BIN_DIR", "/workspace/.testdisk-dev/build/src"))
WINDOWS = os.name == "nt"
if WINDOWS and "RECOVERY_BIN_DIR" not in os.environ:
    ENGINES = HERE / "engines"


def engine_path(name):
    return ENGINES / (name + "_win.exe" if WINDOWS else name)


SESSION = secrets.token_hex(16)
RUNS = tempfile.TemporaryDirectory(prefix="digger-")
JOBS = {}
LOCK = threading.Lock()
PREVIEW = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}


def sample_image(path):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    colors = [(56, 197, 161), (71, 109, 190), (234, 158, 88)]
    with path.open("wb") as disk:
        disk.truncate(8 * 1024 * 1024)
        for index, (red, green, blue) in enumerate(colors, 1):
            pixels = b"".join(
                b"\0" + b"".join(bytes(((red + x) % 256, (green + y) % 256, blue))
                                  for x in range(160))
                for y in range(100)
            )
            png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 160, 100, 8, 2, 0, 0, 0))
                   + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
            disk.seek(index * 1024 * 1024)
            disk.write(png)


def run_job(job, on_progress=None):
    folder = job["folder"]
    commands = {
        "inspect": [str(engine_path("testdisk")), "/list", "source.img"],
        "recover": [str(engine_path("photorec")), "/d", "files",
                    "/cmd", "source.img", "partition_none,wholespace,search"],
    }
    try:
        env = {**os.environ, "TERM": "xterm", "LC_ALL": "C"}
        if WINDOWS:
            folder.joinpath("63").mkdir(exist_ok=True)
            shutil.copyfile(ENGINES / "63" / "cygwin", folder / "63" / "cygwin")
            env.update(TERM="cygwin", TERMINFO=".")
        with (folder / "output.log").open("wb") as log:
            process = subprocess.Popen(commands[job["mode"]], cwd=folder, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       env=env, creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
            with LOCK:
                job["process"] = process
                if job["status"] == "stopping":
                    process.terminate()
            if on_progress is None:
                code = process.wait()
            else:
                while True:
                    try:
                        code = process.wait(timeout=0.35)
                        break
                    except subprocess.TimeoutExpired:
                        on_progress()
                on_progress()
        with LOCK:
            job["status"] = ("stopped" if job["status"] == "stopping"
                             else "complete" if code == 0 else "failed")
            if code and job["status"] == "failed":
                job["error"] = f"The recovery engine exited with status {code}. See the engine log."
    except OSError as exc:
        with LOCK:
            job.update(status="failed", error=str(exc))


def files_in(job):
    return [path for path in sorted(job["folder"].glob("files.*/*"))
            if path.is_file() and not path.is_symlink() and path.name != "report.xml"]


def file_record(path):
    return {"id": f"{path.parent.name}/{path.name}", "name": path.name,
            "size": path.stat().st_size, "type": path.suffix.lower().lstrip("."),
            "preview": path.suffix.lower().lstrip(".") in PREVIEW}


class Handler(BaseHTTPRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403)
            return False
        return True

    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, mime, attachment=None):
        with path.open("rb") as stream:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            if attachment:
                self.send_header("Content-Disposition", f"attachment; filename=\x22{attachment}\x22")
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile)

    def route(self):
        return urlsplit(self.path).path.strip("/").split("/")

    def do_GET(self):
        route = self.route()
        if route in ([""], ["app.css"], ["app.js"]):
            path = HERE / ("index.html" if route == [""] else route[0])
            self.send_file(path, mimetypes.guess_type(path)[0])
            return
        if route == ["api", "session"]:
            self.send_json({"token": SESSION,
                            "engines": {name: os.access(engine_path(name), os.X_OK)
                                        for name in ("testdisk", "photorec")}})
            return
        if len(route) >= 3 and route[:2] == ["api", "jobs"]:
            job = JOBS.get(route[2])
            if job is None:
                self.send_error(404)
                return
            if len(route) == 3:
                with LOCK:
                    status, error = job["status"], job.get("error")
                log = job["folder"] / "output.log"
                details = ""
                if log.exists():
                    with log.open("rb") as stream:
                        stream.seek(max(0, os.fstat(stream.fileno()).st_size - 16384))
                        details = stream.read(16384).decode(errors="replace")
                self.send_json({"status": status, "error": error, "name": job["name"],
                                "files": [file_record(path) for path in files_in(job)]
                                if status in {"complete", "stopped", "failed"} else [],
                                "details": details})
                return
            if len(route) == 6 and route[3] in {"preview", "download"}:
                path = next((path for path in files_in(job)
                             if route[4:] == [path.parent.name, path.name]), None)
                if path and route[3] == "preview" and path.suffix.lower().lstrip(".") in PREVIEW:
                    self.send_file(path, mimetypes.guess_type(path)[0])
                    return
                if path and route[3] == "download":
                    self.send_file(path, "application/octet-stream", path.name)
                    return
        self.send_error(404)

    def read_length(self):
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return -1

    def do_POST(self):
        if self.headers.get("X-Recovery-Session") != SESSION:
            self.send_json({"error": "Invalid session. Reload the app and try again."}, 403)
            return
        route = self.route()
        if route == ["api", "jobs"]:
            self.start_job()
            return
        if len(route) == 4 and route[:2] == ["api", "jobs"] and route[2] in JOBS:
            job = JOBS[route[2]]
            if route[3] == "stop":
                with LOCK:
                    if job["status"] == "running":
                        job["status"] = "stopping"
                        if job.get("process"):
                            job["process"].terminate()
                self.send_json({"ok": True})
                return
            if route[3] == "export" and job["status"] in {"complete", "stopped", "failed"}:
                self.export(job)
                return
        self.send_error(404)

    def start_job(self):
        mode = self.headers.get("X-Recovery-Mode")
        engine = {"inspect": "testdisk", "recover": "photorec"}.get(mode)
        length = self.read_length()
        sample = self.headers.get("X-Recovery-Sample") == "1"
        if not engine or (not sample and length <= 0):
            self.send_json({"error": "Choose a disk image and a scan mode."}, 400)
            return
        if not os.access(engine_path(engine), os.X_OK):
            self.send_json({"error": f"The {engine} binary is unavailable."}, 503)
            return
        if not sample and length > shutil.disk_usage(RUNS.name).free // 2:
            self.send_json({"error": "The image is too large for the available workspace storage."}, 413)
            return
        key = secrets.token_hex(12)
        folder = Path(RUNS.name) / key
        folder.mkdir()
        image = folder / "source.img"
        try:
            if sample:
                sample_image(image)
            else:
                with image.open("wb") as destination:
                    while length > 0:
                        data = self.rfile.read(min(length, 1024 * 1024))
                        if not data:
                            raise OSError("The upload ended early.")
                        destination.write(data)
                        length -= len(data)
            image.chmod(0o400)
        except OSError as exc:
            shutil.rmtree(folder)
            self.send_json({"error": str(exc)}, 400)
            return
        name = "Sample disk image" if sample else self.headers.get("X-Recovery-Name", "Disk image")
        JOBS[key] = job = {"status": "running", "folder": folder, "name": name, "mode": mode}
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        self.send_json({"id": key}, 201)

    def export(self, job):
        length = self.read_length()
        try:
            if not 0 < length <= 1024 * 1024:
                raise ValueError()
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError()
            selected = set(values)
        except ValueError:
            self.send_json({"error": "Invalid selection."}, 400)
            return
        paths = [path for path in files_in(job) if file_record(path)["id"] in selected]
        if not paths:
            self.send_json({"error": "Select files to export."}, 400)
            return
        with tempfile.TemporaryDirectory(dir=job["folder"]) as bundle:
            archive = Path(bundle) / "digger-recovered.zip"
            with ZipFile(archive, "w", compression=ZIP_STORED) as output:
                for path in paths:
                    name = path.name if re.fullmatch(r"[A-Za-z0-9_.-]+", path.name) else "recovered-file"
                    output.write(path, f"{path.parent.name}/{name}")
            self.send_file(archive, "application/zip", archive.name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Open http://127.0.0.1:{server.server_port} in your browser.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
