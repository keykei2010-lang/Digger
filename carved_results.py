"""Read PhotoRec's completion records as it closes and validates carved files."""
from pathlib import Path
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
                        if event == "end" and node.tag == "fileobject":
                            self.add(node)
                            self.root.remove(node)
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
        self.seen.add(path)
        self.job["matches"].append((path, None))
