"""Portable desktop interface for the bundled Windows recovery engines."""
import csv
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import sys
from datetime import datetime
from pathlib import Path, PureWindowsPath
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from recovery_output import atomic_output
from ntfs_recovery import Candidate, extract as extract_ntfs, scan as scan_ntfs
from windows_volumes import require_separate_disk, source_volume

from media_preview import IMAGE_TYPES, VIDEO_TYPES, decode_preview
from recycle_bin import ordinary, scan_recycle_bin
from server import LOCK, RUNS, files_in, run_job, sample_image

if os.name == "nt":
    runtime = Path(sys.executable).parent
    os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")

BG = "#eff5f1"
DARK = "#122b2c"
GREEN = "#17634e"
MINT = "#a9efd0"
TEXT = "#152829"
MUTED = "#526764"
SORTS = ("Name", "Size", "File date", "Type", "Recoverability")
GROUPS = ("None", "Original folder", "Type", "File date", "Recoverability")
CHECKS = {
    "Preview failed": (0, "#a12f32"),
    "Unchecked": (1, "#655414"),
    "Deleted NTFS record": (1, "#655414"),
    "Deleted FAT record": (1, "#655414"),
    "Deleted exFAT record": (1, "#655414"),
    "Expected bytes read": (2, "#156047"),
    "Resident NTFS data": (2, "#156047"),
    "NTFS clusters free": (2, "#156047"),
    "File copied": (2, "#156047"),
    "Video frame decoded": (2, "#156047"),
    "Image decoded": (3, "#156047"),
}


def size_label(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


class RecoveryWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Digger")
        self.geometry("1050x720")
        self.minsize(760, 560)
        self.configure(bg=BG)
        self.mode = tk.StringVar(value="bin")
        self.search = tk.StringVar()
        self.format = tk.StringVar(value="All file types")
        self.source = None
        self.folder_source = None
        self.job = None
        self.saving = False
        self.save_events = queue.SimpleQueue()
        self.files = {}
        self.originals = {}
        self.materialized = {}
        self.metadata = {}
        self.checks = {}
        self.recovery_checks = {}
        self.sort = tk.StringVar(value="Name")
        self.direction = tk.StringVar(value="Ascending")
        self.group = tk.StringVar(value="None")
        self.preview_events = queue.SimpleQueue()
        self.preview_pending = None
        self.preview_active = False
        self.preview_key = None
        self.shown_count = 0
        self.preview_bitmap = None
        self.zoom = tk.DoubleVar(value=1)
        self.animate = tk.BooleanVar(value=os.environ.get("DIGGER_REDUCED_MOTION", os.environ.get("RELAY_REDUCED_MOTION")) != "1")
        self.events = queue.SimpleQueue()
        self.preview_image = None
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.after_idle(self.fade_in)
        self.after(150, self.refresh)

    def build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), foreground=TEXT)
        style.configure("Digger.TButton", background=GREEN, foreground="white", padding=(14, 8))
        style.map("Digger.TButton", background=[("disabled", "#81958c"), ("active", "#124c3d")],
                  foreground=[("disabled", "white")])
        style.configure("Treeview", rowheight=29, fieldbackground="white")
        style.map("Treeview", background=[("selected", "#d3eee2")], foreground=[("selected", DARK)])

        top = tk.Frame(self, bg=DARK)
        top.pack(fill="x")
        tk.Label(top, text="Digger", font=("Segoe UI", 16, "bold"), bg=DARK,
                 fg="white", padx=25, pady=15).pack(side="left")
        tk.Label(top, text="Offline desktop app", font=("Segoe UI", 10), bg=DARK,
                 fg=MINT, padx=25).pack(side="right")

        self.motion_button = ttk.Checkbutton(top, text="Animations", variable=self.animate,
                                            command=self.update_motion)
        self.motion_button.pack(side="right", padx=7)
        body = ttk.Frame(self, padding=(20, 16))
        body.pack(fill="both", expand=True)
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        self.scan_page = ttk.Frame(self.tabs, padding=20)
        self.results_page = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(self.scan_page, text="  New scan  ")
        self.tabs.add(self.results_page, text="  Scan results  ")
        self.build_scan()
        self.build_results()

    def build_scan(self):
        page = self.scan_page
        hero = tk.Frame(page, bg=DARK, padx=23, pady=19)
        hero.pack(fill="x", pady=(0, 20))
        tk.Label(hero, text="Find and recover deleted files", bg=DARK, fg="white",
                 font=("Segoe UI", 25, "bold")).pack(anchor="w")
        tk.Label(hero, text="Scan the Recycle Bin or select a disk image.", bg=DARK,
                 fg=MINT, font=("Segoe UI", 11)).pack(anchor="w", pady=(4, 0))

        modes = ttk.LabelFrame(page, text="Scan mode", padding=12)
        modes.pack(fill="x", pady=(0, 13))
        for index, (value, label) in enumerate((
            ("bin", "Recycle Bin"), ("folder", "Folder in Recycle Bin"), ("live_ntfs", "NTFS folder"),
            ("ntfs_image", "Filesystem image"), ("recover", "Deep scan image"), ("inspect", "Inspect image")
        )):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode,
                            command=self.update_source).grid(row=index // 3, column=index % 3,
                                                               sticky="w", padx=(0, 18), pady=3)

        image = ttk.LabelFrame(page, text="Scan source", padding=12)
        image.pack(fill="x")
        self.browse = ttk.Button(image, text="Browse…", command=self.choose_source)
        self.browse.pack(side="left")
        self.source_label = ttk.Label(image, text="Choose a raw disk image (.img, .dd, .raw).")
        self.source_label.pack(side="left", padx=12)

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=15)
        self.demo = ttk.Button(actions, text="Try a sample scan", command=lambda: self.start_scan(True))
        self.demo.pack(side="left")
        self.start_button = ttk.Button(actions, text="Start scan", style="Digger.TButton",
                                       command=lambda: self.start_scan(False), state="disabled")
        self.start_button.pack(side="right")
        ttk.Label(page, text="NTFS folder scans need administrator access. Recover live-drive files to a different physical disk.",
                  foreground=MUTED, wraplength=690).pack(anchor="w", pady=4)
        self.update_source()

    def build_results(self):
        page = self.results_page
        heading = ttk.Frame(page)
        heading.pack(fill="x")
        self.status_label = ttk.Label(heading, text="No scan yet", font=("Segoe UI", 15, "bold"))
        self.status_label.pack(side="left")
        self.stop_button = ttk.Button(heading, text="Stop scan", command=self.stop_scan, state="disabled")
        self.stop_button.pack(side="right")
        self.progress = ttk.Progressbar(page, mode="indeterminate", length=130)
        self.progress.pack(fill="x", pady=(7, 0))
        tools = ttk.Frame(page)
        tools.pack(fill="x", pady=10)
        ttk.Label(tools, text="Search").pack(side="left")
        ttk.Entry(tools, textvariable=self.search, width=13).pack(side="left", padx=6)
        types = ttk.Combobox(tools, textvariable=self.format, values=("All file types",),
                             state="readonly", width=12)
        types.pack(side="left")
        self.types = types
        sort_button = ttk.Menubutton(tools, text="Sort & group")
        sort_button.pack(side="left", padx=7)
        menu = tk.Menu(sort_button, tearoff=False)
        for title, variable, options in (
            ("Sort by", self.sort, SORTS),
            ("Order", self.direction, ("Ascending", "Descending")),
            ("Group by", self.group, GROUPS),
        ):
            submenu = tk.Menu(menu, tearoff=False)
            for value in options:
                submenu.add_radiobutton(label=value, variable=variable, value=value,
                                        command=self.render_files)
            menu.add_cascade(label=title, menu=submenu)
        sort_button.configure(menu=menu)
        self.export_button = ttk.Button(tools, text="Recover selected…", style="Digger.TButton",
                                        command=self.save_selected, state="disabled")
        self.export_button.pack(side="right")
        self.search.trace_add("write", lambda *_: self.render_files())
        self.format.trace_add("write", lambda *_: self.render_files())

        split = ttk.Panedwindow(page, orient="horizontal")
        split.pack(fill="both", expand=True)
        listing = ttk.Frame(split)
        split.add(listing, weight=3)
        self.tree = ttk.Treeview(listing, columns=("folder", "type", "size", "date", "check"), selectmode="extended")
        self.tree.heading("#0", text="Name")
        self.tree.heading("folder", text="Original folder")
        self.tree.heading("type", text="Type")
        self.tree.heading("size", text="Size")
        self.tree.heading("date", text="File date")
        self.tree.heading("check", text="Recoverability")
        for key, width in (("#0", 145), ("folder", 150), ("type", 62), ("size", 72), ("date", 100), ("check", 153)):
            self.tree.column(key, width=width, minwidth=45)
        for label, (_, color) in CHECKS.items():
            self.tree.tag_configure(label, foreground=color)
        self.tree.tag_configure("group", background="#e3ede8", foreground=DARK)
        ttk.Button(tools, text="By folder", command=lambda: self.group_results("Original folder")).pack(side="left")
        ttk.Button(tools, text="By type", command=lambda: self.group_results("Type")).pack(side="left", padx=(4, 0))

        bar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_preview)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Return>", self.open_file)
        self.tree.bind("<Double-1>", self.open_clicked)
        xbar = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=xbar.set)
        xbar.pack(side="bottom", fill="x")
        selection_bar = ttk.Frame(listing)
        selection_bar.pack(fill="x", pady=(6, 0))
        self.footer = ttk.Label(selection_bar, text="0 files", foreground=MUTED)
        self.footer.pack(side="left")
        ttk.Button(selection_bar, text="Select all", command=self.select_all).pack(side="right", padx=4)
        self.report_button = ttk.Button(selection_bar, text="Save report…", command=self.save_report, state="disabled")
        self.report_button.pack(side="right", padx=4)

        inspector = ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(inspector, weight=2)
        self.preview = ttk.Label(inspector, text="Select an image or video to preview it.", wraplength=245)
        self.preview.pack(anchor="w", pady=(4, 8))
        actions = ttk.Frame(inspector)
        actions.pack(fill="x", pady=(0, 6))
        self.open_button = ttk.Button(actions, text="Open", state="disabled", command=self.open_file)
        self.open_button.pack(side="right")
        self.zoom_label = ttk.Label(actions, text="100%", width=6)
        self.zoom_label.pack(side="left")
        self.zoom_slider = ttk.Scale(actions, from_=0.5, to=4, variable=self.zoom,
                                     command=lambda _: self.draw_preview(), state="disabled")
        self.zoom_slider.pack(side="left", expand=True, fill="x", padx=(0, 8))
        canvas_frame = ttk.Frame(inspector)
        canvas_frame.pack(fill="both", expand=True)
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, height=180, width=190, bg="#e4ede8", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        for axis, row, column, edge in (("x", 1, 0, "ew"), ("y", 0, 1, "ns")):
            scroll = ttk.Scrollbar(canvas_frame, orient="horizontal" if axis == "x" else "vertical",
                                   command=getattr(self.canvas, axis + "view"))
            self.canvas.configure({axis + "scrollcommand": scroll.set})
            scroll.grid(row=row, column=column, sticky=edge)
        self.canvas.bind("<Configure>", lambda _: self.draw_preview())
        self.canvas.bind("<MouseWheel>", self.wheel_zoom)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        self.log = tk.Text(inspector, height=7, width=28, wrap="word", font=("Consolas", 9), state="disabled")
        self.log.pack(fill="x", pady=(6, 0))

    def update_source(self):
        mode = self.mode.get()
        self.browse.configure(state="disabled" if mode == "bin" else "normal")
        if mode == "bin":
            text, ready = "Scan your Recycle Bin across local drives.", True
        elif mode in ("folder", "live_ntfs"):
            hint = ("Choose the original NTFS folder." if mode == "live_ntfs" else
                    "Choose the original folder to search for items deleted from it.")
            text, ready = (str(self.folder_source) if self.folder_source else
                           hint), bool(self.folder_source)
        else:
            text, ready = "Choose a raw disk image (.img, .dd, .raw).", False
            if self.source:
                try:
                    size = self.source.stat().st_size
                    text, ready = f"{self.source.name}  ·  {size_label(size)}", bool(size)
                except OSError:
                    self.source = None
        self.source_label.configure(text=text)
        self.start_button.configure(state="normal" if ready and self.job is None and not self.saving else "disabled")

    def choose_source(self):
        if self.mode.get() in ("folder", "live_ntfs"):
            chosen = filedialog.askdirectory(title="Choose the original folder")
            if chosen:
                self.folder_source = Path(chosen)
        else:
            chosen = filedialog.askopenfilename(filetypes=[
                ("Raw disk images", "*.img *.dd *.raw *.bin *.ima"), ("All files", "*.*")])
            if chosen:
                self.source = Path(chosen)
        self.update_source()

    def start_scan(self, sample):
        if self.job is not None or self.saving:
            return
        mode = self.mode.get()
        if sample and mode != "inspect":
            mode = "recover"
        source = self.folder_source if mode in ("folder", "live_ntfs") else self.source
        if not sample and mode != "bin" and source is None:
            return
        folder = Path(tempfile.mkdtemp(dir=RUNS.name))
        self.job = {"folder": folder, "mode": mode, "status": "preparing"}
        self.files.clear()
        self.originals.clear()
        self.materialized.clear()
        self.metadata.clear()
        self.checks.clear()
        self.recovery_checks.clear()
        self.preview_pending = None
        self.render_files()
        self.set_log("")
        self.status_label.configure(text="Starting scan…")
        self.start_button.configure(state="disabled")
        self.demo.configure(state="disabled")
        self.tabs.select(self.results_page)
        self.update_motion()
        threading.Thread(target=self.scan, args=(self.job, None if sample else source), daemon=True).start()

    def scan(self, job, source):
        if job["mode"] in ("bin", "folder"):
            self.scan_bin(job, source if job["mode"] == "folder" else None)
            return
        if job["mode"] in ("live_ntfs", "ntfs_image"):
            self.scan_indexed(job, source)
            return
        image = job["folder"] / "source.img"
        try:
            if source is None:
                sample_image(image)
            else:
                if not source.is_file() or not source.stat().st_size:
                    raise OSError("Choose a non-empty raw disk image file.")
                shutil.copyfile(source, image)
            image.chmod(0o400)
            with LOCK:
                job["status"] = "running"
            run_job(job)
        except OSError as exc:
            job.update(status="failed", error=str(exc))
        finally:
            try:
                if image.exists():
                    image.chmod(0o600)
                    image.unlink()
            except OSError:
                pass
            finally:
                self.events.put(job)

    def scan_bin(self, job, folder):
        try:
            job.update(status="running", matches=[])
            for source, original in scan_recycle_bin(folder):
                if job["status"] == "stopping":
                    break
                job["matches"].append((source, original))
            job["status"] = "stopped" if job["status"] == "stopping" else "complete"
        except (OSError, subprocess.SubprocessError) as exc:
            job.update(status="failed", error=str(exc))
        finally:
            self.events.put(job)

    def scan_indexed(self, job, source):
        try:
            scope = PureWindowsPath(source.resolve(strict=True)) if job["mode"] == "live_ntfs" else None
            if scope is not None and os.name != "nt":
                raise OSError("Live NTFS scans require Windows.")
            volume = source_volume(scope) if scope is not None else str(source.resolve())
            job.update(status="running", matches=[])
            for entry in scan_ntfs(volume, scope, job):
                job["matches"].append((entry, entry.original))
            job["status"] = "stopped" if job["status"] == "stopping" else "complete"
        except (OSError, subprocess.SubprocessError) as exc:
            job.update(status="failed", error=str(exc))
        finally:
            self.events.put(job)

    def refresh(self):
        try:
            completed = self.events.get_nowait()
        except queue.Empty:
            if self.job is not None and self.job["status"] == "running":
                self.status_label.configure(text="Scanning Recycle Bin…" if self.job["mode"] in ("bin", "folder")
                                            else "Scanning disk image…")
                self.stop_button.configure(state="normal")
        else:
            self.finish_scan(completed)
        self.refresh_preview()
        self.refresh_save()
        self.after(85, self.refresh)

    def finish_scan(self, job):
        labels = {"complete": "Scan complete", "stopped": "Scan stopped", "failed": "Scan failed"}
        self.status_label.configure(text=labels[job["status"]])
        self.stop_button.configure(state="disabled")
        self.demo.configure(state="normal")
        self.job = None
        self.update_source()
        self.update_motion()
        if job.get("error"):
            messagebox.showerror("Scan", job["error"])
        is_bin = job["mode"] in ("bin", "folder")
        indexed = job["mode"] in ("live_ntfs", "ntfs_image")
        matches = job.get("matches", ()) if is_bin or indexed else ((path, None) for path in files_in(job))
        for path, original in matches:
            key = str(len(self.files))
            try:
                info = path if isinstance(path, Candidate) else path.stat()
            except OSError:
                continue
            self.files[key] = path
            self.metadata[key] = info
            if original is not None:
                self.originals[key] = original
            if indexed:
                self.checks[key] = "Deleted " + {"ntfs": "NTFS", "fat": "FAT", "exfat": "exFAT"}[path.filesystem] + " record"
        if indexed and not self.files and job["status"] == "complete":
            self.status_label.configure(text="No matching deleted filesystem records")
        if is_bin and not self.files and job["status"] == "complete":
            self.status_label.configure(text="No matching items in the Recycle Bin")
        self.tree.configure(displaycolumns=("#all" if is_bin or indexed else ("type", "size", "date", "check")))
        if is_bin or indexed:
            self.group.set("Original folder")
        elif self.group.get() == "Original folder":
            self.group.set("None")
        extensions = {self.display_path(key).suffix.upper() for key in self.files}
        self.types.configure(values=("All file types", *sorted(extensions - {""})))
        self.format.set("All file types")
        log = job["folder"] / "output.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16384))
                text = stream.read().decode(errors="replace")
            clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]|[\x00-\x08\x0b-\x1f]", "", text)
            if job["mode"] == "inspect" or job["status"] == "failed":
                self.set_log(clean.strip())

    def set_log(self, content):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", content)
        self.log.configure(state="disabled")

    def fade_in(self, step=0):
        if os.name != "nt":
            return
        if not self.animate.get() or step >= 10:
            self.attributes("-alpha", 1.0)
            return
        self.attributes("-alpha", 1 - 0.30 * (1 - step / 10) ** 2)
        self.after(16, self.fade_in, step + 1)

    def update_motion(self):
        self.progress.stop()
        if (self.job is not None or self.saving) and self.animate.get():
            self.progress.start(16)

    def check_label(self, key):
        return self.recovery_checks.get(key, self.checks.get(key, "Unchecked"))

    def file_date(self, key):
        stamp = self.metadata[key].st_mtime
        try:
            return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") if stamp else "Unknown"
        except (OSError, ValueError, OverflowError):
            return "Unknown"

    def group_results(self, value):
        self.group.set(value)
        self.render_files()

    def display_path(self, key):
        return self.originals.get(key, self.files[key])

    def group_label(self, key):
        path = self.display_path(key)
        return {"Original folder": str(path.parent) if key in self.originals else "Unknown folder",
                "Type": path.suffix.upper().lstrip(".") or "Other",
                "File date": self.file_date(key),
                "Recoverability": self.check_label(key)}.get(self.group.get())

    def render_files(self):
        selected = set(self.selected_files())
        self.tree.delete(*self.tree.get_children())
        query = self.search.get().casefold()
        extension = self.format.get()
        keys = [key for key in self.files if query in str(self.display_path(key)).casefold()
                and extension in ("All file types", self.display_path(key).suffix.upper())]
        sort_key = {
            "Name": lambda key: self.display_path(key).name.casefold(),
            "Type": lambda key: self.display_path(key).suffix.casefold(),
            "Size": lambda key: self.metadata[key].st_size,
            "File date": lambda key: self.metadata[key].st_mtime,
            "Recoverability": lambda key: CHECKS[self.check_label(key)][0],
        }[self.sort.get()]
        keys.sort(key=lambda key: self.display_path(key).name.casefold())
        keys.sort(key=sort_key, reverse=self.direction.get() == "Descending")
        groups = {}
        for key in keys:
            label = self.group_label(key)
            if label is not None and label not in groups:
                group_key = f"group-{len(groups)}"
                groups[label] = self.tree.insert("", "end", iid=group_key, text=label,
                                                 tags=("group",), open=True)
            path = self.display_path(key)
            check = self.check_label(key)
            self.tree.insert(groups.get(label, ""), "end", iid=key, text=path.name,
                             values=(str(path.parent) if key in self.originals else "",
                                     path.suffix.lstrip(".").upper(), size_label(self.metadata[key].st_size),
                                     self.file_date(key), check), tags=(check,))
        self.shown_count = len(keys)
        self.report_button.configure(state="normal" if self.files else "disabled")
        self.tree.selection_set([key for key in keys if key in selected])
        self.show_preview()

    def selected_files(self):
        return [key for key in self.tree.selection() if key in self.files]

    def show_preview(self, _event=None):
        selected = self.selected_files()
        self.footer.configure(text=f"{self.shown_count} files shown  ·  {len(selected)} selected")
        self.export_button.configure(state="normal" if selected and not self.saving else "disabled")
        key = selected[0] if selected else None
        if key == self.preview_key:
            return
        self.preview_key = key
        self.preview_bitmap = None
        self.zoom.set(1)
        self.zoom_slider.configure(state="disabled")
        self.draw_preview()
        self.preview_pending = None
        if key is None:
            self.preview.configure(text="Select an image or video to preview it.")
            self.open_button.configure(state="disabled")
            return
        path = self.files[key]
        display = self.display_path(key)
        suffix = display.suffix.lower()
        media = suffix in IMAGE_TYPES or suffix in VIDEO_TYPES
        if isinstance(path, Candidate):
            output = self.materialized.get(key)
            if output is None and (path.identity is None or path.st_size > 64 * 1024 * 1024):
                self.open_button.configure(state="disabled")
                self.preview.configure(text=f"{display.name} · Recover this file to preview it.")
                return
        self.open_button.configure(text="Play" if suffix in VIDEO_TYPES else "Open",
                                   state="normal" if media else "disabled")
        self.preview.configure(text=f"{display.name} · Loading preview…" if media
                               else f"{display.name} · Preview unavailable")
        if media:
            self.preview_pending = key, path, suffix
            self.start_preview()

    def start_preview(self):
        if self.preview_active or self.preview_pending is None:
            return
        key, path, suffix = self.preview_pending
        self.preview_pending = None
        self.preview_active = True
        saved_path = self.materialized.get(key)

        def work():
            try:
                if saved_path is not None:
                    picture, check = decode_preview(saved_path, suffix)
                elif isinstance(path, Candidate):
                    with tempfile.TemporaryDirectory(dir=RUNS.name) as folder:
                        output = Path(folder) / path.original.name
                        extract_ntfs(path, output)
                        picture, check = decode_preview(output, suffix)
                else:
                    picture, check = decode_preview(path, suffix)
            except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.SubprocessError,
                    Image.DecompressionBombError):
                picture, check = None, "Preview failed"
            self.preview_events.put((key, path, picture, check))

        threading.Thread(target=work, daemon=True).start()

    def refresh_preview(self):
        try:
            key, path, bitmap, check = self.preview_events.get_nowait()
        except queue.Empty:
            return
        self.preview_active = False
        if self.files.get(key) == path:
            self.checks[key] = check
            if self.sort.get() == "Recoverability" or self.group.get() == "Recoverability":
                self.render_files()
            elif self.tree.exists(key):
                shown = self.check_label(key)
                self.tree.set(key, "check", shown)
                self.tree.item(key, tags=(shown,))
            if self.preview_key == key:
                self.preview_bitmap = bitmap
                self.preview.configure(text=f"{self.display_path(key).name} · {check}")
                self.zoom_slider.configure(state="normal" if bitmap is not None else "disabled")
                self.draw_preview()
        self.start_preview()

    def draw_preview(self):
        self.canvas.delete("all")
        self.preview_image = None
        zoom = self.zoom.get()
        self.zoom_label.configure(text=f"{zoom:.0%}")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if self.preview_bitmap is None or min(width, height) < 10:
            return
        source = self.preview_bitmap
        scale = min(min(width / source.width, height / source.height) * zoom,
                    2400 / max(source.size))
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        self.preview_image = ImageTk.PhotoImage(source.resize(size, Image.Resampling.BILINEAR))
        self.canvas.create_image(max(width, size[0]) / 2, max(height, size[1]) / 2, image=self.preview_image)
        self.canvas.configure(scrollregion=(0, 0, max(width, size[0]), max(height, size[1])))

    def wheel_zoom(self, event):
        if self.preview_bitmap is not None:
            self.zoom.set(max(0.5, min(4, self.zoom.get() * (1.15 if event.delta > 0 else 1 / 1.15))))
            self.draw_preview()
        return "break"

    def open_clicked(self, event):
        if self.tree.identify_row(event.y) in self.files:
            self.open_file()

    def open_file(self, _event=None):
        selected = self.selected_files()
        if not selected:
            return
        key = selected[0]
        path = self.files[key]
        if self.display_path(key).suffix.lower() not in IMAGE_TYPES | VIDEO_TYPES:
            return
        try:
            if isinstance(path, Candidate):
                output = self.materialized.get(key)
                if output is None:
                    if path.identity is None or path.st_size > 64 * 1024 * 1024:
                        raise OSError("Recover this file before opening it.")
                    folder = Path(tempfile.mkdtemp(dir=RUNS.name))
                    output = folder / path.original.name
                    extract_ntfs(path, output)
                path = output
            elif key in self.originals:
                if not ordinary(path):
                    raise OSError("The Recycle Bin item is no longer available.")
                preview_dir = Path(tempfile.mkdtemp(dir=RUNS.name))
                copy = preview_dir / self.originals[key].name
                shutil.copyfile(path, copy)
                path = copy
            os.startfile(path)
        except (AttributeError, OSError) as exc:
            messagebox.showerror("Open file", f"Windows could not open this file. Check its default app.\n\n{exc}")

    def select_all(self, _event=None):
        def file_rows(parent=""):
            for key in self.tree.get_children(parent):
                if key in self.files:
                    yield key
                elif self.tree.item(key, "open"):
                    yield from file_rows(key)

        self.tree.selection_set(list(file_rows()))
        return "break"

    def validate_destination(self, keys, folder):
        live_sources = {entry.source for key in keys
                        if isinstance(entry := self.files[key], Candidate) and entry.identity is None}
        for source in live_sources:
            require_separate_disk(source, Path(folder).resolve(strict=True))

    def save_report(self):
        if not self.files:
            return
        name = filedialog.asksaveasfilename(title="Save scan report", defaultextension=".csv",
                                            initialfile="Digger scan.csv", filetypes=[("CSV files", "*.csv")])
        if not name:
            return
        destination = Path(name)
        try:
            self.validate_destination(self.files, destination.parent)
            with destination.open("w", encoding="utf-8-sig", newline="") as stream:
                report = csv.writer(stream)
                report.writerow(("Path", "Size (bytes)", "File date", "Evidence"))
                for key in self.files:
                    path = str(self.display_path(key))
                    # Keep recovered filenames from being interpreted as spreadsheet formulas.
                    if path.lstrip().startswith(("=", "+", "-", "@")):
                        path = "'" + path
                    report.writerow((path, self.metadata[key].st_size, self.file_date(key), self.check_label(key)))
        except OSError as exc:
            messagebox.showerror("Save scan report", str(exc))

    def save_selected(self):
        if self.saving:
            return
        selection = self.selected_files()
        if not selection:
            return
        folder = filedialog.askdirectory(title="Save recovered files to")
        if not folder:
            return
        try:
            self.validate_destination(selection, folder)
            available = shutil.disk_usage(folder).free
            required = sum(self.metadata[key].st_size for key in selection)
            if required > available:
                raise OSError(f"Selected files need {size_label(required)}, and the destination has {size_label(available)} free.")
            destination = Path(tempfile.mkdtemp(prefix="Digger-Recovered-", dir=folder))
        except OSError as exc:
            messagebox.showerror("Recover selected files", str(exc))
            return
        self.saving = True
        self.status_label.configure(text="Recovering selected files…")
        self.export_button.configure(state="disabled")
        self.demo.configure(state="disabled")
        self.update_source()
        self.update_motion()
        entries = [(key, self.files[key], self.originals.get(key)) for key in selection]
        threading.Thread(target=self.copy_selected, args=(destination, entries), daemon=True).start()

    def copy_selected(self, destination, entries):
        saved, failures, materialized, evidence = 0, [], {}, {}
        for key, source, original in entries:
            try:
                if original is not None:
                    if not isinstance(source, Candidate) and not ordinary(source):
                        raise OSError("The item is no longer available.")
                    output = (destination.joinpath(original.drive.rstrip(":"), *original.parts[1:])
                              if original.drive else destination.joinpath(*original.parts))
                else:
                    output = destination / source.parent.name / source.name
                output.parent.mkdir(parents=True, exist_ok=True)
                if output.exists():
                    index = 2
                    while output.with_name(f"{output.stem} ({index}){output.suffix}").exists():
                        index += 1
                    output = output.with_name(f"{output.stem} ({index}){output.suffix}")
                if isinstance(source, Candidate):
                    evidence[key] = extract_ntfs(source, output)
                    materialized[key] = output
                else:
                    with atomic_output(output, metadata_from=source) as staging:
                        shutil.copyfile(source, staging)
                    evidence[key] = "File copied"
                saved += 1
            except OSError as exc:
                failures.append(f"{(original or source).name}: {exc}")
        self.save_events.put((destination, len(entries), saved, failures, materialized, evidence))

    def refresh_save(self):
        try:
            destination, count, saved, failures, materialized, evidence = self.save_events.get_nowait()
        except queue.Empty:
            return
        self.materialized.update(materialized)
        self.recovery_checks.update(evidence)
        self.saving = False
        self.status_label.configure(text=f"Recovered {saved} of {count} selected files")
        self.demo.configure(state="normal")
        self.update_source()
        self.update_motion()
        self.preview_key = None
        self.render_files()
        result = f"Recovered {saved} of {count} selected files to:\n{destination}"
        if failures:
            log = result + "\n\n" + "\n".join(failures)
            self.set_log(log)
            messagebox.showwarning("Recovery finished with errors", result + "\nSee the results log for details.")
        else:
            messagebox.showinfo("Recovery complete", result)

    def stop_scan(self):
        with LOCK:
            if self.job is None or self.job["status"] != "running":
                return
            self.job["status"] = "stopping"
            if self.job.get("process"):
                try:
                    self.job["process"].terminate()
                except OSError:
                    pass
        self.stop_button.configure(state="disabled")
        self.status_label.configure(text="Stopping scan…")

    def close(self):
        if self.saving:
            messagebox.showinfo("Recovery in progress", "Wait for the selected files to finish saving.")
            return
        if self.job is not None:
            messagebox.showinfo("Scan in progress", "Wait for the scan to finish or click Stop scan.")
            return
        self.destroy()


if __name__ == "__main__":
    RecoveryWindow().mainloop()
