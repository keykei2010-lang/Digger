"""Read PhotoRec's completion records as it closes and validates carved files."""
from pathlib import Path

from recovered_format import correct_extension
import re
from xml.etree.ElementTree import ParseError, XMLPullParser


class CarvedResults:
    def __init__(self, job):
        self.job = job
        self.report = job["folder"] / "files.1" / "report.xml"
        self.position = 0
        self.parser = XMLPullParser(events=("start", "end"))
        self.root = None
        self.seen = set()
        self.source_size = 0
        self.report_closed = False
        self.count = 0
        self.last_offset = 0

    def read(self):
        if self.parser is None:
            return
        try:
            with self.report.open("rb") as stream:
                stream.seek(self.position)
                while chunk := stream.read(65536):
                    self.position = stream.tell()
                    self.parser.feed(chunk)
                    for event, node in self.parser.read_events():
                        if self.root is None:
                            self.root = node
                        if event == "end" and node.tag == "image_size" and (node.text or "").isdecimal():
                            self.source_size = int(node.text)
                        if event == "end" and node is self.root:
                            self.report_closed = True
                        if event == "end" and node.tag == "fileobject":
                            self.add(node)
                            try:
                                self.root.remove(node)
                            except ValueError:
                                pass  # nested node; memory is released at exit
        except OSError:
            return
        except ParseError:
            # A complete run can still enumerate the output directory at exit.
            self.parser = None

    def add(self, node):
        name = node.findtext("filename")
        size = node.findtext("filesize", "")
        if not name or not size.isdecimal():
            return
        path = Path(name)
        if path.parent == Path("."):
            path = self.report.parent / path
        elif not path.is_absolute():
            path = self.job["folder"] / path
        path = path.resolve()
        if (path.parent.parent != self.job["folder"].resolve()
                or not re.fullmatch(r"files\.\d+", path.parent.name)
                or path in self.seen or not path.is_file() or path.stat().st_size != int(size)):
            return
        offsets = [int(value) for run in node.findall("byte_runs/byte_run")
                   if (value := run.get("img_offset", "")).isdecimal()]
        if offsets:
            self.last_offset = max(self.last_offset, max(offsets))
            self.job["last_sector_match"] = self.last_offset
        self.register(path)

    def verified_completion(self):
        try:
            with (self.job["folder"] / "photorec.log").open("rb") as stream:
                stream.seek(max(0, (self.job["folder"] / "photorec.log").stat().st_size - 65536))
                log = stream.read()
        except OSError:
            return False
        return (self.report_closed and self.source_size > 0
                and not (self.job["folder"] / "photorec.ses").exists()
                and re.search(rb"(?m)^Total: [0-9]+ files? found\r?$", log) is not None)

    def register(self, path):
        self.seen.add(path)
        corrected = correct_extension(path)
        self.seen.add(corrected)
        self.job["matches"].append((corrected, None))
        self.count += 1
