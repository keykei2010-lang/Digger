"""Portable desktop interface for the bundled Windows recovery engines."""
import argparse
import csv
import hashlib
import lzma
from collections import Counter, deque
import ctypes
import os
import queue
import re
import shutil
import subprocess
import tempfile
from time import monotonic
import threading
import sys
from datetime import datetime
from pathlib import Path, PureWindowsPath
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from carved_results import CarvedResults
from recovery_output import atomic_output
from ntfs_recovery import Candidate, extract as extract_ntfs, preview_bytes, scan as scan_ntfs
from windows_volumes import is_elevated, local_volumes, require_separate_disk, selected_scope, source_volume

from media_preview import IMAGE_TYPES, VIDEO_TYPES, decode_preview
from recycle_bin import current_user_bins, emptied_items, ordinary, scan_recycle_bin
from server import LOCK, RUNS, files_in, run_job

if os.name == "nt":
    runtime = Path(sys.executable).parent
    os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")

BG = "#202329"
DARK = "#191b1f"
PANEL = "#242830"
SURFACE = "#30353e"
BORDER = "#30394c"
ACCENT = "#4277df"
TEXT = "#eff2fa"
MUTED = "#aeb9cc"
SELECT = "#33485d"
LIVE_PREVIEW_LIMIT = 16 * 1024 * 1024
SORTS = ("Discovery order", "Name", "Size", "File date", "Type", "Recoverability")
GROUPS = ("None", "Original folder", "Type", "File date", "Recoverability")
CHECKS = {
    "Preview failed": (0, "#ffa4a9"),
    "Retained in Recycle Bin": (1, "#afc7eb"),
    "Emptied-bin record": (1, "#f4cb92"),
    "Carved from drive": (2, "#acf0ca"),
    "Unchecked": (1, "#f4cb92"),
    "Deleted NTFS record": (1, "#f4cb92"),
    "Deleted FAT record": (1, "#f4cb92"),
    "Deleted exFAT record": (1, "#f4cb92"),
    "Expected bytes read": (2, "#acf0ca"),
    "Resident NTFS data": (2, "#acf0ca"),
    "NTFS clusters free": (2, "#acf0ca"),
    "File copied": (2, "#acf0ca"),
    "Video frame decoded": (2, "#acf0ca"),
    "Image decoded": (3, "#acf0ca"),
}


def elapsed_label(start):
    seconds = monotonic() - start
    if seconds < 1:
        return f"{max(1, round(seconds * 1000))} ms"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds:02}s"


def size_label(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


def file_icon(kind, size=20):
    colors = {"Pictures": "#45aaa2", "Videos": "#956bd7", "Documents": "#588bdb",
              "Audio": "#c76696", "Archives": "#ba8750", "Folder": "#5996d6",
              "Other files": "#687b99"}
    picture = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(picture)
    fill = colors.get(kind, colors["Other files"])
    if kind == "Folder":
        draw.rounded_rectangle((3, 13, 32, 39), radius=6, fill=fill)
        draw.rounded_rectangle((3, 20, 61, 54), radius=6, fill=fill)
        draw.line((8, 27, 55, 27), fill="#abd5ff", width=3)
    else:
        draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill=fill)
        if kind == "Videos":
            draw.polygon(((25, 18), (46, 32), (25, 47)), fill="white")
        elif kind == "Pictures":
            draw.ellipse((16, 16, 25, 25), fill="white")
            draw.polygon(((12, 46), (27, 31), (33, 37), (43, 25), (53, 46)), fill="white")
        elif kind == "Audio":
            draw.line((24, 43, 24, 23, 44, 18, 44, 38), fill="white", width=4)
            for x, y in ((20, 44), (40, 39)):
                draw.ellipse((x-5, y-4, x+4, y+4), fill="white")
        else:
            draw.rounded_rectangle((19, 13, 46, 50), radius=3, outline="white", width=3)
            for y in (25, 32, 39):
                draw.line((25, y, 39, y), fill="white", width=2)
    return ImageTk.PhotoImage(picture.resize((size, size), Image.Resampling.LANCZOS))


def location_icon(kind, size):
    art = Image.new("RGBA", (96, 96))
    d = ImageDraw.Draw(art)
    dark, mid, light = "#374e6b", "#6788ad", "#9bbcdf"
    if kind == "Drive":
        d.polygon([(19,20),(75,20),(86,63),(11,63)], fill=mid)
        d.ellipse((32,29,67,63), fill=light)
        d.ellipse((44,41,55,51), fill=dark)
        d.rounded_rectangle((10,55,86,81),radius=6,fill=dark,outline=mid,width=2)
        d.line((20,67,60,67),fill=mid,width=2)
        d.ellipse((73,64,78,69),fill="#65d8c7")
    elif kind == "Desktop":
        d.rounded_rectangle((4,9,91,69),radius=7,fill=dark)
        for y in range(15,61):
            d.line((10,y,85,y), fill=(83-y//3,122-y//3,161-y//3))
        d.rectangle((43,69,53,78),fill=mid)
        d.rounded_rectangle((30,78,66,82),radius=2,fill=dark)
    elif kind == "Bin":
        d.polygon([(14,18),(82,18),(72,81),(25,81)],fill=mid)
        d.rounded_rectangle((12,12,84,23),radius=4,fill=light)
        for x in (34,47,60):
            d.rounded_rectangle((x,32,x+4,66),radius=2,fill="#b4d4ee")
    elif kind == "Folder":
        d.rounded_rectangle((6,18,47,46),radius=6,fill=mid)
        d.rounded_rectangle((6,29,90,79),radius=6,fill=dark)
        d.line((12,33,83,33),fill=light,width=3)
    else:
        d.rounded_rectangle((15,11,80,83),radius=7,fill=mid)
        d.ellipse((24,20,72,68),fill=light)
        d.pieslice((24,20,72,68),230,280,fill="#d6e7fa")
        d.ellipse((41,37,55,51),fill=dark)
        d.rounded_rectangle((19,68,76,78),radius=3,fill=dark)
    return ImageTk.PhotoImage(art.resize((size,size),Image.Resampling.LANCZOS))


def row_images(picture):
    icons = []
    for checked in (False, True, None):
        tile = Image.new("RGBA", (43, 24))
        draw = ImageDraw.Draw(tile)
        draw.rounded_rectangle((1, 5, 15, 19), radius=3, fill=ACCENT if checked else None,
                               outline="#7cafff" if checked else "#939ba7")
        if checked:
            draw.line(((4, 12), (7, 15), (12, 9)), fill="white", width=2)
        if checked is None:
            draw.line((4, 12, 12, 12), fill="#94bfff", width=2)
        icon = picture.copy()
        icon.thumbnail((22, 22), Image.Resampling.LANCZOS)
        tile.paste(icon, (20, (24-icon.height)//2))
        icons.append(ImageTk.PhotoImage(tile))
    return icons


FILE_GROUPS = {"Pictures": IMAGE_TYPES, "Videos": VIDEO_TYPES,
               "Documents": {".pdf", ".txt", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".csv"},
               "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
               "Archives": {".zip", ".rar", ".7z", ".gz", ".tar"}}
GROUP_BY_SUFFIX = {ext: group for group, extensions in FILE_GROUPS.items() for ext in extensions}


class RecoveryWindow(tk.Tk):
    def __init__(self):
        if os.name == "nt":
            ctypes.windll.user32.SetProcessDPIAware()
        super().__init__()
        self.title("Digger")
        self.geometry("1240x780")
        if os.name == "nt":
            dark = ctypes.c_int(1)
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        self.minsize(900, 600)
        self.configure(bg=BG)
        self.mode = tk.StringVar(value="live_ntfs")
        self.source_text = tk.StringVar()
        self.can_scan_drives = os.name == "nt"
        self.elevated = self.can_scan_drives and is_elevated()
        self.source_paths = {}
        self.drive_roots = {}
        self.facet_rows = {}
        self.facet_counts = Counter()
        self.extensions = set()
        self.facets = {}
        self.facet_filter = None
        self.facet_mode = tk.StringVar(value="Folder")
        self.search = tk.StringVar()
        self.format = tk.StringVar(value="All file types")
        self.job = None
        self.checked = set()
        self.name_hint = tk.StringVar()
        self.movie_filter = tk.StringVar(value="All file types")
        self.raw_source = None
        self.output_folder = None
        self.page_number = 0
        self.page_size = 250
        self.test_folders = []
        self.next_render = 0
        self.completed_job = None
        self.saving = False
        self.save_events = queue.SimpleQueue()
        self.files = {}
        self.originals = {}
        self.materialized = {}
        self.metadata = {}
        self.checks = {}
        self.recovery_checks = {}
        self.sort = tk.StringVar(value="Discovery order")
        self.direction = tk.StringVar(value="Ascending")
        self.group = tk.StringVar(value="None")
        self.preview_events = queue.SimpleQueue()
        self.preview_pending = None
        self.preview_active = False
        self.preview_key = None
        self.match_count = 0
        self.shown_count = 0
        self.preview_bitmap = None
        self.zoom = tk.DoubleVar(value=1)
        self.animate = tk.BooleanVar(value=os.environ.get("DIGGER_REDUCED_MOTION", os.environ.get("RELAY_REDUCED_MOTION")) != "1")
        self.events = queue.SimpleQueue()
        self.preview_image = None
        self.protocol("WM_DELETE_WINDOW", self.close)
        kinds = (*FILE_GROUPS, "Folder", "Other files")
        self.icons = {kind: file_icon(kind) for kind in kinds}
        self.large_icons = {kind: file_icon(kind, 56) for kind in kinds}
        self.row_icons = {kind: row_images(ImageTk.getimage(icon)) for kind, icon in self.icons.items()}
        self.thumbnails = {}
        self.thumb_tried = set()
        self.thumb_thread = None
        self.thumb_events = queue.SimpleQueue()
        self.scan_generation = 0
        self.admin_warning = None
        self.back_stack = []
        self.scan_source = "Results"
        self.build()
        if len(sys.argv) > 1:
            parser = argparse.ArgumentParser()
            parser.add_argument("--mode", choices=("live_ntfs", "formatted", "bin", "folder"), default="live_ntfs")
            parser.add_argument("--source", default="")
            parser.add_argument("--name", default="")
            parser.add_argument("--movies", action="store_true")
            parser.add_argument("--resume-scan", action="store_true")
            self.after_idle(self.restore_launch, parser.parse_args())
        self.after_idle(self.fade_in)
        self.after(150, self.refresh)

    def build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Vertical.TScrollbar", background="#394456", troughcolor=PANEL,
                        arrowcolor=MUTED, borderwidth=0, arrowsize=12)
        style.configure("Horizontal.TScrollbar", background="#394456", troughcolor=PANEL,
                        arrowcolor=MUTED, borderwidth=0, arrowsize=12)
        style.configure("Toolbutton", relief="flat", padding=(12, 7), background=SURFACE)
        style.map("Toolbutton", background=[("selected", "#284d81"), ("active", SELECT)],
                  foreground=[("selected", "white")])
        style.configure("TButton", background=SURFACE, borderwidth=0, relief="flat", padding=(12, 7))
        style.map("TButton", background=[("active", SELECT)], foreground=[("disabled", MUTED)])
        style.configure("Digger.TButton", background=ACCENT, foreground="white", borderwidth=0, padding=(17, 9))
        style.map("Digger.TButton", background=[("disabled", SURFACE), ("active", "#396ac4")],
                  foreground=[("disabled", MUTED), ("active", "white")])
        style.configure("Preview.TButton", background="#304e7c", foreground="white")
        style.map("Preview.TButton", background=[("active", "#3c6199"), ("disabled", SURFACE)])
        style.configure("TEntry", fieldbackground=SURFACE, insertcolor=TEXT, padding=7)
        style.configure("TCombobox", fieldbackground=SURFACE, arrowcolor=TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE)], foreground=[("readonly", TEXT)])
        style.configure("TMenubutton", background=SURFACE, padding=(8, 6))
        style.configure("Treeview", rowheight=36, fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, borderwidth=0)
        style.configure("Treeview.Heading", background=SURFACE, foreground=TEXT,
                        relief="flat", padding=(7, 6))
        style.map("Treeview.Heading", background=[("active", SELECT)])
        style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 9), background=PANEL, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", SELECT)],
                  foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE)

        top = tk.Frame(self, bg=DARK)
        top.pack(fill="x")
        mark = tk.Canvas(top, width=34, height=34, bg=DARK, highlightthickness=0)
        mark.pack(side="left", padx=(22, 0))
        mark.create_oval(3, 3, 27, 27, outline="#79abff", width=3)
        mark.create_arc(8, 8, 22, 22, start=20, extent=225, style="arc", outline="#79abff", width=2)
        mark.create_line(23, 23, 31, 31, fill="#79abff", width=4)
        tk.Label(top, text="Digger Data Recovery", font=("Segoe UI Semibold", 12), bg=DARK,
                 fg=TEXT, padx=10, pady=12).pack(side="left")

        self.banner = tk.Frame(self, bg=PANEL, padx=20, pady=9)
        self.banner.pack(side="bottom", fill="x")
        self.ring = tk.Canvas(self.banner, width=56, height=56, bg=PANEL, highlightthickness=0)
        self.ring.pack(side="left", padx=(0, 16))
        self.ring.create_oval(4, 4, 52, 52, outline=SURFACE, width=4)
        self.ring_arc = self.ring.create_arc(4, 4, 52, 52, start=90, extent=0,
                                             style="arc", outline=ACCENT, width=4)
        self.ring_text = self.ring.create_text(28, 28, text="—", fill=TEXT, font=("Segoe UI Semibold", 11))
        self.stop_button = ttk.Button(self.banner, text="Cancel scan", command=self.stop_scan)
        self.status_label = tk.Label(self.banner, bg=PANEL, fg=MUTED, text="Ready to scan",
                                     font=("Segoe UI Semibold", 17), anchor="w")
        self.status_label.pack(fill="x")
        self.source_summary = ttk.Label(self.banner, background=PANEL, foreground=TEXT)
        self.activity = tk.Label(self, anchor="w", padx=12, pady=3, bg="#28354b", fg="#cadbf5")
        self.progress = ttk.Progressbar(self.banner, mode="indeterminate")
        self.activity.pack(side="bottom", fill="x")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        style.layout("Workspace.TNotebook.Tab", [])
        style.layout("Workspace.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
        style.configure("Workspace.TNotebook", borderwidth=0, lightcolor=BG, darkcolor=BG)
        self.tabs = ttk.Notebook(body, style="Workspace.TNotebook")
        self.tabs.pack(fill="both", expand=True)
        self.scan_page = ttk.Frame(self.tabs)
        self.results_page = ttk.Frame(self.tabs)
        self.tabs.add(self.scan_page, text="  New scan  ")
        self.tabs.add(self.results_page, text="  Scan results  ")
        nav = ttk.Menubutton(top, text="☰", width=3)
        nav.pack(side="right", padx=10)
        nav_menu = tk.Menu(nav, tearoff=False, bg=SURFACE, fg=TEXT)
        nav_menu.add_command(label="Scan locations", command=lambda: self.tabs.select(self.scan_page))
        nav_menu.add_command(label="Recovery results", command=lambda: self.tabs.select(self.results_page))
        nav_menu.add_checkbutton(label="Animations", variable=self.animate, command=self.update_motion)
        nav.configure(menu=nav_menu)
        ttk.Button(top, text="Results", command=lambda: self.tabs.select(self.results_page)).pack(side="right")
        self.build_scan()
        self.build_results()
        def sync_status(_event=None):
            visible = self.tabs.select() == str(self.results_page) or self.job is not None
            if visible and not self.banner.winfo_manager():
                self.banner.pack(before=body, side="bottom", fill="x")
                self.activity.pack(before=body, side="bottom", fill="x")
            elif not visible:
                self.banner.pack_forget()
                self.activity.pack_forget()
        self.tabs.bind("<<NotebookTabChanged>>", sync_status)
        sync_status()

    def build_scan(self):
        page = self.scan_page
        rail = tk.Frame(page, bg=DARK, width=206)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self.nav_buttons = {}
        sidebar = (("Data recovery", (("live_ntfs", "Hardware disk", "Drive"), ("bin", "Recycle Bin", "Bin"),
                                     ("ntfs_image", "Disk image", "Image"))),
                   ("Advanced recovery", (("formatted", "Formatted drive", "Drive"), ("recover", "Deep scan image", "Image"),
                                          ("inspect", "Inspect partitions", "Image"), ("folder", "Bin by folder", "Folder"))))
        self.location_icons = {(kind, size): location_icon(kind, size) for kind in
                               ("Drive", "Bin", "Desktop", "Folder", "Image") for size in (21, 48)}
        for section, items in sidebar:
            tk.Label(rail, text=section, bg=DARK, fg="#9b9fa9", font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(24, 8))
            for mode, title, kind in items:
                button = tk.Button(rail, text="  " + title, image=self.location_icons[kind, 21], compound="left",
                                   anchor="w", padx=12, pady=7, bg=DARK, fg=TEXT, bd=0, relief="flat",
                                   activebackground=SELECT, activeforeground=TEXT, cursor="hand2",
                                   command=lambda m=mode: self.pick_mode(m))
                button.pack(fill="x", padx=8, pady=2)
                self.nav_buttons[mode] = button
        self.demo = tk.Button(rail, text="Test deleted-file recovery", bg=DARK, fg=MUTED, bd=0,
                              activebackground=SELECT, activeforeground=TEXT, command=lambda: self.start_scan(True))
        self.demo.pack(side="bottom", anchor="w", padx=16, pady=16)
        main = tk.Frame(page, bg=BG)
        main.pack(fill="both", expand=True)
        footer = tk.Frame(main, bg=BG, highlightthickness=1, highlightbackground=SURFACE)
        footer.pack(side="bottom", fill="x")
        self.start_button = ttk.Button(footer, text="Search for lost data", style="Digger.TButton",
                                       command=lambda: self.start_scan(False))
        self.start_button.pack(side="right", padx=16, pady=12)
        self.source_label = tk.Label(footer, anchor="w", bg=BG, fg=MUTED, wraplength=360, justify="left")
        self.source_label.pack(side="left", fill="x", expand=True, padx=16)
        viewport = tk.Canvas(main, bg=BG, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(main, orient="vertical", command=viewport.yview)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(fill="both", expand=True)
        self.location_content = content = tk.Frame(viewport, bg=BG, padx=26, pady=22)
        window = viewport.create_window(0, 0, anchor="nw", window=content)
        content.bind("<Configure>", lambda _: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda e: viewport.itemconfigure(window, width=e.width))
        self.bind("<MouseWheel>", lambda e: viewport.yview_scroll(-int(e.delta/120), "units")
                  if self.tabs.select() == str(page) and viewport.yview() != (0., 1.) else None, add="+")
        heading = tk.Frame(content, bg=BG)
        heading.pack(fill="x", pady=(4, 20))
        self.location_heading = tk.Label(heading, bg=BG, fg=TEXT, font=("Segoe UI Semibold", 16))
        self.location_heading.pack(side="left")
        tk.Button(heading, text="↻", bg=BG, fg=MUTED, bd=0, font=("Segoe UI", 14),
                  activebackground=BG, activeforeground=TEXT, command=self.refresh_drives).pack(side="left", padx=8)
        self.drives_box = tk.Frame(content, bg=BG)
        self.drives_box.pack(fill="x")
        self.locations_box = tk.Frame(content, bg=BG)
        self.locations_box.pack(fill="x", pady=(12, 0))
        tk.Label(self.locations_box, text="Specify a location", bg=BG, fg=MUTED).pack(anchor="w", pady=(0, 10))
        shortcuts = tk.Frame(self.locations_box, bg=BG)
        shortcuts.pack(fill="x")
        for index, (kind, title) in enumerate((("Bin","Recycle Bin"), ("Desktop","Desktop"), ("Folder","Select folder"), ("Image","Disk image"))):
            button = tk.Button(shortcuts, text="   " + title, image=self.location_icons[kind,48], compound="left",
                               anchor="w", bg=BG, fg=TEXT, activebackground=SELECT, activeforeground=TEXT,
                               padx=6, pady=10, bd=0, cursor="hand2", command=lambda k=kind: self.shortcut(k))
            button.grid(row=index//3, column=index%3, sticky="ew", pady=3)
            shortcuts.columnconfigure(index%3, weight=1)
        self.path_panel = ttk.Frame(content)
        self.path_label = ttk.Label(self.path_panel, foreground=MUTED)
        self.path_label.pack(anchor="w", pady=(0, 5))
        self.path_entry = ttk.Entry(self.path_panel, textvariable=self.source_text)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<Return>", lambda _: self.start_scan(False))
        ttk.Button(self.path_panel, text="Browse…", command=self.choose_source).pack(side="left", padx=(6,0))
        self.options_button = tk.Button(content, text="Scan options  ▾", bg=BG, fg=MUTED, bd=0,
                                        activebackground=BG, activeforeground=TEXT, command=self.toggle_options)
        self.options_button.pack(anchor="w", pady=(12, 0))
        self.hint_box = ttk.Frame(content)
        ttk.Label(self.hint_box, text="File name contains").pack(side="left")
        ttk.Entry(self.hint_box, textvariable=self.name_hint, width=26).pack(side="left", padx=8)
        self.carve_options = ttk.Frame(content)
        ttk.Label(self.carve_options, text="Look for").pack(side="left")
        ttk.Combobox(self.carve_options, state="readonly", textvariable=self.movie_filter,
                     values=("All file types", "MP4 / MOV movies"), width=24).pack(side="left", padx=8)
        self.cards = {}
        self.volume_names = {}
        self.options_open = False
        self.source_text.trace_add("write", lambda *_: self.update_source())
        self.bind("<Control-l>", self.focus_path)
        self.refresh_drives()
        self.change_source_mode()

    def pick_mode(self, mode):
        self.mode.set(mode)
        self.change_source_mode()

    def shortcut(self, kind):
        if kind == "Bin":
            self.pick_mode("bin")
        elif kind == "Image":
            self.pick_mode("ntfs_image")
            self.choose_source()
        elif kind == "Desktop":
            self.pick_mode("live_ntfs")
            folder = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, folder) == 0:
                self.source_text.set(folder.value)
            self.focus_path()
        else:
            self.pick_mode("live_ntfs")
            self.focus_path()
            self.choose_source()

    def toggle_options(self):
        self.options_open = not self.options_open
        self.layout_locations()

    def refresh_drives(self):
        self.drive_roots.clear()
        self.volume_names.clear()
        self.cards.clear()
        for child in self.drives_box.winfo_children():
            child.destroy()
        groups = {}
        for root, name, filesystem in local_volumes() if self.can_scan_drives else ():
            self.drive_roots[root] = (root, filesystem)
            self.volume_names[root] = name
            group = "Removable drives" if ctypes.windll.kernel32.GetDriveTypeW(root) == 2 else "Local drives"
            groups.setdefault(group, []).append(root)
        for group in ("Removable drives", "Local drives"):
            roots = groups.get(group, [])
            if not roots:
                continue
            tk.Label(self.drives_box, text=f"{group} ({len(roots)})", bg=BG, fg=MUTED).pack(anchor="w", pady=(4,8))
            grid = tk.Frame(self.drives_box, bg=BG)
            grid.pack(fill="x", pady=(0,12))
            for i, root in enumerate(roots):
                card = tk.Canvas(grid, width=214, height=80, bg=BG, highlightthickness=0, takefocus=True, cursor="hand2")
                card.grid(row=i//2, column=i%2, sticky="w", padx=(0,8), pady=3)
                self.cards[root] = card
                card.create_rectangle(1,1,213,79, outline=BG, fill=BG, tags="surface")
                card.create_image(12,40, anchor="w", image=self.location_icons["Drive",48])
                card.create_text(69,23, anchor="w", text=f"{self.volume_names[root][:15]} ({root[:2]})",
                                 fill=TEXT, font=("Segoe UI Semibold",10))
                try:
                    space = shutil.disk_usage(root)
                except OSError:
                    space = None
                if space and space.total:
                    card.create_rectangle(69,40,195,44,fill="#464c57",outline="")
                    card.create_rectangle(69,40,69+126*space.used/space.total,44,fill="#54b8db",outline="")
                    card.create_text(69,58,anchor="w",text=f"{size_label(space.free)} free of {size_label(space.total)}",
                                     fill=MUTED,font=("Segoe UI",8))
                card.create_oval(194,5,208,19, fill=ACCENT, outline="",tags="check")
                card.create_text(201,12,text="✓",fill="white",font=("Segoe UI",8),tags="check")
                for event in ("<Button-1>", "<Return>", "<space>"):
                    card.bind(event, lambda _, r=root: self.source_text.set(r))
                card.bind("<FocusIn>", lambda _, c=card:c.configure(highlightthickness=1,highlightbackground=MUTED,highlightcolor=MUTED))
                card.bind("<FocusOut>", lambda _, c=card:c.configure(highlightthickness=0))
        if not groups:
            tk.Label(self.drives_box, text="No mounted drives found", bg=BG, fg=MUTED).pack(anchor="w")
        if not self.source_text.get() and self.mode.get() in ("live_ntfs", "formatted"):
            self.source_text.set(next((r for r, fs in self.drive_roots.values() if fs.upper()=="NTFS"), next(iter(self.drive_roots),"")))
        self.update_source()

    def focus_path(self, _event=None):
        self.tabs.select(self.scan_page)
        if self.mode.get() != "bin":
            self.options_open = True
            self.layout_locations()
            self.path_entry.focus_set()
            self.path_entry.selection_range(0, "end")
        return "break"

    def restore_launch(self, options):
        self.mode.set(options.mode)
        self.change_source_mode()
        self.source_text.set(options.source)
        self.name_hint.set(options.name)
        self.movie_filter.set("MP4 / MOV movies" if options.movies else "All file types")
        if options.resume_scan:
            self.start_scan(False)

    def show_admin_warning(self):
        if self.admin_warning is not None and self.admin_warning.winfo_exists():
            self.admin_warning.destroy()
        panel = self.admin_warning = tk.Frame(self, bg=SURFACE, padx=14, pady=10,
                                               highlightbackground="#e4b75f", highlightthickness=1)
        tk.Label(panel, text="Administrator access required", anchor="w", bg=SURFACE,
                 fg="#ffd484", font=("Segoe UI Semibold", 11)).grid(row=0, column=0, sticky="w")
        tk.Label(panel, text="To start this scan, right-click Digger.exe and choose Run as administrator.",
                 wraplength=330, justify="left", bg=SURFACE, fg=TEXT).grid(row=1, column=0, sticky="w")
        tk.Button(panel, text="Dismiss", bg=SURFACE, fg=TEXT, activebackground=SELECT, activeforeground=TEXT,
                  relief="flat", command=panel.destroy).grid(row=0, column=1, padx=(8, 0))
        def reveal(step):
            panel.place(relx=1, rely=1, x=-18, y=-85 + (8-step)**2//2, anchor="se")
            if step < 8:
                panel.after(18, reveal, step+1)
        reveal(0 if self.animate.get() else 8)
        panel.lift()
        panel.after(14000, panel.destroy)

    def continue_deep_scan(self):
        drive = self.deep_drive
        self.mode.set("formatted")
        self.change_source_mode()
        self.source_text.set(drive)
        self.start_scan(False)

    def build_results(self):
        page = self.results_page

        heading = ttk.Frame(page, padding=(12, 8))
        heading.pack(side="bottom", fill="x")
        self.save_status = ttk.Label(heading, foreground=TEXT)
        self.save_status.pack(side="left")
        self.deep_button = ttk.Button(heading, text="Deep scan this drive…", command=self.continue_deep_scan)
        self.open_output = ttk.Button(heading, text="Open recovery folder", state="disabled",
                                      command=lambda: os.startfile(self.output_folder))

        tools = ttk.Frame(page, padding=(10, 8))
        tools.pack(fill="x")
        ttk.Button(tools, text="⌂", width=3, command=lambda: self.tabs.select(self.scan_page)).pack(side="left")
        self.back_button = ttk.Button(tools, text="←", width=3, state="disabled", command=self.facet_back)
        self.back_button.pack(side="left", padx=(8, 3))
        self.up_button = ttk.Button(tools, text="↑", width=3, state="disabled", command=self.facet_up)
        self.up_button.pack(side="left")
        self.breadcrumb = ttk.Label(tools, text="Results", font=("Segoe UI Semibold", 12), anchor="w")
        self.breadcrumb.pack(side="left", fill="x", expand=True, padx=10)
        self.search_entry = ttk.Entry(tools, textvariable=self.search, width=23)
        self.bind("<Control-f>", self.focus_search)
        sort_button = ttk.Menubutton(tools, text="View")
        sort_button.pack(side="left", padx=4)
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
        menu.add_separator()
        menu.add_command(label="Select this page", command=self.select_all)
        menu.add_command(label="Clear selection", command=self.clear_selection)
        menu.add_command(label="Save scan report…", command=self.save_report)
        self.filter_button = ttk.Menubutton(tools, text="Filter")
        self.filter_button.pack(side="left", padx=4)
        self.filter_menu = tk.Menu(self.filter_button, tearoff=False, postcommand=self.populate_filters)
        self.filter_button.configure(menu=self.filter_menu)
        self.search_entry.pack(side="left", padx=(8, 0))
        search_hint = tk.Label(self.search_entry, text="Search files or folders", bg=SURFACE, fg=MUTED)
        def hint(_event=None):
            if self.search.get() or self.focus_get() == self.search_entry:
                search_hint.place_forget()
            else:
                search_hint.place(x=7, rely=.5, anchor="w")
        search_hint.bind("<Button-1>", self.focus_search)
        self.search_entry.bind("<FocusIn>", hint)
        self.search_entry.bind("<FocusOut>", hint)
        self.search.trace_add("write", lambda *_: hint())
        hint()

        self.export_button = ttk.Button(self.banner, text="Recover selected…", style="Digger.TButton",
                                        command=self.save_selected, state="disabled")
        self.export_button.pack(side="right", before=self.status_label, padx=8)

        self.search.trace_add("write", lambda *_: self.render_files())
        self.format.trace_add("write", lambda *_: self.render_files())

        split = ttk.Panedwindow(page, orient="horizontal")
        split.pack(fill="both", expand=True)
        navigation = ttk.Frame(split, padding=(0, 0, 8, 0), width=210)
        split.add(navigation, weight=0)

        facet_tabs = ttk.Frame(navigation)
        facet_tabs.pack(fill="x", pady=(0, 8))
        for name in ("Folder", "Type"):
            ttk.Radiobutton(facet_tabs, style="Toolbutton", text="Path" if name == "Folder" else "Type", value=name, variable=self.facet_mode,
                            command=self.refresh_facets).pack(side="left", expand=True, fill="x")
        self.facet_list = ttk.Treeview(navigation, columns=("count",), show="tree",
                                       selectmode="browse", height=8)
        self.facet_list.heading("#0", text="Folder")
        self.facet_list.heading("count", text="Files")
        self.facet_list.column("#0", width=156, minwidth=70)
        self.facet_list.column("count", width=44, minwidth=40, stretch=False, anchor="e")
        facet_scroll = ttk.Scrollbar(navigation, orient="vertical", command=self.facet_list.yview)
        self.facet_list.configure(yscrollcommand=facet_scroll.set)
        facet_scroll.pack(side="right", fill="y")
        self.facet_list.pack(fill="both", expand=True)
        self.facet_list.bind("<Button-1>", self.check_facet)
        self.facet_list.bind("<<TreeviewSelect>>", self.select_facet)
        self.refresh_facets(render=False)
        listing = ttk.Frame(split)
        split.add(listing, weight=3)
        self.tree = ttk.Treeview(listing, columns=("folder", "type", "size", "date", "check"), selectmode="extended")
        self.tree["columns"] = ("pick", "folder", "type", "size", "date", "check")
        self.tree.heading("pick", text="✓")
        self.tree.column("pick", width=35, minwidth=30, stretch=False, anchor="center")
        self.tree.bind("<Button-1>", self.toggle_checked)
        self.tree.bind("<space>", self.check_focused)
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.configure(displaycolumns=("date", "type", "size"))
        self.tree.heading("folder", text="Original folder")
        self.tree.heading("type", text="Type")
        self.tree.heading("size", text="Size")
        self.tree.heading("date", text="Date modified")
        self.tree.heading("check", text="Recoverability")
        for key, width in (("#0", 265), ("folder", 150), ("type", 90), ("size", 85), ("date", 120), ("check", 153)):
            self.tree.column(key, width=width, minwidth=45)
        for label, (_, color) in CHECKS.items():
            self.tree.tag_configure(label, foreground=TEXT)
        self.tree.tag_configure("alternate", background="#292e37")
        self.tree.tag_configure("group", background=SURFACE, foreground=TEXT)
        for key, field in (("#0", "Name"), ("type", "Type"), ("size", "Size"),
                           ("date", "File date"), ("check", "Recoverability")):
            self.tree.heading(key, command=lambda field=field: self.sort_by(field))
        bar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self.show_preview)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Return>", self.open_file)
        self.tree.bind("<Double-1>", self.open_clicked)
        self.tree.bind("<Button-3>", self.show_file_menu)
        self.file_menu = tk.Menu(self, tearoff=False, bg=SURFACE, fg=TEXT)
        self.file_menu.add_command(label="Recover selected…", command=self.save_selected)
        self.file_menu.add_command(label="Copy original path", command=self.copy_original_path)
        xbar = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=xbar.set)
        xbar.pack(side="bottom", fill="x")
        selection_bar = ttk.Frame(listing)
        selection_bar.pack(side="bottom", fill="x")
        self.next_page = ttk.Button(selection_bar, text="Next ›", command=lambda: self.change_page(1))
        self.prev_page = ttk.Button(selection_bar, text="‹ Previous", command=lambda: self.change_page(-1))
        self.next_page.pack(side="right")
        self.prev_page.pack(side="right", padx=4)
        self.footer = ttk.Label(selection_bar, text="0 files", foreground=MUTED)
        self.footer.pack(side="left")




        self.tree.pack(fill="both", expand=True)

        inspector = ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(inspector, weight=2)
        def initial_panes(event):
            if event.width > 600:
                split.sashpos(0, int(event.width * .23))
                split.sashpos(1, int(event.width * .83))
                split.unbind("<Configure>", binding)
        binding = split.bind("<Configure>", initial_panes)
        self.preview_symbol = ttk.Label(inspector)
        self.details = ttk.Label(inspector, foreground=MUTED, wraplength=245, justify="left")

        self.preview = ttk.Label(inspector, text="Select an image or video to preview it.", wraplength=245)
        self.preview.pack(anchor="w", pady=(4, 8))
        actions = ttk.Frame(inspector)
        actions.pack(fill="x", pady=(0, 6))
        self.open_button = ttk.Button(actions, text="Preview", style="Preview.TButton", state="disabled", command=self.preview_clicked)
        self.open_button.pack(fill="x")
        self.zoom_controls = ttk.Frame(actions)
        self.zoom_label = ttk.Label(self.zoom_controls, text="100%", width=6)
        self.zoom_label.pack(side="left")
        self.zoom_slider = ttk.Scale(self.zoom_controls, from_=0.5, to=4, variable=self.zoom,
                                     command=lambda _: self.draw_preview(), state="disabled")
        self.zoom_slider.pack(side="left", expand=True, fill="x", padx=(0, 8))
        canvas_frame = ttk.Frame(inspector)
        self.canvas_frame = canvas_frame
        self.preview_actions = actions
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, height=118, width=190, bg=PANEL, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _: self.draw_preview())
        self.canvas.bind("<MouseWheel>", self.wheel_zoom)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        self.log = tk.Text(inspector, height=7, width=28, wrap="word", font=("Consolas", 9), state="disabled",
                           bg=PANEL, fg=TEXT, relief="flat")

    def layout_locations(self):
        mode = self.mode.get()
        titles = {"live_ntfs":"Select a location to search for lost data", "bin":"Search the Recycle Bin",
                  "formatted":"Select a formatted drive", "folder":"Select the original folder",
                  "recover":"Deep scan a disk image", "ntfs_image":"Search a disk image", "inspect":"Inspect disk partitions"}
        self.location_heading.configure(text=titles[mode])
        for key, button in self.nav_buttons.items():
            button.configure(bg="#313e50" if key == mode else DARK, fg="#82b3ff" if key == mode else TEXT)
        for box in (self.drives_box,self.locations_box,self.path_panel,self.hint_box,self.carve_options,self.options_button):
            box.pack_forget()
        if mode in ("live_ntfs", "formatted"):
            self.drives_box.pack(fill="x")
        if mode == "live_ntfs":
            self.locations_box.pack(fill="x", pady=(8,0))
        if mode != "bin" and (self.options_open or mode not in ("live_ntfs","formatted")):
            self.path_panel.pack(fill="x", pady=(8,0))
        self.options_button.configure(text="Hide scan options  ▴" if self.options_open else "Scan options  ▾")
        self.options_button.pack(anchor="w", pady=(10,0))
        if self.options_open or mode == "formatted":
            (self.carve_options if mode == "formatted" else self.hint_box).pack(fill="x", pady=6)

    def change_source_mode(self):
        mode = self.mode.get()
        self.options_open = False
        self.layout_locations()
        value = self.source_paths.get(mode, "")
        if not value and mode in ("live_ntfs", "formatted"):
            value = next((r for r, fs in self.drive_roots.values() if fs.upper()=="NTFS"), next(iter(self.drive_roots),""))
        self.source_text.set(value)

    def update_source(self):
        mode = self.mode.get()
        value = self.source_text.get().strip()
        self.source_paths[mode] = value
        for root, card in self.cards.items():
            selected = root.casefold() == value.casefold()
            card.itemconfigure("surface", fill=SELECT if selected else BG, outline=ACCENT if selected else BG)
            card.itemconfigure("check", state="normal" if selected else "hidden")
        labels = {"formatted": "Search formatted drive", "live_ntfs": "Scan for deleted files", "bin": "Scan Recycle Bin", "folder": "Scan Recycle Bin",
                  "ntfs_image": "Scan image", "recover": "Deep scan image", "inspect": "Inspect image"}
        self.start_button.configure(text="Search for lost data" if mode == "live_ntfs" else labels[mode])
        self.path_label.configure(text="Drive or folder path" if mode in ("live_ntfs", "folder", "formatted") else "Image file path")
        ready = bool(value) or mode == "bin"
        if mode == "live_ntfs":
            if not self.elevated:
                text = "Administrator access is required to read the drive."
                if self.can_scan_drives:
                    text = "Start Digger as administrator to scan this drive."
            else:
                text = "Save recovered files to a different physical disk."
            if value:
                try:
                    scope = selected_scope(os.path.expandvars(value))
                    filesystem = next((fs for root, fs in self.drive_roots.values()
                                       if root.casefold() == scope.anchor.casefold()), None)
                    if filesystem and filesystem.upper() != "NTFS":
                        text = f"{scope.drive} uses {filesystem}. To scan this filesystem, open a disk image."
                        ready = False
                except ValueError as exc:
                    text, ready = str(exc), False
        elif mode == "formatted":
            text = "Scan the entire mounted volume for file contents. Digger saves discoveries to a different physical disk."
        elif mode == "bin":
            text = "List retained items, then search mounted NTFS drives for files removed when the bin was emptied."
        elif mode == "folder":
            text = "Find Recycle Bin items originally deleted from this folder."
        else:
            text = "Open a raw disk image such as .img, .dd, or .raw."
        self.source_label.configure(text=text)
        self.start_button.configure(state="normal" if ready and self.job is None and not self.saving else "disabled")

    def choose_source(self):
        if self.mode.get() in ("folder", "live_ntfs", "formatted"):
            current = Path(self.source_text.get())
            chosen = filedialog.askdirectory(title="Choose the original folder",
                                             initialdir=str(current) if current.is_dir() else None)
        else:
            chosen = filedialog.askopenfilename(filetypes=[
                ("Raw disk images", "*.img *.dd *.raw *.bin *.ima"), ("All files", "*.*")])
        if chosen:
            self.source_text.set(chosen)

    def start_scan(self, sample):
        if self.job is not None or self.saving:
            return
        mode = self.mode.get()
        if sample:
            mode = "ntfs_image"
        source = None
        if not sample and mode != "bin":
            value = os.path.expandvars(self.source_text.get().strip().strip('"'))
            if not value:
                self.focus_path()
                return
            if mode in ("folder", "live_ntfs", "formatted"):
                try:
                    source = selected_scope(value)
                except ValueError as exc:
                    messagebox.showerror("Scan source", str(exc))
                    return
                if mode == "formatted" and source != PureWindowsPath(source.anchor):
                    messagebox.showerror("Formatted drive", "Select a drive letter. A formatted-drive search reads the whole volume.")
                    return
            else:
                source = Path(value)
                if not source.is_file():
                    messagebox.showerror("Scan source", "Choose an existing disk image file.")
                    return
        if not sample and mode in ("live_ntfs", "formatted", "bin", "folder") and not self.elevated:
            self.show_admin_warning()
            return
        if sample:
            location = filedialog.askdirectory(title="Choose a healthy drive for the 48 MiB recovery test")
            if not location:
                return
            try:
                scratch = tempfile.TemporaryDirectory(prefix="Digger-Test-", dir=location)
            except OSError as exc:
                messagebox.showerror("Recovery test", str(exc))
                return
            self.test_folders.append(scratch)
            folder = Path(scratch.name)
        elif mode == "formatted":
            destination = self.choose_recovery_folder(f"Scan all of {source.drive} and save discoveries on a different physical disk.")
            if not destination:
                return
            try:
                device = source_volume(source)
                require_separate_disk(device, Path(destination).resolve(strict=True))
                folder = Path(tempfile.mkdtemp(prefix="Digger-Formatted-", dir=destination))
            except OSError as exc:
                messagebox.showerror("Recovery destination", str(exc))
                return
        else:
            folder = Path(tempfile.mkdtemp(dir=RUNS.name))
        self.deep_button.pack_forget()
        self.raw_source = device if mode == "formatted" else None
        self.output_folder = folder if mode == "formatted" else None
        if self.output_folder:
            self.open_output.configure(state="normal")
            self.open_output.pack(side="right")
        else:
            self.open_output.pack_forget()
        self.job = {"folder": folder, "mode": mode, "status": "preparing", "matches": deque(),
                    "self_test": sample, "started": monotonic(), "source": str(source or ("Recycle Bin" if mode == "bin" else "Built-in NTFS test image"))}
        if mode == "formatted":
            self.job.update(device=device, movies_only=self.movie_filter.get() == "MP4 / MOV movies")
        self.scan_source = self.job["source"]
        self.thumbnails.clear()
        self.thumb_tried.clear()
        self.scan_generation += 1
        self.breadcrumb.configure(text=self.scan_source)
        self.ring.itemconfigure(self.ring_text, text="…")
        self.ring.itemconfigure(self.ring_arc, extent=0, outline=ACCENT)
        self.source_summary.configure(text=self.job["source"])
        self.source_summary.pack(fill="x")
        self.save_status.configure(text="")
        self.activity.configure(text="Opening the scan source…")
        self.stop_button.pack(before=self.status_label, side="right", padx=(10, 0))
        self.stop_button.configure(text="Cancel scan", state="normal")
        self.preview_key = object()
        self.page_number = 0
        self.filter_button.configure(text="Filter")
        indexed = mode in ("live_ntfs", "ntfs_image", "bin", "folder")

        self.facet_mode.set("Folder" if indexed else "Type")
        self.checked.clear()
        self.extensions.clear()
        self.files.clear()
        self.originals.clear()
        self.materialized.clear()
        self.metadata.clear()
        self.checks.clear()
        self.recovery_checks.clear()
        self.preview_pending = None
        self.clear_filters()
        self.search.set("" if sample or mode == "formatted" else self.name_hint.get().strip())
        self.set_log("")
        self.set_scan_status("Starting scan", "#91bdff")
        self.start_button.configure(state="disabled")
        self.demo.configure(state="disabled")
        self.tabs.select(self.results_page)
        self.update_motion()
        threading.Thread(target=self.scan, args=(self.job, None if sample else source), daemon=True).start()

    def set_scan_status(self, text, color):
        self.status_label.configure(text=text, fg=color)
        self.title(f"{text}  ·  Digger")

    def scan(self, job, source):
        try:
            with LOCK:
                if job["status"] == "stopping":
                    return
                job["status"] = "running"
            if job["self_test"]:
                source = job["folder"] / "recovery-check.img"
                with lzma.open(Path(__file__).with_name("recovery-check.img.xz")) as fixture, source.open("wb") as target:
                    shutil.copyfileobj(fixture, target)
            if job["mode"] in ("bin", "folder"):
                self.scan_bin(job, source if job["mode"] == "folder" else None)
            elif job["mode"] == "formatted":
                self.carve(job)
            elif job["mode"] in ("live_ntfs", "ntfs_image"):
                self.scan_indexed(job, source)
            else:
                self.scan_image(job, source)
        except Exception as exc:
            with LOCK:
                if job["status"] != "stopping":
                    job.update(status="failed", error=str(exc) or type(exc).__name__)
        finally:
            with LOCK:
                if job["status"] == "stopping":
                    job["status"] = "stopped"
                elif job["status"] == "running":
                    job["status"] = "complete"
            self.events.put(job)

    def scan_image(self, job, source):
        image = job["folder"] / "source.img"
        try:
            if not source.is_file() or not source.stat().st_size:
                raise OSError("Choose a non-empty raw disk image file.")
            total = source.stat().st_size
            job["copying"] = (0, total)
            with source.open("rb") as src, image.open("wb") as dst:
                while chunk := src.read(4 * 1024 * 1024):
                    if job["status"] == "stopping":
                        job["status"] = "stopped"
                        return
                    dst.write(chunk)
                    job["copying"] = dst.tell(), total
            job.pop("copying", None)
            image.chmod(0o400)
            with LOCK:
                if job["status"] == "stopping":
                    job["status"] = "stopped"
                    return
                job["status"] = "running"
            if job["mode"] == "recover":
                self.carve(job)
            else:
                run_job(job)
        finally:
            try:
                if image.exists():
                    image.chmod(0o600)
                    image.unlink()
            except OSError:
                pass

    def carve(self, job):
        carved = CarvedResults(job)
        run_job(job, carved.read)
        if job["status"] == "complete":
            job["volume_bytes"] = carved.source_size
            if job["mode"] == "formatted" and not carved.source_size:
                raise OSError("PhotoRec did not confirm a readable source volume.")
            for path in files_in(job):
                if path.resolve() not in carved.seen:
                    job["matches"].append((path, None))

    def scan_bin(self, job, folder):
        for source, original in scan_recycle_bin(folder, cancelled=lambda: job["status"] == "stopping"):
            if job["status"] == "stopping":
                return
            job["matches"].append((source, original))
        volumes = {root.casefold() for root, _, fs in local_volumes() if fs.upper() == "NTFS"}
        searched = 0
        failures = []
        for bin_path in current_user_bins():
            scope = PureWindowsPath(bin_path)
            if scope.anchor.casefold() not in volumes or (folder and folder.drive.casefold() != scope.drive.casefold()):
                continue
            if job["status"] == "stopping":
                return
            job.pop("records", None)
            job["phase"] = f"Searching {scope.drive} for emptied-bin records"
            try:
                for payload, original in emptied_items(scope, job, folder):
                    job["matches"].append((payload, original))
                searched += 1
            except OSError as exc:
                failures.append(f"{scope.drive} {exc}")
        if job["status"] != "stopping":
            if failures:
                raise OSError("Emptied-bin search incomplete. " + " • ".join(failures))
            if not searched:
                raise OSError("No mounted NTFS volumes were available for the emptied-bin search.")

    def scan_indexed(self, job, source):
        scope = source if job["mode"] == "live_ntfs" else None
        if scope is not None and os.name != "nt":
            raise OSError("Live NTFS scans require Windows.")
        volume = source_volume(scope) if scope is not None else str(source.resolve())
        verified = False
        for entry in scan_ntfs(volume, scope, job):
            job["matches"].append((entry, entry.original))
            if job["self_test"] and entry.original.parts[-2:] == ("Documents", "Important.bin"):
                output = job["folder"] / "recovered.bin"
                extract_ntfs(entry, output)
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                verified = digest == "833d518b36e9b97849398241881ea06f37c70ecb2d7b1c6d2af78c44ea2cb973"
        if job["self_test"] and not verified and job["status"] != "stopping":
            raise OSError("The recovery check could not reproduce the original file.")

    def refresh(self):
        job = self.job
        if job is not None:
            if monotonic() >= self.next_render and job["matches"]:
                self.consume_matches(job)
                self.next_render = monotonic() + (0.3 if self.sort.get() != "Discovery order" else 0)
            elapsed = elapsed_label(job["started"])
            records = job.get("records")
            stage = {"formatted": "Searching raw sectors for surviving file contents", "live_ntfs": "Reading NTFS file table", "ntfs_image": "Reading filesystem index",
                     "recover": "Carving disk image", "inspect": "Inspecting partition table",
                     "bin": "Reading Recycle Bin", "folder": "Reading Recycle Bin"}[job["mode"]]
            detail = ""
            copying = job.get("copying")
            if copying or records:
                done, total = copying or records
                if str(self.progress.cget("mode")) != "determinate":
                    self.progress.stop()
                self.progress.configure(mode="determinate", maximum=max(1, total), value=done)
                if not self.progress.winfo_manager():
                    self.progress.pack(fill="x", pady=(5, 0))
                if copying:
                    stage = "Preparing image copy"
                    detail = f"   ·   {size_label(done)} / {size_label(total)}"
                else:
                    stage = job.get("phase", "Reading NTFS file table")
                    detail = f"   ·   {done:,} / {total:,} records"
            elif str(self.progress.cget("mode")) == "determinate":
                self.update_motion()
            stage = job.get("phase", stage)
            if job["status"] == "running":
                percentage = f"  ·  {min(100, 100 * done // max(1, total))}%" if copying or records else ""
                self.set_scan_status("Scanning" + percentage, "#91bdff")
                self.ring.itemconfigure(self.ring_text, text=percentage.split()[-1] if percentage else "…")
                self.ring.itemconfigure(self.ring_arc, extent=-359 * done / max(1, total) if percentage else -75,
                                        start=90 if percentage or not self.animate.get() else -int(monotonic() * 120) % 360)
            latest = ""
            if self.files:
                latest = "   ·   Latest: " + self.display_path(next(reversed(self.files))).name
            self.activity.configure(text=f"{stage}   ·   {len(self.files):,} files found{detail}   ·   {elapsed}{latest}")
        if self.completed_job is None:
            try:
                self.completed_job = self.events.get_nowait()
            except queue.Empty:
                pass
        if self.completed_job is not None:
            if self.completed_job["matches"]:
                self.stop_button.configure(state="disabled")
                label = {"stopped": "Scan canceled", "complete": "Scan finished", "failed": "Scan failed"}
                self.set_scan_status(label[self.completed_job["status"]] + "  ·  Listing results", MUTED)
            else:
                completed, self.completed_job = self.completed_job, None
                self.finish_scan(completed)
        self.refresh_preview()
        self.refresh_thumbnails()
        self.refresh_save()
        self.after(100, self.refresh)

    def consume_matches(self, job):
        matches = job["matches"]
        self.add_matches(matches.popleft() for _ in range(min(len(matches), 768)))

    def add_matches(self, matches):
        added = []
        counts = Counter()
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
            if isinstance(path, Candidate):
                self.checks[key] = "Deleted " + {"ntfs": "NTFS", "fat": "FAT", "exfat": "exFAT"}[path.filesystem] + " record"
            if self.job and self.job["mode"] in ("bin", "folder"):
                self.checks[key] = "Emptied-bin record" if isinstance(path, Candidate) else "Retained in Recycle Bin"
            elif self.job and self.job["mode"] == "formatted":
                self.checks[key] = "Carved from drive"
            added.append(key)
            counts.update(self.facet_path(key))
            suffix = self.display_path(key).suffix.upper()
            if suffix:
                self.extensions.add(suffix)
        for value, count in counts.items():
            self.facet_counts[value] += count
            self.update_facet(value)
        self.facet_list.set("all", "count", len(self.files))

        if self.sort.get() == "Discovery order" and self.direction.get() == "Ascending" and self.group.get() == "None":
            start = self.page_number * self.page_size
            for key in added:
                if self.matches_filter(key):
                    if start <= self.match_count < start + self.page_size:
                        self.insert_result(key, "")
                        self.shown_count += 1
                    self.match_count += 1

            self.next_page.configure(state="normal" if start + self.shown_count < self.match_count else "disabled")
            self.show_preview()
        elif added:
            self.render_files()

    def finish_scan(self, job):
        labels = {"complete": ("Scan complete", "#92dfa6"), "stopped": ("Scan canceled", "#f8cb83"),
                  "failed": ("Scan failed", "#ffa8a8")}
        title, color = labels[job["status"]]
        if job["status"] == "complete" and job["self_test"]:
            title = "Recovery test passed"
        self.ring.itemconfigure(self.ring_text, text={"complete": "✓", "failed": "!", "stopped": "■"}[job["status"]])
        self.ring.itemconfigure(self.ring_arc, extent=-359, outline=color)
        self.set_scan_status(title, color)
        self.stop_button.pack_forget()
        self.stop_button.configure(state="disabled")
        self.job = None
        if job["mode"] == "live_ntfs":
            self.deep_drive = PureWindowsPath(job["source"]).anchor
            self.deep_button.pack(side="right", padx=4)
        self.demo.configure(state="disabled" if self.saving else "normal")
        self.update_source()
        self.update_motion()
        if job.get("error"):
            self.set_log(job["error"])
        is_bin = job["mode"] in ("bin", "folder")
        indexed = job["mode"] in ("live_ntfs", "ntfs_image")
        elapsed = elapsed_label(job["started"])
        detail = f"   ·   {job['records'][0]:,} records examined" if job.get("records") else ""
        summary = f"{len(self.files):,} files found{detail}   ·   {elapsed}"
        if job["status"] == "stopped":
            summary += "   ·   Scanning has stopped. Partial results are available."
        elif job.get("error"):
            summary = job["error"].splitlines()[0]
        elif job["self_test"]:
            summary = "Recovered Documents\\Important.bin from the test image and verified all 12,032 bytes."
        self.activity.configure(text=summary)
        if job["mode"] == "formatted" and job["status"] == "complete":
            self.activity.configure(text=f"{summary}   ·   {size_label(job['volume_bytes'])} volume   ·   Saved to {job['folder']}")
        if job.get("error"):
            messagebox.showerror("Scan", job["error"])
        if indexed and not self.files and job["status"] == "complete":
            self.set_scan_status("Scan complete  ·  No deleted records found", color)
            self.set_log("No matching deleted filesystem records survived in the selected source. "
                         "If you scanned a folder, try the whole drive. Use Formatted drive to search raw sectors when the original records are gone.")
        if is_bin and not self.files and job["status"] == "complete":
            self.set_scan_status("Scan complete  ·  No matching Recycle Bin items", color)
        log = job["folder"] / "output.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16384))
                text = stream.read().decode(errors="replace")
            clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]|[\x00-\x08\x0b-\x1f]", "", text)
            if job["mode"] == "inspect" or job["status"] == "failed":
                self.set_log(clean.strip())

        if job["mode"] == "ntfs_image" and job["status"] == "complete":
            groups = Counter((entry.offset, entry.filesystem) for entry in self.files.values()
                             if isinstance(entry, Candidate) and entry.offset > 0)
            if groups:
                self.partition_notice(groups)

    def set_log(self, content):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", content)
        self.log.configure(state="disabled")
        if content.strip():
            self.log.pack(fill="x", pady=(6, 0))
        else:
            self.log.pack_forget()

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
        self.progress.configure(mode="indeterminate")
        if self.job is not None and self.animate.get():
            self.progress.pack(fill="x", pady=(5, 0))
            self.progress.start(16)
        else:
            self.progress.pack_forget()

    def check_label(self, key):
        return self.recovery_checks.get(key, self.checks.get(key, "Unchecked"))

    def file_date(self, key):
        stamp = self.metadata[key].st_mtime
        try:
            return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") if stamp else "Unknown"
        except (OSError, ValueError, OverflowError):
            return "Unknown"

    def focus_search(self, _event=None):
        self.tabs.select(self.results_page)
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        return "break"

    def facet_path(self, key):
        path = self.display_path(key)
        if self.facet_mode.get() == "Type":
            suffix = path.suffix.lower()
            group = GROUP_BY_SUFFIX.get(suffix, "Other files")
            return [(group,), (group, suffix.lstrip(".").upper() or "No extension")]
        if key not in self.originals or "$OrphanFiles" in path.parts:
            return [("Unknown folder",)]
        parts = path.parent.parts
        return [parts[:length] for length in range(1, len(parts) + 1)] or [("Unknown folder",)]

    def update_facet(self, value):
        if value not in self.facet_rows:
            row = str(len(self.facet_rows))
            self.facets[row] = value
            self.facet_rows[value] = row
            parent = self.facet_rows.get(value[:-1], "")
            self.facet_list.insert(parent, "end", iid=row, text=value[-1], open=len(value) == 1,
                                   values=(self.facet_counts[value],))
        kind = "Folder" if self.facet_mode.get() == "Folder" or len(value) > 1 else value[0]
        count = self.facet_checked[value]
        state = 0 if not count else 1 if count == self.facet_counts[value] else 2
        self.facet_list.item(self.facet_rows[value], image=self.row_icons[kind][state])
        self.facet_list.set(self.facet_rows[value], "count", self.facet_counts[value])

    def update_checked_counts(self, keys, delta):
        changes = Counter(part for key in keys for part in self.facet_path(key))
        for part, count in changes.items():
            self.facet_checked[part] += delta * count
            self.update_facet(part)

    def check_facet(self, event):
        tree = self.facet_list
        row = tree.identify_row(event.y)
        if "image" not in tree.identify_element(event.x, event.y) or row not in self.facets:
            return
        value = self.facets[row]
        if value is None:
            return
        adding = self.facet_checked[value] < self.facet_counts[value]
        changed = [key for key in self.files if (key in self.checked) != adding and value in self.facet_path(key)]
        if adding:
            self.checked.update(changed)
        else:
            self.checked.difference_update(changed)
            self.tree.selection_remove(*self.tree.selection())
        self.update_checked_counts(changed, 1 if adding else -1)
        for key in changed:
            if self.tree.exists(key):
                self.tree.item(key, image=self.result_icon(key))
        self.show_preview()
        return "break"

    def refresh_facets(self, render=True):
        self.facet_list.delete(*self.facet_list.get_children())
        self.back_stack.clear()
        self.facets = {"all": None}
        self.facet_filter = None
        self.facet_list.heading("#0", text=self.facet_mode.get())
        self.facet_list.insert("", "end", iid="all", text="All files", values=(len(self.files),))
        self.facet_counts = Counter(part for key in self.files for part in self.facet_path(key))
        self.facet_checked = Counter(part for key in self.checked for part in self.facet_path(key))
        self.facet_rows.clear()
        for value in sorted(self.facet_counts):
            self.update_facet(value)
        self.facet_list.selection_set("all")
        self.update_breadcrumb()
        if render:
            self.render_files()

    def populate_filters(self):
        self.filter_menu.delete(0, "end")
        for extension in ("All file types", *sorted(self.extensions)):
            self.filter_menu.add_radiobutton(label=extension, variable=self.format, value=extension)
        self.filter_menu.add_separator()
        self.filter_menu.add_command(label="Clear filters", command=self.clear_filters)

    def update_breadcrumb(self):
        parts = self.facet_filter or ()
        self.breadcrumb.configure(text="   ›   ".join([self.scan_source, *parts]))
        self.up_button.configure(state="normal" if parts else "disabled")
        self.back_button.configure(state="normal" if self.back_stack else "disabled")

    def goto_facet(self, value):
        self.facet_filter = value
        row = self.facet_rows.get(value, "all")
        self.facet_list.selection_set(row)
        self.facet_list.see(row)
        self.page_number = 0
        self.update_breadcrumb()
        self.render_files()

    def facet_back(self):
        if self.back_stack:
            self.goto_facet(self.back_stack.pop())

    def facet_up(self):
        if self.facet_filter:
            self.back_stack.append(self.facet_filter)
            self.goto_facet(self.facet_filter[:-1] or None)

    def select_facet(self, _event=None):
        selection = self.facet_list.selection()
        value = self.facets.get(selection[0]) if selection else None
        if value != self.facet_filter:
            self.back_stack.append(self.facet_filter)
            self.goto_facet(value)

    def clear_filters(self):
        self.facet_filter = None
        self.page_number = 0
        self.search.set("")
        self.format.set("All file types")
        self.refresh_facets()

    def sort_by(self, field):
        self.direction.set("Descending" if self.sort.get() == field and self.direction.get() == "Ascending"
                           else "Ascending")
        self.sort.set(field)
        self.render_files()

    def show_file_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row not in self.files:
            return
        if row not in self.selected_files():
            self.tree.selection_set(row)
        self.file_menu.entryconfigure(0, state="disabled" if self.saving else "normal")
        self.file_menu.entryconfigure(1, state="normal" if row in self.originals else "disabled")
        self.file_menu.tk_popup(event.x_root, event.y_root)

    def copy_original_path(self):
        paths = [str(self.originals[key]) for key in self.selected_files() if key in self.originals]
        if paths:
            self.clipboard_clear()
            self.clipboard_append("\n".join(paths))

    def display_path(self, key):
        return self.originals.get(key, self.files[key])

    def group_label(self, key):
        path = self.display_path(key)
        return {"Original folder": str(path.parent) if key in self.originals else "Unknown folder",
                "Type": path.suffix.upper().lstrip(".") or "Other",
                "File date": self.file_date(key),
                "Recoverability": self.check_label(key)}.get(self.group.get())

    def matches_filter(self, key):
        path = self.display_path(key)
        return (self.query in str(path).casefold()
                and self.extension in ("All file types", path.suffix.upper())
                and (self.facet_filter is None or self.facet_filter in self.facet_path(key)))

    def refresh_thumbnails(self):
        try:
            generation, results = self.thumb_events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.thumb_thread = None
            if generation == self.scan_generation:
                for key, picture in results:
                    if self.tree.exists(key):
                        self.remember_thumbnail(key, picture)
        if self.thumb_thread or not self.tree.winfo_ismapped():
            return
        visible = {self.tree.identify_row(y) for y in range(0, self.tree.winfo_height(), 12)}
        work = []
        for key in visible:
            if key not in self.files or key in self.thumbnails or key in self.thumb_tried:
                continue
            path = self.materialized.get(key, self.files[key])
            if isinstance(path, Candidate) or self.display_path(key).suffix.lower() not in IMAGE_TYPES:
                continue
            work.append((key, path))
            self.thumb_tried.add(key)
        if work:
            def read(generation):
                results = []
                for key, path in work:
                    try:
                        with Image.open(path) as img:
                            img.thumbnail((22, 22))
                            results.append((key, img.convert("RGBA")))
                    except (OSError, ValueError, Image.DecompressionBombError):
                        continue
                self.thumb_events.put((generation, results))
            self.thumb_thread = threading.Thread(target=read, args=(self.scan_generation,), daemon=True)
            self.thumb_thread.start()

    def remember_thumbnail(self, key, picture):
        if key not in self.thumbnails and len(self.thumbnails) >= self.page_size:
            oldest = next(iter(self.thumbnails))
            self.thumbnails.pop(oldest)
            self.thumb_tried.discard(oldest)
            if self.tree.exists(oldest):
                self.tree.item(oldest, image=self.result_icon(oldest))
        self.thumbnails[key] = row_images(picture)
        if self.tree.exists(key):
            self.tree.item(key, image=self.result_icon(key))

    def result_icon(self, key):
        kind = GROUP_BY_SUFFIX.get(self.display_path(key).suffix.lower(), "Other files")
        return self.thumbnails.get(key, self.row_icons[kind])[key in self.checked]

    def insert_result(self, key, parent):
        path = self.display_path(key)
        check = self.check_label(key)
        self.tree.insert(parent, "end", iid=key, text=path.name,
                         image=self.result_icon(key),
                         values=("☑" if key in self.checked else "☐", str(path.parent) if key in self.originals else "",
                                 path.suffix.lstrip(".").upper(), size_label(self.metadata[key].st_size),
                                 self.file_date(key), check), tags=(check, "alternate") if int(key) % 2 else (check,))

    def change_page(self, delta):
        self.page_number += delta
        self.tree.selection_remove(*self.tree.selection())
        self.render_files()

    def render_files(self):
        selected = set(self.tree.selection())
        scroll = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        self.query = self.search.get().casefold()
        self.extension = self.format.get()
        self.filter_button.configure(text="Filter" if self.extension == "All file types" else self.extension)
        keys = [key for key in self.files if self.matches_filter(key)]
        sort_key = {
            "Discovery order": int,
            "Name": lambda key: self.display_path(key).name.casefold(),
            "Type": lambda key: self.display_path(key).suffix.casefold(),
            "Size": lambda key: self.metadata[key].st_size,
            "File date": lambda key: self.metadata[key].st_mtime,
            "Recoverability": lambda key: CHECKS[self.check_label(key)][0],
        }[self.sort.get()]
        if self.sort.get() not in ("Discovery order", "Name"):
            keys.sort(key=lambda key: self.display_path(key).name.casefold())
        keys.sort(key=sort_key, reverse=self.direction.get() == "Descending")
        self.match_count = len(keys)
        self.page_number = max(0, min(self.page_number, (len(keys) - 1) // self.page_size))
        start = self.page_number * self.page_size
        keys = keys[start:start + self.page_size]
        self.prev_page.configure(state="normal" if start else "disabled")
        self.next_page.configure(state="normal" if start + len(keys) < self.match_count else "disabled")
        groups = {}
        for key in keys:
            label = self.group_label(key)
            if label is not None and label not in groups:
                group_key = f"group-{len(groups)}"
                groups[label] = self.tree.insert("", "end", iid=group_key, text=label,
                                                 tags=("group",), open=True)
            self.insert_result(key, groups.get(label, ""))
        self.shown_count = len(keys)

        self.tree.selection_set([key for key in keys if key in selected])
        if scroll:
            self.tree.yview_moveto(scroll[0])
        self.show_preview()

    def check_focused(self, _event=None):
        key = self.tree.focus()
        if key in self.files:
            self.set_checked(key)
        return "break"

    def toggle_checked(self, event):
        if self.tree.identify_column(event.x) != "#0" or "image" not in self.tree.identify_element(event.x, event.y):
            return
        key = self.tree.identify_row(event.y)
        if key in self.files:
            self.tree.selection_set(key)
            self.tree.focus(key)
            self.set_checked(key)
            return "break"

    def clear_selection(self):
        previous = list(self.checked)
        self.checked.clear()
        for key in previous:
            if self.tree.exists(key):
                self.tree.item(key, image=self.result_icon(key))
        self.update_checked_counts(previous, -1)
        self.tree.selection_remove(*self.tree.selection())
        self.show_preview()

    def set_checked(self, key):
        if key in self.checked:
            self.checked.remove(key)
            if not self.checked:
                self.tree.selection_remove(*self.tree.selection())
        else:
            self.checked.add(key)
        self.update_checked_counts([key], 1 if key in self.checked else -1)
        self.tree.item(key, image=self.result_icon(key))
        self.show_preview()

    def recovery_selection(self):
        return sorted(self.checked, key=int) if self.checked else self.selected_files()

    def selected_files(self):
        return [key for key in self.tree.selection() if key in self.files]

    def show_preview(self, _event=None):
        selected = self.selected_files()
        self.footer.configure(text=f"{self.page_number * self.page_size + int(bool(self.shown_count))}–{self.page_number * self.page_size + self.shown_count} of {self.match_count:,}  ·  {len(self.checked) or len(selected)} selected")
        self.export_button.configure(state="normal" if (self.checked or selected) and not self.saving else "disabled")
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
            self.details.pack_forget()
            self.preview_symbol.pack_forget()
            self.details.configure(text="")
            self.preview.configure(text="Select an image or video to preview it.")
            self.open_button.configure(state="disabled")
            return
        path = self.files[key]
        display = self.display_path(key)
        icon = self.large_icons[GROUP_BY_SUFFIX.get(display.suffix.lower(), "Other files")]
        self.preview_symbol.configure(image=icon)
        self.preview_symbol.pack(before=self.preview, anchor="w", pady=(6, 8))
        self.details.pack(anchor="w", fill="x", pady=(10, 6))
        folder = str(display.parent) if key in self.originals else "Original folder unknown"
        self.details.configure(text=f"Name\n{display.name}\n\nSize\n{size_label(self.metadata[key].st_size)}"
                                    f"\n\nType\n{display.suffix.upper().lstrip('.')} file\n\nPath\n{folder}\n\nRecovery evidence\n{self.check_label(key)}")
        suffix = display.suffix.lower()
        media = suffix in IMAGE_TYPES or suffix in VIDEO_TYPES
        live_preview = False
        if isinstance(path, Candidate):
            output = self.materialized.get(key)
            live_preview = output is None and path.identity is None
            can_preview_live = suffix in IMAGE_TYPES and path.st_size <= LIVE_PREVIEW_LIMIT
            if media and output is None and (live_preview and not can_preview_live
                                             or path.st_size > 64 * 1024 * 1024):
                self.open_button.configure(state="disabled")
                self.preview.configure(text=f"{display.name} · Recover this file to preview it.")
                return
        self.open_button.configure(text="Play" if suffix in VIDEO_TYPES else "Preview",
                                   state="normal" if media and not live_preview else "disabled")
        self.preview.configure(text="Loading preview…" if media
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
                elif isinstance(path, Candidate) and path.identity is None:
                    with preview_bytes(path, LIVE_PREVIEW_LIMIT) as data:
                        picture, check = decode_preview(data, suffix)
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
            if bitmap is not None:
                self.remember_thumbnail(key, bitmap)
            if self.preview_key == key:
                self.preview_bitmap = bitmap
                if bitmap is not None:
                    self.preview_symbol.pack_forget()
                    self.open_button.configure(state="normal")
                self.preview.configure(text="" if bitmap is not None else check)
                self.zoom_slider.configure(state="normal" if bitmap is not None else "disabled")
                self.draw_preview()
        self.start_preview()

    def draw_preview(self):
        self.canvas.delete("all")
        self.preview_image = None
        if self.preview_bitmap is None:
            self.canvas_frame.pack_forget()
            self.zoom_controls.pack_forget()
            return
        if not self.canvas_frame.winfo_manager():
            self.canvas_frame.pack(before=self.preview, fill="x", pady=(8, 5))
            self.zoom_controls.pack(fill="x", pady=(4, 0))
        zoom = self.zoom.get()
        self.zoom_label.configure(text=f"{zoom:.0%}")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if min(width, height) < 10:
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

    def preview_clicked(self):
        if self.preview_bitmap is None or self.preview_key is None:
            return self.open_file()
        path = self.display_path(self.preview_key)
        if path.suffix.lower() in VIDEO_TYPES:
            return self.open_file()
        viewer = tk.Toplevel(self)
        viewer.title(path.name)
        viewer.transient(self)
        picture = self.preview_bitmap.copy()
        picture.thumbnail((int(self.winfo_screenwidth()*.85), int(self.winfo_screenheight()*.8)))
        image = ImageTk.PhotoImage(picture)
        label = tk.Label(viewer, image=image, bg=PANEL)
        label.image = image
        label.pack(fill="both", expand=True)
        viewer.bind("<Escape>", lambda _: viewer.destroy())

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

        keys = list(file_rows())
        self.update_checked_counts(set(keys) - self.checked, 1)
        self.checked.update(keys)
        for key in keys:
            self.tree.item(key, image=self.result_icon(key))
        self.tree.selection_set(keys)
        self.show_preview()
        return "break"

    def validate_destination(self, keys, folder):
        live_sources = {entry.source for key in keys
                        if isinstance(entry := self.files[key], Candidate) and entry.identity is None}
        if self.raw_source:
            live_sources.add(self.raw_source)
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

    def modal(self, title, width, height):
        # The shade covers only Digger's client area.
        shade = tk.Toplevel(self)
        shade.withdraw()
        shade.overrideredirect(True)
        shade.transient(self)
        shade.configure(bg="black")
        shade.attributes("-alpha", .48)
        shade.geometry(f"{self.winfo_width()}x{self.winfo_height()}+{self.winfo_rootx()}+{self.winfo_rooty()}")
        shade.deiconify()
        dialog = tk.Toplevel(shade)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(shade)
        dialog.overrideredirect(True)
        dialog.configure(bg="#353940", highlightbackground="#4a505b", highlightthickness=1)
        x = self.winfo_rootx() + max(0, (self.winfo_width()-width)//2)
        y = self.winfo_rooty() + max(0, (self.winfo_height()-height)//2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.deiconify()
        dialog.lift()
        dialog.grab_set()
        dialog.focus_set()
        dialog.bind("<Escape>", lambda _: shade.destroy())
        dialog.protocol("WM_DELETE_WINDOW", shade.destroy)
        tk.Button(dialog, text="×", command=shade.destroy, relief="flat", borderwidth=0,
                  bg="#353940", fg=TEXT, font=("Segoe UI", 15)).place(relx=1, x=-10, y=6, anchor="ne")
        return shade, dialog

    def partition_notice(self, groups):
        shade, dialog = self.modal("Partitions found", 452, 300)
        tk.Label(dialog, text="Partitions with deleted files found", bg="#353940", fg=TEXT,
                 font=("Segoe UI Semibold", 13)).pack(pady=(30, 6))
        tk.Label(dialog, text="Choose a folder in the Path view to explore the recovered names.",
                 bg="#353940", fg=MUTED).pack()
        tree = ttk.Treeview(dialog, show="tree", height=3)
        tree.pack(fill="both", expand=True, padx=48, pady=16)
        for (sector, fs), count in sorted(groups.items()):
            node = tree.insert("", "end", text=f"{fs.upper()} partition at sector {sector:,}",
                               image=self.icons["Folder"], open=True)
            tree.insert(node, "end", text=f"{count:,} deleted files")
        ttk.Button(dialog, text="Got it", style="Digger.TButton", command=shade.destroy).pack(pady=(0, 16))
        self.wait_window(shade)

    def choose_recovery_folder(self, subtitle):
        self.update_idletasks()
        shade, dialog = self.modal("Recovery destination", 562, min(576, self.winfo_height()-30))
        color = "#353940"
        icon = tk.Canvas(dialog, width=60, height=60, bg=color, highlightthickness=0)
        icon.pack(pady=(28, 6))
        icon.create_polygon(9, 4, 42, 4, 53, 15, 53, 56, 9, 56, fill="#75bafa")
        icon.create_rectangle(19, 5, 38, 20, fill="#bde6ff", outline="")
        icon.create_rectangle(16, 33, 46, 54, fill="#347bd2", outline="")
        for y in (40, 44):
            icon.create_line(21, y, 41, y, fill="#a9d9ff", width=2)
        tk.Label(dialog, text="Choose a destination to save the files", bg=color, fg=TEXT,
                 font=("Segoe UI Semibold", 13)).pack()
        tk.Label(dialog, text=subtitle, bg=color, fg=MUTED).pack(pady=(4, 12))
        footer = tk.Frame(dialog, bg=color)
        footer.pack(side="bottom", fill="x", padx=24, pady=14)
        panel = tk.Frame(dialog, bg="#282c32", padx=8, pady=8)
        panel.pack(fill="both", expand=True, padx=24)
        tk.Label(panel, text="Local", bg="#424750", fg=TEXT, pady=5).pack(fill="x", pady=(0, 6))
        choice = tk.StringVar(dialog)
        drives = ttk.Treeview(panel, columns=("space",), show="tree", height=3, selectmode="browse")
        drives.column("#0", width=270)
        drives.column("space", width=140, anchor="e")
        drives.pack(fill="both", expand=True)
        for row, (root, _) in self.drive_roots.items():
            try:
                space = shutil.disk_usage(root)
            except OSError:
                continue
            label = self.volume_names[row]
            drives.insert("", "end", iid=row, text=f"{label} ({root})", image=self.icons["Folder"],
                          values=(f"{size_label(space.free)} free",))
        def select_drive(_event):
            selection = drives.selection()
            if selection:
                choice.set(self.drive_roots[selection[0]][0])
        drives.bind("<<TreeviewSelect>>", select_drive)
        field = tk.Frame(panel, bg="#282c32")
        field.pack(fill="x", pady=(6, 0))
        def browse():
            folder = filedialog.askdirectory(parent=dialog, title="Choose recovery folder")
            if folder:
                drives.selection_remove(*drives.selection())
                choice.set(folder)
        ttk.Button(field, text="Choose folder…", command=browse).pack(side="left", padx=(0, 6))
        ttk.Entry(field, textvariable=choice).pack(side="left", fill="x", expand=True)
        result = []
        def accept():
            path = Path(choice.get())
            if not path.is_absolute() or not path.is_dir():
                messagebox.showerror("Recovery destination", "Choose an existing folder.", parent=dialog)
                return
            result.append(choice.get())
            shade.destroy()
        ttk.Button(footer, text="Save", width=12, style="Digger.TButton", command=accept).pack(side="right")
        dialog.bind("<Return>", lambda _: accept())
        self.wait_window(shade)
        return result[0] if result else ""

    def save_selected(self):
        if self.saving:
            return
        selection = self.recovery_selection()
        if not selection:
            return
        folder = self.choose_recovery_folder(f"{len(selection):,} selected file{'s' if len(selection) != 1 else ''}  ·  {size_label(sum(self.metadata[k].st_size for k in selection))}")
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
        self.save_status.configure(text="Recovering selected files…")
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
        self.save_status.configure(text=f"Recovered {saved} of {count} selected files")
        self.demo.configure(state="disabled" if self.saving or self.job is not None else "normal")
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
            if self.job is None or self.job["status"] not in ("running", "preparing"):
                return
            self.job["status"] = "stopping"
            if self.job.get("process"):
                try:
                    self.job["process"].terminate()
                except OSError:
                    pass
        self.stop_button.configure(state="disabled")
        self.stop_button.configure(text="Cancel requested")
        self.set_scan_status("Canceling scan…", "#f8cb83")

    def close(self):
        if self.saving:
            messagebox.showinfo("Recovery in progress", "Wait for the selected files to finish saving.")
            return
        if self.job is not None:
            messagebox.showinfo("Scan in progress", "Wait for the scan to finish or click Cancel scan.")
            return
        self.destroy()


if __name__ == "__main__":
    RecoveryWindow().mainloop()
