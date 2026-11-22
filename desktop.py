"""Portable desktop interface for the bundled Windows recovery engines."""
import argparse
import csv
import hashlib
import lzma
import json
from collections import Counter, deque
import ctypes
from ctypes import wintypes
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

from media_playback import play_sector_video
from sector_catalog import SectorFile, scan_sectors
from recovery_output import atomic_output, output_name
from recovered_format import correct_extension, unique_path
from ntfs_recovery import Candidate, extract as extract_ntfs, scan as scan_ntfs, seekable_preview, ntfs_record
from windows_volumes import (check_volume_read_access, is_elevated, local_volumes,
                             require_separate_disk, selected_scope, source_volume)

from media_preview import IMAGE_TYPES, VIDEO_TYPES, decode_preview
from recycle_bin import current_user_bins, emptied_items, ordinary, scan_recycle_bin
from server import LOCK, RUNS, run_job

if os.name == "nt":
    runtime = Path(sys.executable).parent
    os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")

BG = "#1d2230"
DARK = "#0e121d"
PANEL = "#1d2331"
SURFACE = "#2a3142"
BORDER = "#404758"
ACCENT = "#087fed"
TEXT = "#f2f6ff"
MUTED = "#aeb9cc"
SELECT = "#34425c"
SIZE_FILTERS = {"All sizes": 0, "At least 100 KB": 100_000, "At least 1 MB": 1_000_000,
                "At least 10 MB": 10_000_000, "At least 100 MB": 100_000_000}
THUMBNAIL_BOUNDS = (288, 208)
FILE_STATUSES = ("All statuses", "Deleted record", "Still in Recycle Bin", "Raw scan · status unknown")
PREFERENCES = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Digger" / "preferences.json"
SORTS = ("Discovery order", "Name", "Size", "File date", "Type", "Recoverability")
GROUPS = ("None", "Original folder", "Type", "File date", "Recoverability")
CHECKS = {
    "Recovery failed": (0, "#ffa4a9"),
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


def button_surfaces(style):
    """Stretchable, antialiased control surfaces for the Windows results browser."""
    images = []

    def plate(fill, edge):
        image = Image.new("RGBA", (72, 72))
        ImageDraw.Draw(image).rounded_rectangle((1, 1, 70, 70), radius=17, fill=fill, outline=edge, width=2)
        result = ImageTk.PhotoImage(image.resize((24, 24), Image.Resampling.LANCZOS))
        images.append(result)
        return result

    for name, base, active, selected, edge in (
            ("Action", "#262c3a", "#333c50", "#344564", "#495263"),
            ("Primary", "#087ff0", "#258ffc", "#006bd1", "#46baff"),
            ("Segment", "#272d3c", "#354058", "#0878df", "#353e50"),
            ("Filter", PANEL, "#30384b", SELECT, PANEL)):
        style.element_create(name + ".surface", "image", plate(base, edge),
                             ("disabled", plate("#252c3b", "#363d4d")),
                             ("pressed", plate(selected, ACCENT)), ("selected", plate(selected, ACCENT)),
                             ("active", plate(active, edge)), border=7, padding=2, sticky="nsew")
    for name, kind in (("TButton", "Action"), ("Digger.TButton", "Primary"),
                       ("Toolbutton", "Segment"), ("Filter.TMenubutton", "Filter")):
        children = [("Button.label", {"sticky": "nswe"})]
        if name.endswith("Menubutton"):
            children.insert(0, ("Menubutton.indicator", {"side": "right", "sticky": "e"}))
        style.layout(name, [(kind + ".surface", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": children})]})])
    style.configure("Filter.TMenubutton", padding=(7, 6), arrowcolor=MUTED)
    return images


def file_icon(kind, size=20):
    colors = {"Pictures": "#00a9f0", "Videos": "#7469f6", "Documents": "#f6a330",
              "Audio": "#37ae7c", "Archives": "#b786ea", "Folder": "#efaa31",
              "Other files": "#687b99"}
    picture = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(picture)
    fill = colors.get(kind, colors["Other files"])
    if kind == "Folder":
        draw.rounded_rectangle((3, 13, 32, 39), radius=6, fill=fill)
        draw.rounded_rectangle((3, 20, 61, 54), radius=6, fill=fill)
        draw.rounded_rectangle((4, 22, 61, 53), radius=5, fill="#ffbd42")
        draw.line((8, 24, 55, 24), fill="#ffe092", width=3)
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


class ResultsGrid(tk.Canvas):
    card_height = 210
    base_width = 208

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG, highlightthickness=0, takefocus=True, yscrollincrement=24)
        self.app = app
        self.cards = []
        self.headers = []
        self.draw_request = None
        self.photos = {}
        self.zoom = 100
        self.bind("<Configure>", lambda _: self.layout_cards())
        self.bind("<Map>", lambda _: self.layout_cards())
        self.bind("<Button-1>", self.click_card)
        self.bind("<Double-1>", lambda event: self.click_card(event, double=True))
        self.bind("<MouseWheel>", self.scroll_wheel)
        self.bind("<Control-MouseWheel>", self.zoom_wheel)
        self.bind("<Prior>", lambda _: self.scroll_page(-1))
        self.bind("<Next>", lambda _: self.scroll_page(1))
        self.bind("<space>", lambda _: app.check_focused())
        self.bind("<Control-a>", app.select_all)
        self.bind("<Return>", lambda _: app.preview_clicked())
        for key, delta in (("Left", -1), ("Right", 1), ("Up", -1), ("Down", 1)):
            self.bind(f"<{key}>", lambda _, k=key, d=delta: self.move_focus(k, d))

    def layout_cards(self):
        if not self.winfo_manager():
            return
        self.cards.clear()
        self.headers.clear()
        width = self.winfo_width()
        columns = max(1, (width - 12) // round(self.base_width*self.zoom/100))
        self.card_height = round(144*self.zoom/100) + 66
        self.image_height = self.card_height - 70
        self.columns = columns
        self.card_width = max(1, (width - 8) // columns - 8)
        y, column = 8, 0
        tree = self.app.tree
        for row in tree.get_children():
            if row in self.app.files:
                keys = (row,)
            else:
                if column:
                    y += self.card_height
                    column = 0
                self.headers.append((y, tree.item(row, "text")))
                y += 30
                keys = tree.get_children(row)
            for key in keys:
                x = 8 + column * (self.card_width + 8)
                self.cards.append((key, x, y))
                column += 1
                if column == columns:
                    y += self.card_height
                    column = 0
        self.configure(scrollregion=(0, 0, width, y + (self.card_height if column else 0)))
        self.invalidate()

    def invalidate(self):
        if self.draw_request is None and self.winfo_ismapped():
            self.draw_request = self.after_idle(self.paint)

    def visible_cards(self):
        first = self.canvasy(0)
        last = first + self.winfo_height()
        return [(key, x, y) for key, x, y in self.cards if y + self.card_height > first and y < last]

    def paint(self):
        self.draw_request = None
        self.delete("all")
        photos = {}
        if not self.cards:
            self.photos.clear()
            self.create_text(18, 20, anchor="nw", text="No matching files", fill=MUTED)
            return
        first = self.canvasy(0)
        last = first + self.winfo_height()
        for y, label in self.headers:
            if first - 30 < y < last:
                self.create_text(10, y+4, text=label, fill=MUTED, anchor="nw")
        selected = set(self.app.selected_files())
        for key, x, y in self.visible_cards():
            path = self.app.display_path(key)
            kind = GROUP_BY_SUFFIX.get(path.suffix.lower(), "Other files")
            right = x + self.card_width
            middle = (x + right) // 2
            highlighted = key in selected or key in self.app.checked
            self.create_rectangle(x, y, right, y+self.card_height-10, fill=SURFACE if highlighted else PANEL,
                                  outline=ACCENT if highlighted else BORDER, width=2 if highlighted else 1)
            self.create_rectangle(x+5, y+5, right-5, y+self.image_height+5, fill=DARK, outline="")
            thumbnail = self.app.grid_thumbnails.get(key)
            image = self.app.large_icons[kind]
            if thumbnail is not None:
                bounds = (max(1, self.card_width-16), self.image_height-4)
                cache_key = key, id(thumbnail), bounds
                image = self.photos.get(cache_key)
                if image is None:
                    resized = thumbnail.copy()
                    resized.thumbnail(bounds, Image.Resampling.LANCZOS)
                    image = ImageTk.PhotoImage(resized)
                photos[cache_key] = image
            self.create_image(middle, y+5+self.image_height//2, image=image)
            checked = key in self.app.checked
            self.create_rectangle(x+12, y+12, x+30, y+30, fill=ACCENT if checked else BG, outline=MUTED)
            if checked:
                self.create_line(x+15, y+21, x+19, y+25, x+27, y+17, fill="white", width=2)
            chars = max(8, self.card_width // 8)
            name = path.name if len(path.name) <= chars else path.name[:chars-1] + "…"
            self.create_text(x+12, y+self.image_height+19, text=name, fill=TEXT, anchor="w",
                             width=max(1, self.card_width-20), font=("Segoe UI Semibold", 10))
            state = " · No preview" if key in self.app.preview_errors and thumbnail is None else ""
            self.create_text(x+12, y+self.image_height+41,
                             text=f"{path.suffix.lstrip('.').upper()} · {size_label(self.app.metadata[key].st_size)}{state}",
                             fill=MUTED, anchor="w", width=max(1, self.card_width-20))
        self.photos = photos

    def scroll_page(self, delta):
        self.yview_scroll(delta, "pages")
        return "break"

    def change_zoom(self, value, anchor=None, screen_y=0):
        value = max(60, min(170, round(float(value))))
        if value == self.zoom:
            return
        if anchor is None:
            cards = self.visible_cards()
            anchor = cards[0][0] if cards else None
        self.zoom = value
        self.app.grid_zoom.set(value)
        self.app.grid_zoom_label.configure(text=f"{value}%")
        self.layout_cards()
        if anchor:
            position = next((y for key, _, y in self.cards if key == anchor), None)
            if position is not None:
                bottom = int(self.cget("scrollregion").split()[-1])
                self.yview_moveto(max(0, position-screen_y) / max(1, bottom))

    def zoom_wheel(self, event):
        hit = self.hit(event)
        direction = 1 if event.delta > 0 else -1
        self.change_zoom(self.zoom + direction * 10, hit[0] if hit else None, event.y if hit else 0)
        return "break"

    def scroll_wheel(self, event):
        self.yview_scroll(-int(event.delta / 120) * 3, "units")
        return "break"

    def hit(self, event):
        return next(((key, x, y) for key, x, y in self.visible_cards()
                     if x <= event.x <= x+self.card_width and y <= self.canvasy(event.y) <= y+self.card_height-10), None)

    def click_card(self, event, double=False):
        hit = self.hit(event)
        if hit is None:
            return
        key, x, y = hit
        already_selected = key in self.app.selected_files()
        self.focus_set()
        self.select_card(key)
        if event.x <= x+34 and self.canvasy(event.y) <= y+34:
            self.app.set_checked(key)
        elif double and already_selected:
            self.app.preview_clicked()

    def select_card(self, key):
        self.app.tree.selection_set(key)
        self.app.tree.focus(key)
        self.app.tree.see(key)
        self.app.show_preview()

    def move_focus(self, key, delta):
        if not self.cards:
            return "break"
        focused = self.app.tree.focus()
        index = next((i for i, card in enumerate(self.cards) if card[0] == focused), None)
        advance = delta * (self.columns if key in ("Up", "Down") else 1)
        new_index = 0 if index is None else max(0, min(len(self.cards)-1, index+advance))
        selected, _, y = self.cards[new_index]
        self.select_card(selected)
        first = self.canvasy(0)
        if not first <= y <= first+self.winfo_height()-self.card_height:
            bottom = int(self.cget("scrollregion").split()[-1])
            self.yview_moveto(y / max(1, bottom))
        return "break"


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
        self.facet_bytes = Counter()
        self.extensions = set()
        self.facets = {}
        self.facet_selections = {"Folder": None, "Type": None}
        self.facet_mode = tk.StringVar(value="Folder")
        self.search = tk.StringVar()
        self.format = tk.StringVar(value="All file types")
        self.job = None
        self.checked = set()
        self.name_hint = tk.StringVar()
        self.movie_filter = tk.StringVar(value="All file types")
        self.scan_area = tk.StringVar(value="Whole volume")
        self.scan_depth = tk.StringVar(value="Quick + deep")
        self.raw_source = None
        self.output_folder = None
        self.page_number = 0
        self.page_size = 250
        self.test_folders = []
        self.next_render = 0
        self.completed_job = None
        self.saving = False
        self.last_recovery_report = None
        self.save_events = queue.SimpleQueue()
        self.files = {}
        self.originals = {}
        self.result_view = tk.StringVar(value="Details")
        self.preview_errors = {}
        try:
            with PREFERENCES.open(encoding="utf-8") as saved:
                value = json.load(saved)["recovery_destination"]
            self.last_destination = value if isinstance(value, str) else ""
        except (OSError, ValueError, KeyError, TypeError):
            self.last_destination = ""
        self.grid_thumbnails = {}
        self.bin_view = False
        self.deleted_dates = {}
        self.deleted_years = set()
        self.modified_years = set()
        self.modified_year_filter = tk.StringVar(value="All dates")
        self.year_filter = tk.StringVar(value="All dates")
        self.size_filter = tk.StringVar(value="All sizes")
        self.status_filter = tk.StringVar(value=FILE_STATUSES[0])
        self.materialized = {}
        self.metadata = {}
        self.checks = {}
        self.recovery_checks = {}
        self.sort = tk.StringVar(value="Discovery order")
        self.direction = tk.StringVar(value="Ascending")
        self.group = tk.StringVar(value="None")
        self.preview_events = queue.SimpleQueue()
        self.play_errors = queue.SimpleQueue()
        self.preview_pending = None
        self.preview_active = False
        self.preview_key = None
        self.match_count = 0
        self.match_bytes = 0
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
        self.forward_stack = []
        self.returned_from_results = False
        self.scan_source = "Results"
        self.build()
        if len(sys.argv) > 1:
            parser = argparse.ArgumentParser()
            parser.add_argument("--mode", choices=("live_ntfs", "formatted", "bin", "folder"), default="live_ntfs")
            parser.add_argument("--source", default="")
            parser.add_argument("--name", default="")
            parser.add_argument("--movies", action="store_true")
            self.after_idle(self.restore_launch, parser.parse_args())
        if os.name == "nt":
            self.bind("<Button-4>", self.facet_back)
            self.bind("<Button-5>", self.facet_forward)
        self.bind("<Alt-Left>", self.facet_back)
        self.bind("<Alt-Right>", self.facet_forward)
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
        style.configure("Digger.TButton", background=ACCENT, foreground="white", borderwidth=0, padding=(18, 12), font=("Segoe UI Semibold", 12))
        style.map("Digger.TButton", background=[("disabled", SURFACE), ("active", "#396ac4")],
                  foreground=[("disabled", MUTED), ("active", "white")])
        style.configure("Scope.TButton", background=SELECT, foreground=TEXT, padding=(8, 6))
        style.map("Scope.TButton", background=[("active", "#365b8b")])
        style.configure("Preview.TButton", background="#304e7c", foreground="white")
        style.map("Preview.TButton", background=[("active", "#3c6199"), ("disabled", SURFACE)])
        style.configure("TEntry", fieldbackground=SURFACE, insertcolor=TEXT, padding=7)
        style.configure("TCombobox", fieldbackground=SURFACE, arrowcolor=TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE)], foreground=[("readonly", TEXT)])
        style.configure("TMenubutton", background=SURFACE, padding=(8, 6))
        style.configure("Treeview", rowheight=36, fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL, foreground=TEXT,
                        relief="flat", padding=(7, 6))
        style.map("Treeview.Heading", background=[("active", SELECT)])
        style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 9), background=PANEL, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", SELECT)],
                  foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE)

        self.control_images = button_surfaces(style)
        top = tk.Frame(self, bg=DARK)
        top.pack(fill="x")
        mark = tk.Canvas(top, width=34, height=34, bg=DARK, highlightthickness=0)
        mark.pack(side="left", padx=(22, 0))
        mark.create_oval(3, 3, 27, 27, outline="#79abff", width=3)
        mark.create_arc(8, 8, 22, 22, start=20, extent=225, style="arc", outline="#79abff", width=2)
        mark.create_line(23, 23, 31, 31, fill="#79abff", width=4)
        tk.Label(top, text="Digger Data Recovery", font=("Segoe UI Semibold", 11), bg=DARK,
                 fg=TEXT, padx=10, pady=8).pack(side="left")

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
        self.activity = tk.Label(self, anchor="w", padx=18, pady=4, bg="#232c40", fg="#b9d9fc")
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
        self.page_choice = tk.StringVar()
        for label, page in (("Locations", self.scan_page), ("Results", self.results_page)):
            ttk.Radiobutton(top, text=label, style="Toolbutton", value=str(page), variable=self.page_choice,
                            command=lambda target=page: self.tabs.select(target)).pack(side="left", padx=4)

        self.build_scan()
        self.build_results()
        def sync_status(_event=None):
            self.page_choice.set(self.tabs.select())
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
                   ("Advanced recovery", (("formatted", "Deep Scan", "Drive"), ("recover", "Deep scan image", "Image"),
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
        self.location_heading = tk.Label(heading, bg=BG, fg=TEXT, font=("Segoe UI Semibold", 22))
        self.location_heading.pack(side="left")
        ttk.Button(heading, text="Refresh drives", command=self.refresh_drives).pack(side="right", padx=(8, 0))
        self.drives_box = tk.Frame(content, bg=BG)
        self.drives_box.pack(fill="x")
        self.locations_box = tk.Frame(content, bg=BG)
        self.locations_box.pack(fill="x", pady=(12, 0))
        tk.Label(self.locations_box, text="Quick access", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(0, 10))
        shortcuts = tk.Frame(self.locations_box, bg=BG)
        shortcuts.pack(fill="x")
        self.shortcuts = {}
        self.desktop_path = ""
        if os.name == "nt":
            folder = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, folder) == 0:
                self.desktop_path = folder.value
        for index, (kind, title) in enumerate((("Bin","Recycle Bin"), ("Desktop","Desktop"), ("Folder","Select folder"), ("Image","Disk image"))):
            button = tk.Button(shortcuts, text="   " + title, image=self.location_icons[kind,48], compound="left",
                               anchor="w", bg=PANEL, fg=TEXT, activebackground=SELECT, activeforeground=TEXT,
                               highlightbackground=BORDER, highlightcolor=ACCENT, highlightthickness=1,
                               padx=10, pady=12, bd=0, cursor="hand2", command=lambda k=kind: self.shortcut(k))
            self.shortcuts[kind] = (button, title)
            button.grid(row=index//2, column=index%2, sticky="ew", padx=(0, 8), pady=4)
            shortcuts.columnconfigure(index%2, weight=1, uniform="locations")
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
        self.depth_options = ttk.Frame(content)
        ttk.Label(self.depth_options, text="Search depth").pack(side="left", padx=(0, 8))
        depth = ttk.Combobox(self.depth_options, state="readonly", textvariable=self.scan_depth,
                             values=("Quick + deep", "Quick only"), width=18)
        depth.pack(side="left")
        self.folder_depth = ttk.Label(self.depth_options, text="Deleted records in this folder")
        self.depth_picker = depth
        depth.bind("<<ComboboxSelected>>", lambda _: self.update_source())
        self.carve_options = ttk.Frame(content)
        ttk.Label(self.carve_options, text="Look for").pack(side="left")
        ttk.Combobox(self.carve_options, state="readonly", textvariable=self.movie_filter,
                     values=("All file types", "MP4 / MOV movies"), width=24).pack(side="left", padx=8)
        self.area_options = area = ttk.Frame(self.carve_options)
        area.pack(side="left", padx=(14, 0))
        ttk.Label(area, text="Scan area").pack(side="left", padx=(0, 6))
        ttk.Combobox(area, state="readonly", textvariable=self.scan_area,
                     values=("Whole volume", "Unallocated space only"), width=22).pack(side="left")
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
            if self.mode.get() == "formatted":
                drive = PureWindowsPath(self.source_text.get()).anchor
                if not drive:
                    messagebox.showinfo("Recycle Bin", "Choose the drive containing the Recycle Bin first.", parent=self)
                    return
                try:
                    path = next(path for path in current_user_bins() if PureWindowsPath(path).anchor.casefold() == drive.casefold())
                except (OSError, StopIteration, subprocess.SubprocessError):
                    messagebox.showerror("Recycle Bin", "Could not locate your Recycle Bin on this drive.", parent=self)
                    return
                self.source_text.set(str(path))
            else:
                self.source_text.set("Recycle Bin")
        elif kind == "Desktop":
            self.source_text.set(self.desktop_path)
        else:
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
            tk.Label(self.drives_box, text=f"{group} ({len(roots)})", bg=BG, fg=TEXT,
                     font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(4,8))
            grid = tk.Frame(self.drives_box, bg=BG)
            grid.pack(fill="x", pady=(0,12))
            for i, root in enumerate(roots):
                card = tk.Canvas(grid, width=214, height=80, bg=BG, highlightthickness=0, takefocus=True, cursor="hand2")
                card.grid(row=i//2, column=i%2, sticky="w", padx=(0,8), pady=3)
                self.cards[root] = card
                card.create_polygon(10,1,10,1,204,1,204,1,213,1,213,10,213,10,213,70,213,70,213,79,204,79,
                                    204,79,10,79,10,79,1,79,1,70,1,70,1,10,1,10,1,1,
                                    smooth=True, outline=BORDER, fill=PANEL, tags="surface")
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

    def show_admin_warning(self, title, text):
        if self.admin_warning is not None and self.admin_warning.winfo_exists():
            self.admin_warning.destroy()
        panel = self.admin_warning = tk.Frame(self, bg=SURFACE, padx=14, pady=10,
                                               highlightbackground="#e4b75f", highlightthickness=1)
        tk.Label(panel, text=title, anchor="w", bg=SURFACE,
                 fg="#ffd484", font=("Segoe UI Semibold", 11)).grid(row=0, column=0, sticky="w")
        tk.Label(panel, text=text,
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

    def choose_media_drive(self):
        self.pick_mode("formatted")
        self.tabs.select(self.scan_page)
        selected = self.selected_files()
        path = self.originals.get(selected[0]) if selected else None
        folder = self.facet_selections["Folder"]
        if path is None and folder:
            path = PureWindowsPath(folder[0])
        if path is None:
            path = PureWindowsPath(self.scan_source)
        for root in self.drive_roots:
            if root.casefold() == path.anchor.casefold():
                self.source_text.set(root)
                break
        filesystem = self.drive_roots.get(self.source_text.get(), ("", ""))[1].upper()
        self.scan_area.set("Unallocated space only" if filesystem == "NTFS" else "Whole volume")
        self.source_label.configure(text="Choose the original drive. A sector scan can find media after its name, date and bin record are gone.")

    def continue_deep_scan(self):
        drive = self.deep_drive
        self.mode.set("formatted")
        self.change_source_mode()
        self.source_text.set(drive)
        self.scan_area.set("Whole volume" if self.deep_already_ran else "Unallocated space only")
        self.tabs.select(self.scan_page)

    def toggle_inspector(self):
        panel = self.inspector_panel
        if self.inspector_visible.get():
            if str(panel) not in self.split.panes():
                self.split.add(panel, weight=1)
                self.split.update_idletasks()
                self.split.sashpos(1, self.split.winfo_width() - min(340, max(230, self.split.winfo_width() // 4)))
        elif str(panel) in self.split.panes():
            self.split.forget(panel)

    def build_results(self):
        page = self.results_page

        heading = ttk.Frame(page, padding=(12, 8))
        heading.pack(side="bottom", fill="x")
        self.save_status = ttk.Label(heading, foreground=TEXT)
        self.save_status.pack(side="left")
        self.recovery_details_button = ttk.Button(heading, text="Recovery details", command=self.show_recovery_report)
        self.media_drive_button = ttk.Button(heading, text="Deep scan a drive…", command=self.choose_media_drive)
        self.deep_button = ttk.Button(heading, text="Deep scan this drive…", command=self.continue_deep_scan)
        self.open_output = ttk.Button(heading, text="Open recovery folder", state="disabled",
                                      command=lambda: os.startfile(self.output_folder))

        navigation_bar = ttk.Frame(page, padding=(14, 9, 14, 9), style="Navigation.TFrame")
        ttk.Style(self).configure("Navigation.TFrame", background=DARK)
        navigation_bar.pack(fill="x")
        ttk.Button(navigation_bar, text="⌂  Home", width=7, command=lambda: self.tabs.select(self.scan_page)).pack(side="left", padx=(0, 8))
        self.back_button = ttk.Button(navigation_bar, text="←  Back", width=7, command=self.facet_back)
        self.back_button.pack(side="left", padx=(0, 4))
        self.forward_button = ttk.Button(navigation_bar, text="Forward  →", width=9, state="disabled", command=self.facet_forward)
        self.forward_button.pack(side="left", padx=(0, 4))
        self.up_button = ttk.Button(navigation_bar, text="↑  Up", width=5, state="disabled", command=self.facet_up)
        self.up_button.pack(side="left")
        self.breadcrumb = ttk.Frame(navigation_bar, style="Navigation.TFrame")

        tools = ttk.Frame(page, padding=(16, 9, 16, 9))
        tools.pack(fill="x")
        self.filter_chips = ttk.Frame(tools)
        self.filter_chips.pack(side="right")
        self.scope_buttons = {}
        for mode in ("Folder", "Type"):
            self.scope_buttons[mode] = ttk.Button(self.filter_chips, style="Scope.TButton",
                                                 command=lambda facet=mode: self.clear_scope(facet))
        self.search_entry = ttk.Entry(navigation_bar, textvariable=self.search, width=20)
        self.bind("<Control-f>", self.focus_search)
        views = []
        for view, label in (("Details", "☰  List"), ("Grid", "▦  Grid")):
            views.append(ttk.Radiobutton(navigation_bar, text=label, value=view, variable=self.result_view,
                                         style="Toolbutton", command=self.switch_result_view))
        sort_button = ttk.Menubutton(navigation_bar, text="Sort", width=5)
        sort_button.pack(side="right", padx=4)
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
        self.filter_button = ttk.Menubutton(tools, text="File type", style="Filter.TMenubutton")
        self.filter_button.pack(side="left", padx=4)
        self.filter_menu = tk.Menu(self.filter_button, tearoff=False, postcommand=self.populate_filters)
        self.filter_button.configure(menu=self.filter_menu)
        self.status_button = ttk.Menubutton(tools, text="File status", style="Filter.TMenubutton", width=12)
        self.status_button.pack(side="left", before=self.filter_button, padx=(0, 4))
        status_menu = tk.Menu(self.status_button, tearoff=False)
        for status in FILE_STATUSES:
            status_menu.add_radiobutton(label=status, variable=self.status_filter, value=status,
                                        command=self.change_status_filter)
        self.status_button.configure(menu=status_menu)
        self.date_button = ttk.Menubutton(tools, text="Date modified", style="Filter.TMenubutton")
        self.date_button.pack(side="left", padx=4)
        dates = tk.Menu(self.date_button, tearoff=False)
        def populate_dates():
            dates.delete(0, "end")
            for year in ("All dates", *sorted(self.modified_years, reverse=True), "Unknown date"):
                dates.add_radiobutton(label=year, variable=self.modified_year_filter, value=year, command=self.change_status_filter)
        dates.configure(postcommand=populate_dates)
        self.date_button.configure(menu=dates)
        self.size_button = ttk.Menubutton(tools, text="File size", style="Filter.TMenubutton")
        self.size_button.pack(side="left", padx=4)
        sizes = tk.Menu(self.size_button, tearoff=False)
        for label in SIZE_FILTERS:
            sizes.add_radiobutton(label=label, variable=self.size_filter, value=label, command=self.change_status_filter)
        self.size_button.configure(menu=sizes)
        ttk.Button(tools, text="Reset filters", command=self.clear_filters).pack(side="right", padx=6)
        self.search_entry.pack(side="right", before=sort_button, padx=(8, 0))
        for view_button in views:
            view_button.pack(side="right")
        self.breadcrumb.pack(side="left", fill="x", expand=True, padx=8)
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

        self.export_button = ttk.Button(self.banner, text="Recover", width=16, style="Digger.TButton",
                                        command=self.save_selected, state="disabled")
        self.export_button.pack(side="right", before=self.status_label, padx=(12, 4))
        self.selection_summary = tk.Label(self.banner, text="0 files selected", bg=PANEL, fg=TEXT,
                                          font=("Segoe UI Semibold", 11), anchor="e")
        self.selection_summary.pack(side="right", before=self.status_label)


        self.search.trace_add("write", lambda *_: self.render_files())
        self.format.trace_add("write", lambda *_: self.render_files())

        border = tk.Frame(page, bg=BORDER, padx=1, pady=1)
        border.pack(fill="both", expand=True, padx=14)
        split = ttk.Panedwindow(border, orient="horizontal")
        split.pack(fill="both", expand=True)
        navigation = ttk.Frame(split, padding=(8, 10, 8, 0), width=245)
        split.add(navigation, weight=0)

        facet_tabs = ttk.Frame(navigation)
        facet_tabs.pack(fill="x", pady=(0, 8))
        for name in ("Folder", "Type"):
            ttk.Radiobutton(facet_tabs, style="Toolbutton", text="File location" if name == "Folder" else "File type", value=name, variable=self.facet_mode,
                            command=self.refresh_facets).pack(side="left", expand=True, fill="x")
        self.facet_list = ttk.Treeview(navigation, columns=("count", "size"), show="tree headings",
                                       selectmode="browse", height=8)
        self.facet_list.heading("#0", text="Folder")
        self.facet_list.heading("count", text="Files")
        self.facet_list.heading("size", text="Total size")
        self.facet_list.column("#0", width=154, minwidth=116)
        self.facet_list.column("count", width=55, minwidth=50, stretch=False, anchor="e")
        self.facet_list.column("size", width=76, minwidth=68, stretch=False, anchor="e")
        facet_scroll = ttk.Scrollbar(navigation, orient="vertical", command=self.facet_list.yview)
        self.facet_list.configure(yscrollcommand=facet_scroll.set)
        facet_scroll.pack(side="right", fill="y")
        self.facet_list.pack(fill="both", expand=True)
        self.facet_list.bind("<Button-1>", self.check_facet)
        self.facet_list.bind("<<TreeviewSelect>>", self.select_facet)
        self.refresh_facets(render=False)
        listing = ttk.Frame(split, padding=(6, 0, 6, 0))
        split.add(listing, weight=3)
        result_header = ttk.Frame(listing, padding=(8, 7))
        result_header.pack(fill="x")
        self.scope_title = ttk.Label(result_header, text="All results", font=("Segoe UI Semibold", 15))
        self.scope_title.pack(side="left")
        self.scope_summary = ttk.Label(result_header, foreground=MUTED, font=("Segoe UI", 10))
        self.scope_summary.pack(side="right")
        self.tree = ttk.Treeview(listing, columns=("folder", "type", "size", "date", "check"), selectmode="extended")
        self.tree["columns"] = ("pick", "folder", "type", "size", "date", "status", "check")
        self.tree.heading("pick", text="✓")
        self.tree.column("pick", width=35, minwidth=30, stretch=False, anchor="center")
        self.tree.bind("<Button-1>", self.toggle_checked)
        self.tree.bind("<space>", self.check_focused)
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.configure(displaycolumns=("size", "type", "date", "folder", "status"))
        self.tree.heading("status", text="File status")
        self.tree.column("status", width=115, minwidth=78)
        self.tree.heading("folder", text="Original folder")
        self.tree.heading("type", text="Type")
        self.tree.heading("size", text="Size")
        self.tree.heading("date", text="Date modified")
        self.tree.heading("check", text="Recoverability")
        for key, width in (("#0", 265), ("folder", 150), ("type", 90), ("size", 85), ("date", 120), ("check", 153)):
            self.tree.column(key, width=width, minwidth=45)
        for label in CHECKS:
            self.tree.tag_configure(label, foreground=CHECKS[label][1] if label == "Recovery failed" else TEXT)
        self.tree.tag_configure("alternate", background="#222838")
        self.tree.tag_configure("group", background=SURFACE, foreground=TEXT)
        self.extension_rows = {}
        for key, field in (("#0", "Name"), ("type", "Type"), ("size", "Size"),
                           ("date", "File date"), ("check", "Recoverability")):
            self.tree.heading(key, command=lambda field=field: self.sort_by(field))
        bar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.list_scrollbar = bar

        self.tree.bind("<<TreeviewSelect>>", self.show_preview)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Return>", self.open_result)
        self.tree.bind("<Double-1>", self.open_clicked)
        self.tree.bind("<Button-3>", self.show_file_menu)
        self.file_menu = tk.Menu(self, tearoff=False, bg=SURFACE, fg=TEXT)
        self.file_menu.add_command(label="Recover selected…", command=self.save_selected)
        self.file_menu.add_command(label="Copy original path", command=self.copy_original_path)
        xbar = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=xbar.set)
        self.list_horizontal = xbar
        selection_bar = ttk.Frame(listing)
        selection_bar.pack(side="bottom", fill="x")
        self.next_page = ttk.Button(selection_bar, text="Next ›", command=lambda: self.change_page(1))
        self.prev_page = ttk.Button(selection_bar, text="‹ Previous", command=lambda: self.change_page(-1))
        self.next_page.pack(side="right")
        self.prev_page.pack(side="right", padx=4)
        self.footer = ttk.Label(selection_bar, text="0 files", foreground=MUTED)
        self.footer.pack(side="left")
        self.grid_tools = ttk.Frame(selection_bar)
        ttk.Label(self.grid_tools, text="Thumbnail size").pack(side="left", padx=(8, 4))
        self.grid_zoom = tk.DoubleVar(value=100)
        slider = ttk.Scale(self.grid_tools, variable=self.grid_zoom, from_=60, to=170, length=78,
                           command=lambda value: self.results_grid.change_zoom(value))
        slider.pack(side="left")
        self.grid_zoom_label = ttk.Label(self.grid_tools, text="100%", width=5)
        self.grid_zoom_label.pack(side="left", padx=(4, 0))



        self.list_scrollbar.pack(side="right", fill="y")
        self.list_horizontal.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.results_grid = ResultsGrid(listing, self)
        self.grid_scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.results_grid.yview)
        def grid_scroll(first, last):
            self.grid_scrollbar.set(first, last)
            self.results_grid.invalidate()
        self.results_grid.configure(yscrollcommand=grid_scroll)

        inspector_panel = ttk.Frame(split, padding=(10, 0, 0, 0))
        self.split, self.inspector_panel = split, inspector_panel
        self.inspector_visible = tk.BooleanVar(value=False)
        self.preview_toggle = ttk.Checkbutton(navigation_bar, text="Preview", style="Toolbutton",
                                              variable=self.inspector_visible, command=self.toggle_inspector)
        self.preview_toggle.pack(side="left", padx=(4, 0))
        inspector_view = tk.Canvas(inspector_panel, bg=BG, highlightthickness=0, yscrollincrement=22)
        inspector_scroll = ttk.Scrollbar(inspector_panel, command=inspector_view.yview)
        inspector_scroll.pack(side="right", fill="y")
        inspector_view.pack(side="left", fill="both", expand=True)
        inspector_view.configure(yscrollcommand=inspector_scroll.set)
        inspector = ttk.Frame(inspector_view)
        inspector_window = inspector_view.create_window(0, 0, window=inspector, anchor="nw")
        inspector.bind("<Configure>", lambda _: inspector_view.configure(scrollregion=inspector_view.bbox("all")))
        def inspect_scroll(event):
            if str(event.widget).startswith(str(inspector_panel)):
                inspector_view.yview_scroll(-int(event.delta / 120) * 3, "units")
        self.bind("<MouseWheel>", inspect_scroll, add="+")
        def initial_panes(event):
            if event.width > 600:
                split.sashpos(0, min(360, max(286, int(event.width * .23))))

                split.unbind("<Configure>", binding)
        binding = split.bind("<Configure>", initial_panes)
        ttk.Label(inspector, text="File preview", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(6, 8))
        self.preview_symbol = ttk.Label(inspector)
        self.details = ttk.Label(inspector, foreground=MUTED, wraplength=245, justify="left")

        self.preview = ttk.Label(inspector, text="Select an image or video to preview it.", wraplength=245)
        self.preview.pack(anchor="w", pady=(4, 8))
        def fit_inspector(event):
            inspector_view.itemconfigure(inspector_window, width=event.width)
            width = max(40, event.width - 8)
            self.preview.configure(wraplength=width)
            self.details.configure(wraplength=width)
        inspector_view.bind("<Configure>", fit_inspector)
        self.inspector_view = inspector_view
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
        self.canvas = tk.Canvas(canvas_frame, height=190, width=190, bg=PANEL, highlightthickness=0)
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
                  "formatted":"Deep Scan", "folder":"Select the original folder",
                  "recover":"Deep scan a disk image", "ntfs_image":"Search a disk image", "inspect":"Inspect disk partitions"}
        self.location_heading.configure(text=titles[mode])
        for key, button in self.nav_buttons.items():
            button.configure(bg="#313e50" if key == mode else DARK, fg="#82b3ff" if key == mode else TEXT)
        for box in (self.drives_box,self.locations_box,self.path_panel,self.hint_box,self.depth_options,self.carve_options,self.options_button):
            box.pack_forget()
        if mode in ("live_ntfs", "formatted"):
            self.drives_box.pack(fill="x")
        kinds = (("Image",) if mode in ("ntfs_image", "recover", "inspect") else
                 ("Desktop", "Folder") if mode == "folder" else ("Bin", "Desktop", "Folder"))
        for kind, (button, _) in self.shortcuts.items():
            if kind in kinds:
                index = kinds.index(kind)
                button.grid(row=index//2, column=index%2)
            else:
                button.grid_remove()
        self.locations_box.pack(fill="x", pady=(8,0))
        self.path_panel.pack(fill="x", pady=(8,0))
        if mode == "live_ntfs":
            self.depth_options.pack(fill="x", pady=(10, 0))
        self.options_button.configure(text="Hide scan options  ▴" if self.options_open else "Scan options  ▾")
        self.options_button.pack(anchor="w", pady=(10,0))
        if self.options_open or mode == "formatted":
            (self.carve_options if mode == "formatted" else self.hint_box).pack(fill="x", pady=6)

    def change_source_mode(self):
        mode = self.mode.get()
        self.options_open = False
        self.layout_locations()
        value = self.source_paths.get(mode, "Recycle Bin" if mode == "bin" else "")
        if not value and mode in ("live_ntfs", "formatted"):
            value = next((r for r, fs in self.drive_roots.values() if fs.upper()=="NTFS"), next(iter(self.drive_roots),""))
        self.source_text.set(value)

    def update_source(self):
        mode = self.mode.get()
        value = self.source_text.get().strip()
        self.source_paths[mode] = value
        for root, card in self.cards.items():
            selected = PureWindowsPath(root) == PureWindowsPath(value)
            card.itemconfigure("surface", fill=SELECT if selected else PANEL, outline=ACCENT if selected else BORDER)
            card.itemconfigure("check", state="normal" if selected else "hidden")
        path = PureWindowsPath(value)
        bin_target = value == "Recycle Bin"
        kind = None
        if value:
            if mode in ("ntfs_image", "inspect", "recover"):
                kind = "Image"
            elif bin_target or (mode == "formatted" and len(path.parts) == 3 and path.parts[1].lower() == "$recycle.bin"):
                kind = "Bin"
            elif self.desktop_path and path == PureWindowsPath(self.desktop_path):
                kind = "Desktop"
            elif path.is_absolute() and path != PureWindowsPath(path.anchor):
                kind = "Folder"
        for name, (button, title) in self.shortcuts.items():
            selected = name == kind
            button.configure(text=("   ✓   " if selected else "   ") + title,
                             bg=SELECT if selected else PANEL, highlightbackground=ACCENT if selected else BORDER,
                             state="disabled" if name == "Desktop" and not self.desktop_path else "normal")
        labels = {"formatted": "Start deep scan", "live_ntfs": "Scan for deleted files", "bin": "Scan Recycle Bin", "folder": "Scan Recycle Bin",
                  "ntfs_image": "Scan image", "recover": "Deep scan image", "inspect": "Inspect image"}
        self.start_button.configure(text="Search for lost data" if mode == "live_ntfs" else labels[mode])
        self.path_label.configure(text="Drive or folder path" if mode in ("live_ntfs", "folder", "formatted") else "Image file path")
        ready = bool(value) or mode == "bin"
        if bin_target:
            text = "Search retained and emptied Recycle Bin records."
        elif mode == "live_ntfs":
            text = ("Search deleted records first, then scan unallocated space on this drive."
                    if self.scan_depth.get() == "Quick + deep" else "Quick scan reads deleted filesystem records.")
            if value:
                try:
                    scope = selected_scope(os.path.expandvars(value))
                    if scope == PureWindowsPath(scope.anchor):
                        self.folder_depth.pack_forget()
                        self.depth_picker.pack(side="left")
                    else:
                        self.depth_picker.pack_forget()
                        self.folder_depth.pack(side="left")
                        text = f"Search deleted records under {scope}. To include files with unknown paths, deep scan {scope.drive} separately."
                    filesystem = next((fs for root, fs in self.drive_roots.values()
                                       if root.casefold() == scope.anchor.casefold()), None)
                    if filesystem and filesystem.upper() != "NTFS":
                        text = f"{scope.drive} uses {filesystem}. To scan this filesystem, open a disk image."
                        ready = False
                except ValueError as exc:
                    text, ready = str(exc), False
        elif mode == "formatted":
            self.area_options.pack(side="left", padx=(14, 0))
            text = "Browse sector matches as they appear. Choose where to save files when you click Recover."
            if kind in ("Bin", "Folder", "Desktop"):
                self.area_options.pack_forget()
                text = f"Search deleted NTFS records under {path} and scan their surviving free clusters."
        elif mode == "bin":
            text = "Search Recycle Bin records for items originally deleted from this folder."
        elif mode == "folder":
            text = "Find Recycle Bin items originally deleted from this folder."
        else:
            text = "Open a raw disk image such as .img, .dd, or .raw."
        if self.can_scan_drives and mode in ("bin", "folder", "live_ntfs", "formatted"):
            launch = {True: "Running as administrator", False: "Standard launch", None: "Launch privileges unknown"}
            text = f"{launch[self.elevated]} · {text}"
        self.source_label.configure(text=text)
        self.start_button.configure(state="normal" if ready and self.job is None and not self.saving else "disabled")

    def choose_source(self):
        if self.mode.get() in ("bin", "folder", "live_ntfs", "formatted"):
            current = Path(self.source_text.get())
            chosen = filedialog.askdirectory(parent=self, title="Choose the original folder",
                                             initialdir=str(current) if current.is_dir() else None)
        else:
            chosen = filedialog.askopenfilename(parent=self, filetypes=[
                ("Raw disk images", "*.img *.dd *.raw *.bin *.ima"), ("All files", "*.*")])
        if chosen:
            self.source_text.set(chosen)

    def start_scan(self, sample):
        if self.job is not None or self.saving:
            return
        mode = self.mode.get()
        if mode in ("bin", "live_ntfs") and self.source_text.get() == "Recycle Bin":
            mode = "bin"
        elif mode == "bin":
            mode = "folder"
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
            else:
                source = Path(value)
                if not source.is_file():
                    messagebox.showerror("Scan source", "Choose an existing disk image file.")
                    return
        deep_after_quick = (mode == "live_ntfs" and source == PureWindowsPath(source.anchor)
                            and self.scan_depth.get() == "Quick + deep")
        folder_deep = mode == "formatted" and source != PureWindowsPath(source.anchor)
        only_free = folder_deep or deep_after_quick or (mode == "formatted" and self.scan_area.get() == "Unallocated space only")
        if only_free and not any(root.casefold() == source.anchor.casefold() and fs.upper() == "NTFS"
                                 for root, fs in self.drive_roots.values()):
            messagebox.showerror("Scan area", "Folder deep scans require a mounted NTFS volume." if folder_deep else "Unallocated-space scans require a mounted NTFS volume. Choose Whole volume for this source.", parent=self)
            return
        if mode in ("live_ntfs", "formatted"):
            try:
                device = source_volume(source)
                check_volume_read_access(device)
            except OSError as exc:
                if self.needs_admin(exc):
                    self.show_admin_warning("Windows blocked direct drive access",
                                            "Right-click the extracted Digger.exe and choose Run as administrator.")
                else:
                    messagebox.showerror("Cannot read source drive", str(exc))
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
        else:
            folder = Path(tempfile.mkdtemp(dir=RUNS.name))
        self.deep_button.pack_forget()
        self.media_drive_button.pack_forget()
        has_raw_scan = mode == "formatted" or deep_after_quick
        self.raw_source = device if has_raw_scan else None
        self.output_folder = None
        self.open_output.pack_forget()
        self.job = {"folder": folder, "mode": mode, "status": "preparing", "matches": deque(),
                    "self_test": sample, "started": monotonic(), "source": str(source or ("Recycle Bin" if mode == "bin" else "Built-in NTFS test image"))}
        if folder_deep:
            self.job["folder_scope"] = True
        if has_raw_scan:
            self.job.update(device=device, movies_only=mode == "formatted" and self.movie_filter.get() == "MP4 / MOV movies",
                            only_free=only_free, deep_after_quick=deep_after_quick, raw_search=mode == "formatted")
        self.scan_source = self.job["source"]
        self.thumbnails.clear()
        self.grid_thumbnails.clear()
        self.preview_errors.clear()
        self.thumb_tried.clear()
        self.scan_generation += 1
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
        self.filter_button.configure(text="File type")
        indexed = mode in ("live_ntfs", "ntfs_image", "bin", "folder")

        self.facet_mode.set("Folder" if indexed else "Type")
        self.checked.clear()
        self.extensions.clear()
        self.files.clear()
        self.bin_view = mode in ("bin", "folder")
        self.tree.heading("date", text="Date deleted" if self.bin_view else "Date modified")
        self.deleted_dates.clear()
        self.deleted_years.clear()
        self.modified_years.clear()
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
            elif job.get("folder_scope"):
                runs = []
                job["phase"] = "Finding deleted file records belonging to the selected folder"
                for entry in scan_ntfs(job["device"], source, job):
                    if not job["movies_only"] or entry.original.suffix.lower() in (".mp4", ".mov"):
                        job["matches"].append((entry, entry.original))
                    try:
                        _, layout = ntfs_record(entry)
                    except OSError:
                        continue
                    if isinstance(layout, list):
                        runs.extend(layout)
                job["folder_runs"] = runs
                job.pop("records", None)
                job["phase"] = "Scanning free clusters mapped to the selected folder"
                if runs and job["status"] != "stopping":
                    scan_sectors(job, job["device"])
                else:
                    job["sector_matches"] = 0
            elif job["mode"] in ("formatted", "recover"):
                scan_sectors(job, job.get("device") or source)
            elif job.get("deep_after_quick"):
                self.scan_quick_and_deep(job, source)
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
            run_job(job)
        finally:
            try:
                if image.exists():
                    image.chmod(0o600)
                    image.unlink()
            except OSError:
                pass

    def scan_quick_and_deep(self, job, source):
        job["phase"] = "Quick scan · Reading NTFS file records"
        try:
            self.scan_indexed(job, source)
        except OSError as exc:
            job["quick_warning"] = str(exc)
        if job["status"] == "stopping":
            return
        job["quick_records"] = job.pop("records", None)
        job["phase"] = "Deep scan · Searching unallocated space for file contents"
        job["raw_search"] = True
        scan_sectors(job, job["device"])

    def scan_bin(self, job, folder):
        for match in scan_recycle_bin(folder, cancelled=lambda: job["status"] == "stopping"):
            if job["status"] == "stopping":
                return
            job["matches"].append(match)
        volumes = {root.casefold() for root, _, fs in local_volumes() if fs.upper() == "NTFS"}
        searched = 0
        failures = []
        blocked = []
        for bin_path in current_user_bins():
            scope = PureWindowsPath(bin_path)
            if scope.anchor.casefold() not in volumes or (folder and folder.drive.casefold() != scope.drive.casefold()):
                continue
            if job["status"] == "stopping":
                return
            job.pop("records", None)
            job["phase"] = f"Opening {scope.drive} for an emptied-bin search"
            try:
                check_volume_read_access(source_volume(scope))
                job["phase"] = f"Searching {scope.drive} for emptied-bin records"
                for match in emptied_items(scope, job, folder):
                    job["matches"].append(match)
                searched += 1
            except OSError as exc:
                if self.needs_admin(exc):
                    blocked.append(scope.drive)
                label = f"{scope.drive} · {exc}"
                failures.append(label)
        if not searched and not failures:
            failures.append("No mounted NTFS volumes were available for the emptied-bin search.")
        if failures:
            job["bin_search"] = ("partial" if searched else "unavailable", failures, blocked)

    def needs_admin(self, error):
        return self.elevated is not True and getattr(error, "winerror", None) in (5, 1314)

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
            if job.get("raw_search") and job.get("only_free"):
                stage = "Deep scan · Searching unallocated space for file contents"
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
            if job.get("volume_bytes") and "sector_position" in job:
                detail += f"   ·   Reading {size_label(job['sector_position'])} of {size_label(job['volume_bytes'])}"
            stage = job.get("phase", stage)
            if job["status"] == "running":
                percentage = f"  ·  {min(100, 100 * done // max(1, total))}%" if copying or records else ""
                heading = "Deep scan" if job.get("raw_search") or job["mode"] == "recover" else "Scanning"
                self.set_scan_status(heading + percentage, "#91bdff")
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
        if not self.play_errors.empty():
            messagebox.showerror("Video preview", self.play_errors.get_nowait(), parent=self)
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
        byte_counts = Counter()
        scope_bytes = 0
        for path, original, *bin_date in matches:
            key = str(len(self.files))
            try:
                info = path if isinstance(path, (SectorFile, Candidate)) else path.stat()
            except OSError:
                continue
            if bin_date and bin_date[0] is not None:
                self.deleted_dates[key] = bin_date[0]
                self.deleted_years.add(str(bin_date[0].year))
            self.files[key] = path
            self.metadata[key] = info
            if original is not None:
                self.originals[key] = original
            modified = self.modified_date(key)
            if modified != "Unknown":
                self.modified_years.add(modified[:4])
            if isinstance(path, Candidate):
                self.checks[key] = "Deleted " + {"ntfs": "NTFS", "fat": "FAT", "exfat": "exFAT"}[path.filesystem] + " record"
            if self.job and self.job["mode"] in ("bin", "folder"):
                self.checks[key] = "Emptied-bin record" if isinstance(path, Candidate) else "Retained in Recycle Bin"
            elif original is None:
                self.checks[key] = "Carved from drive"
            added.append(key)
            if self.matches_other_facet(key):
                scope_bytes += info.st_size
                for facet in self.facet_path(key, self.facet_mode.get()):
                    counts[facet] += 1
                    byte_counts[facet] += info.st_size
            suffix = self.display_path(key).suffix.upper()
            if suffix:
                self.extensions.add(suffix)
        for value, count in counts.items():
            self.facet_counts[value] += count
            self.facet_bytes[value] += byte_counts[value]
            self.update_facet(value)
        self.facet_total += sum(self.matches_other_facet(key) for key in added)
        self.facet_total_bytes += scope_bytes
        self.facet_list.item("all", values=(f"{self.facet_total:,}", size_label(self.facet_total_bytes)))

        if (not self.show_type_folders() and self.sort.get() == "Discovery order"
                and self.direction.get() == "Ascending" and self.group.get() == "None"):
            start = self.page_number * self.page_size
            for key in added:
                if self.matches_filter(key):
                    if start <= self.match_count < start + self.page_size:
                        self.insert_result(key, "")
                        self.shown_count += 1
                    self.match_count += 1
                    self.match_bytes += self.metadata[key].st_size

            self.next_page.configure(state="normal" if start + self.shown_count < self.match_count else "disabled")
            self.results_grid.layout_cards()
            self.show_preview()
        elif added:
            self.render_files()

    def finish_scan(self, job):
        labels = {"complete": ("Scan complete", "#92dfa6"), "stopped": ("Scan canceled", "#f8cb83"),
                  "failed": ("Scan failed", "#ffa8a8")}
        title, color = labels[job["status"]]
        bin_search = job.get("bin_search") if job["status"] == "complete" else None
        symbol = {"complete": "✓", "failed": "!", "stopped": "■"}[job["status"]]
        if bin_search:
            coverage, _, _ = bin_search
            title = "Emptied-bin search " + coverage
            color, symbol = "#f8cb83", "!"
        if job["status"] == "complete" and job["self_test"]:
            title = "Recovery test passed"
        elif job["status"] == "complete" and job["mode"] in ("live_ntfs", "ntfs_image") and not job.get("raw_search"):
            title = "Quick scan complete"
        elif job["status"] == "complete" and job["mode"] in ("bin", "folder") and not bin_search:
            title = "Bin scan complete"
        self.ring.itemconfigure(self.ring_text, text=symbol)
        self.ring.itemconfigure(self.ring_arc, extent=-359, outline=color)
        self.set_scan_status(title, color)
        self.stop_button.pack_forget()
        self.stop_button.configure(state="disabled")
        self.job = None
        if job["mode"] in ("bin", "folder"):
            self.media_drive_button.pack(side="right", padx=4)
        if job["mode"] == "live_ntfs":
            self.deep_drive = PureWindowsPath(job["source"]).anchor
            self.deep_button.configure(text="Search whole volume…" if job.get("raw_search") else "Continue with deep scan…")
            self.deep_already_ran = bool(job.get("raw_search"))
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
        if is_bin:
            emptied = sum(isinstance(item, Candidate) for item in self.files.values())
            summary = f"{emptied:,} emptied-bin records · {len(self.files)-emptied:,} still in bin{detail} · {elapsed}"
        if job["status"] == "stopped":
            summary += "   ·   Scanning has stopped. Partial results are available."
        elif job.get("warnings") and not job.get("error"):
            summary += "   ·   Some partitions were skipped"
            self.set_log("\n\n".join(job["warnings"]))
        elif bin_search:
            _, failures, blocked = bin_search
            summary += "   ·   " + failures[0].splitlines()[0]
            self.set_log("\n\n".join(failures))
            if blocked:
                self.show_admin_warning("Windows blocked direct drive access",
                                        f"Drive access denied for {', '.join(blocked)}. "
                                        "Right-click the extracted Digger.exe and choose Run as administrator.")
        elif job.get("error"):
            summary = job["error"].splitlines()[0]
        elif job["self_test"]:
            summary = "Recovered Documents\\Important.bin from the test image and verified all 12,032 bytes."
        self.activity.configure(text=summary)
        if job.get("folder_scope") and job["status"] == "complete":
            raw = job["sector_matches"]
            self.set_scan_status("Folder deep scan complete", color)
            coverage = "No surviving cluster map for this folder" if not job["folder_runs"] else f"{raw:,} sector matches"
            if job.get("read_errors"):
                coverage += " · Some sectors were unreadable"
            self.activity.configure(text=f"{len(self.files)-raw:,} file records · {coverage} · {elapsed}")
        elif (job.get("raw_search") or job["mode"] == "recover") and job["status"] == "complete":
            area = "Unallocated space" if job.get("only_free") else "Whole image" if job["mode"] == "recover" else "Whole volume"
            raw = job["sector_matches"]
            indexed_count = len(self.files) - raw
            coverage = f"{indexed_count:,} from file records · {raw:,} from sectors" if job.get("deep_after_quick") else f"{raw:,} from sectors"
            if job.get("quick_warning"):
                self.set_log(job["quick_warning"])
                coverage += " · Quick scan incomplete"
            if job.get("read_errors"):
                coverage += " · Some sectors were unreadable"
            if self.files and not self.match_count:
                coverage += " · Clear filters to see results"
            self.activity.configure(text=f"{coverage} · {area} · {size_label(job['volume_bytes'])} volume · {elapsed}")
            self.set_scan_status("Deep scan partial · Read errors" if job.get("read_errors") else
                                 "Deep scan complete" if raw else "Deep scan complete · No sector matches", color)
        if job.get("error"):
            messagebox.showerror("Scan", job["error"])
        if indexed and not self.files and job["status"] == "complete" and not job.get("raw_search"):
            self.set_scan_status("Quick scan complete · No deleted records", color)
            self.set_log("No matching deleted filesystem records survived in the selected source. "
                         "If you scanned a folder, try the whole drive. Use Deep Scan to search raw sectors when the original records are gone.")
        if is_bin and not self.files and job["status"] == "complete" and not bin_search:
            self.set_scan_status("Bin scan complete · No matches", color)
            self.set_log("This scan checked Recycle Bin records. Choose Deep scan a drive to look for surviving file contents.")
        log = job["folder"] / "output.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16384))
                text = stream.read().decode(errors="replace")
            clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]|[\x00-\x08\x0b-\x1f]", "", text)
            if job["mode"] == "inspect":
                self.set_log(clean.strip())
            elif job["status"] == "failed" and clean.strip():
                self.set_log(job.get("error", "") + "\n\n" + clean.strip())

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
        if self.bin_view:
            deleted = self.deleted_dates.get(key)
            return deleted.date().isoformat() if deleted else "Unknown"
        return self.modified_date(key)

    def modified_date(self, key):
        if key not in self.originals and not isinstance(self.files[key], SectorFile):
            return "Unknown"
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

    @property
    def facet_filter(self):
        return self.facet_selections[self.facet_mode.get()]

    def facet_path(self, key, mode):
        path = self.display_path(key)
        if mode == "Type":
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
            self.facet_list.insert(parent, "end", iid=row, text=value[-1], open=len(value) == 1 and self.facet_mode.get() == "Folder",
                                   values=(self.facet_counts[value],))
        kind = "Folder" if self.facet_mode.get() == "Folder" or len(value) > 1 else value[0]
        count = self.facet_checked[value]
        state = 0 if not count else 1 if count == self.facet_counts[value] else 2
        self.facet_list.item(self.facet_rows[value], image=self.row_icons[kind][state])
        self.facet_list.item(self.facet_rows[value], values=(f"{self.facet_counts[value]:,}", size_label(self.facet_bytes[value])))

    def update_checked_counts(self, keys, delta):
        mode = self.facet_mode.get()
        changes = Counter(part for key in keys if self.matches_other_facet(key) for part in self.facet_path(key, mode))
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
        return self.toggle_facet_files(value)

    def toggle_facet_files(self, value):
        adding = self.facet_checked[value] < self.facet_counts[value]
        mode = self.facet_mode.get()
        changed = [key for key in self.files if (key in self.checked) != adding
                   and self.matches_other_facet(key) and value in self.facet_path(key, mode)]
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

    def matches_other_facet(self, key):
        other = "Folder" if self.facet_mode.get() == "Type" else "Type"
        selection = self.facet_selections[other]
        return self.matches_controls(key) and (selection is None or selection in self.facet_path(key, other))

    def refresh_facets(self, render=True):
        self.facet_list.delete(*self.facet_list.get_children())
        self.facets = {"all": None}
        mode = self.facet_mode.get()
        available = [key for key in self.files if self.matches_other_facet(key)]
        self.facet_total = len(available)
        self.facet_total_bytes = sum(self.metadata[key].st_size for key in available)
        self.facet_bytes = Counter()
        for key in available:
            for facet in self.facet_path(key, mode):
                self.facet_bytes[facet] += self.metadata[key].st_size
        self.facet_list.heading("#0", text="Folder" if mode == "Folder" else "File type")
        self.facet_list.insert("", "end", iid="all", text="All folders" if mode == "Folder" else "All file types",
                               values=(f"{self.facet_total:,}", size_label(self.facet_total_bytes)))
        self.facet_counts = Counter(part for key in available for part in self.facet_path(key, mode))
        self.facet_checked = Counter(part for key in available if key in self.checked for part in self.facet_path(key, mode))
        selection = self.facet_filter or ()
        for size in range(1, len(selection)+1):
            self.facet_counts.setdefault(selection[:size], 0)
        self.facet_rows.clear()
        for value in sorted(self.facet_counts):
            self.update_facet(value)
        row = self.facet_rows.get(self.facet_filter, "all")
        self.facet_list.selection_set(row)
        self.facet_list.see(row)
        if self.facet_filter:
            self.facet_list.item(row, open=True)
        self.update_breadcrumb()
        if render:
            self.render_files()

    def populate_filters(self):
        self.filter_menu.delete(0, "end")
        if self.bin_view:
            submenu = tk.Menu(self.filter_menu, tearoff=False)
            self.filter_menu.add_cascade(label="Date deleted", menu=submenu)
            for choice in ("All dates", *sorted(self.deleted_years, reverse=True), "Unknown date"):
                submenu.add_radiobutton(label=choice, variable=self.year_filter, value=choice, command=self.render_files)
            self.filter_menu.add_separator()
        modified = tk.Menu(self.filter_menu, tearoff=False)
        self.filter_menu.add_cascade(label="Date modified", menu=modified)
        for choice in ("All dates", *sorted(self.modified_years, reverse=True), "Unknown date"):
            modified.add_radiobutton(label=choice, variable=self.modified_year_filter, value=choice, command=self.render_files)
        self.filter_menu.add_separator()
        for extension in ("All file types", "Photos and videos", *sorted(self.extensions)):
            self.filter_menu.add_radiobutton(label=extension, variable=self.format, value=extension)
        self.filter_menu.add_separator()
        self.filter_menu.add_command(label="Clear filters", command=self.clear_filters)

    def update_breadcrumb(self):
        folder = self.facet_selections["Folder"] or ()
        for widget in self.breadcrumb.winfo_children():
            widget.destroy()
        parts = [(self.scan_source, None), *((name, folder[:index+1]) for index, name in enumerate(folder))]
        if len(parts) > 3:
            ancestors = ttk.Menubutton(self.breadcrumb, text="Ancestors")
            menu = tk.Menu(ancestors, tearoff=False)
            for name, scope in parts[:-2]:
                menu.add_command(label=name, command=lambda path=scope: self.set_scope("Folder", path))
            ancestors.configure(menu=menu)
            ancestors.pack(side="left")
            parts = parts[-2:]
        for index, (name, scope) in enumerate(parts):
            if index:
                ttk.Label(self.breadcrumb, text="›", foreground=MUTED).pack(side="left", padx=2)
            label = name if len(name) <= 24 else name[:11] + "…" + name[-10:]
            if index == len(parts)-1:
                ttk.Label(self.breadcrumb, text=label, font=("Segoe UI Semibold", 11)).pack(side="left")
            else:
                ttk.Button(self.breadcrumb, text=label,
                           command=lambda path=scope: self.set_scope("Folder", path)).pack(side="left")
        self.up_button.configure(state="normal" if folder else "disabled")
        self.forward_button.configure(state="normal" if self.forward_stack else "disabled")
        for mode, button in self.scope_buttons.items():
            button.pack_forget()
            selected = self.facet_selections[mode]
            if selected:
                text = selected[-1]
                if len(text) > 18:
                    text = text[:15] + "…"
                button.configure(text=f"{mode}: {text}  ×")
                button.pack(side="left", padx=(0, 6))

    def facet_state(self):
        return self.facet_mode.get(), tuple(self.facet_selections.items())

    def set_scope(self, mode, value):
        if self.facet_selections[mode] == value:
            return
        self.back_stack.append(self.facet_state())
        self.forward_stack.clear()
        self.facet_selections[mode] = value
        self.page_number = 0
        if mode == self.facet_mode.get():
            row = self.facet_rows.get(value, "all")
            self.facet_list.selection_set(row)
            self.facet_list.see(row)
            self.update_breadcrumb()
            self.render_files()
        else:
            self.refresh_facets()
        self.results_grid.yview_moveto(0)

    def goto_facet(self, value):
        self.set_scope(self.facet_mode.get(), value)

    def clear_scope(self, mode):
        self.set_scope(mode, None)

    def restore_facet_state(self, state):
        mode, selections = state
        self.facet_mode.set(mode)
        self.facet_selections = dict(selections)
        self.page_number = 0
        self.refresh_facets()
        self.results_grid.yview_moveto(0)

    def facet_back(self, _event=None):
        if not self.grab_current() and self.tabs.select() == str(self.results_page):
            if self.back_stack:
                self.forward_stack.append(self.facet_state())
                self.restore_facet_state(self.back_stack.pop())
            else:
                self.returned_from_results = True
                self.tabs.select(self.scan_page)
        return "break"

    def facet_forward(self, _event=None):
        if not self.grab_current():
            if self.tabs.select() == str(self.results_page) and self.forward_stack:
                self.back_stack.append(self.facet_state())
                self.restore_facet_state(self.forward_stack.pop())
            elif self.tabs.select() == str(self.scan_page) and self.returned_from_results:
                self.returned_from_results = False
                self.tabs.select(self.results_page)
        return "break"

    def facet_up(self):
        folder = self.facet_selections["Folder"]
        if folder:
            self.set_scope("Folder", folder[:-1] or None)

    def select_facet(self, _event=None):
        selection = self.facet_list.selection()
        if selection:
            self.goto_facet(self.facets[selection[0]])

    def clear_filters(self):
        self.facet_selections = {"Folder": None, "Type": None}
        self.back_stack.clear()
        self.forward_stack.clear()
        self.returned_from_results = False
        self.page_number = 0
        self.search.set("")
        self.year_filter.set("All dates")
        self.modified_year_filter.set("All dates")
        self.status_filter.set(FILE_STATUSES[0])
        self.size_filter.set("All sizes")
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
        item = self.files[key]
        return self.originals.get(key, Path(item.name) if isinstance(item, SectorFile) else item)

    def group_label(self, key):
        path = self.display_path(key)
        return {"Original folder": str(path.parent) if key in self.originals else "Unknown folder",
                "Type": path.suffix.upper().lstrip(".") or "Other",
                "File date": self.file_date(key),
                "Recoverability": self.check_label(key)}.get(self.group.get())

    def file_status(self, key):
        if isinstance(self.files[key], Candidate):
            return FILE_STATUSES[1]
        if key in self.originals:
            return FILE_STATUSES[2]
        return FILE_STATUSES[3]

    def change_status_filter(self):
        self.page_number = 0
        self.render_files()
        self.results_grid.yview_moveto(0)

    def matches_controls(self, key):
        path = self.display_path(key)
        if self.active_modified != "All dates":
            stamp = self.modified_date(key)
            year = "Unknown date" if stamp == "Unknown" else stamp[:4]
            if self.active_modified != year:
                return False
        if self.bin_view:
            stamp = self.deleted_dates.get(key)
            year = str(stamp.year) if stamp else "Unknown date"
            if self.active_year not in ("All dates", year):
                return False
        return (self.metadata[key].st_size >= self.minimum_size
                and self.active_status in (FILE_STATUSES[0], self.file_status(key))
                and self.query in str(path).casefold()
                and (self.extension in ("All file types", path.suffix.upper()) or
                     self.extension == "Photos and videos" and path.suffix.lower() in IMAGE_TYPES | VIDEO_TYPES)
                )

    def matches_filter(self, key):
        return (self.matches_other_facet(key)
                and (self.facet_filter is None or self.facet_filter in self.facet_path(key, self.facet_mode.get())))

    def switch_result_view(self):
        self.render_files()
        if self.result_view.get() == "Grid":
            for widget in (self.tree, self.list_scrollbar, self.list_horizontal):
                widget.pack_forget()
            self.grid_tools.pack(side="left", padx=8)
            self.grid_scrollbar.pack(side="right", fill="y")
            self.results_grid.pack(fill="both", expand=True)
            self.results_grid.focus_set()
            self.results_grid.after_idle(self.results_grid.layout_cards)
        else:
            self.results_grid.pack_forget()
            self.grid_scrollbar.pack_forget()
            self.grid_tools.pack_forget()
            self.list_scrollbar.pack(side="right", fill="y")
            self.list_horizontal.pack(side="bottom", fill="x")
            self.tree.pack(fill="both", expand=True)
            self.tree.focus_set()

    def refresh_thumbnails(self):
        try:
            generation, results = self.thumb_events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.thumb_thread = None
            if generation == self.scan_generation:
                for key, source, picture, error in results:
                    if self.materialized.get(key, self.files.get(key)) != source:
                        continue
                    if picture is not None:
                        self.preview_errors.pop(key, None)
                        self.remember_thumbnail(key, picture)
                    elif key not in self.grid_thumbnails:
                        self.preview_errors[key] = error
                self.results_grid.invalidate()
        if self.thumb_thread:
            return
        grid = self.results_grid.winfo_ismapped()
        if grid:
            visible = [key for key, _, _ in self.results_grid.visible_cards()]
        elif self.tree.winfo_ismapped():
            visible = dict.fromkeys(self.tree.identify_row(y) for y in range(0, self.tree.winfo_height(), 12))
        else:
            return
        work = []
        for key in visible:
            if key not in self.files or key in self.grid_thumbnails or key in self.thumb_tried:
                continue
            path = self.materialized.get(key, self.files[key])
            suffix = self.materialized.get(key, self.display_path(key)).suffix.lower()
            if grid:
                if suffix not in IMAGE_TYPES | VIDEO_TYPES:
                    continue
            elif suffix not in IMAGE_TYPES or isinstance(path, Candidate):
                continue
            work.append((key, path, suffix))
            self.thumb_tried.add(key)
            if len(work) >= (2 if grid else 12):
                break
        if work:
            def read(generation):
                results = []
                for key, path, suffix in work:
                    try:
                        picture, _ = self.read_media(path, suffix, THUMBNAIL_BOUNDS, budget=12 * 1024 * 1024)
                    except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.SubprocessError,
                            Image.DecompressionBombError) as exc:
                        results.append((key, path, None, str(exc)))
                    else:
                        results.append((key, path, picture, ""))
                self.thumb_events.put((generation, results))
            self.thumb_thread = threading.Thread(target=read, args=(self.scan_generation,), daemon=True)
            self.thumb_thread.start()

    def remember_thumbnail(self, key, picture):
        if key not in self.thumbnails and len(self.thumbnails) >= self.page_size:
            oldest = next(iter(self.thumbnails))
            self.thumbnails.pop(oldest)
            self.grid_thumbnails.pop(oldest, None)
            self.thumb_tried.discard(oldest)
            if self.tree.exists(oldest):
                self.tree.item(oldest, image=self.result_icon(oldest))
        thumb = picture.copy()
        thumb.thumbnail(THUMBNAIL_BOUNDS)
        self.thumbnails[key] = row_images(thumb)
        self.grid_thumbnails[key] = thumb
        if self.tree.exists(key):
            self.tree.item(key, image=self.result_icon(key))
        self.results_grid.invalidate()

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
                                 self.file_date(key), {FILE_STATUSES[1]: "Deleted record", FILE_STATUSES[2]: "Still in bin",
                                                       FILE_STATUSES[3]: "Raw scan"}[self.file_status(key)], check), tags=(check, "alternate") if int(key) % 2 else (check,))

    def change_page(self, delta):
        self.page_number += delta
        self.tree.selection_remove(*self.tree.selection())
        self.render_files()
        self.results_grid.yview_moveto(0)

    def render_files(self):
        selected = set(self.tree.selection())
        scroll = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        self.extension_rows = {}
        self.query = self.search.get().casefold()
        self.extension = self.format.get()
        self.active_year = self.year_filter.get()
        self.active_modified = self.modified_year_filter.get()
        self.active_status = self.status_filter.get()
        self.minimum_size = SIZE_FILTERS[self.size_filter.get()]
        controls = (self.query, self.extension, self.active_modified, self.active_year, self.active_status, self.minimum_size)
        if controls != getattr(self, "_current_filters", None):
            self._current_filters = controls
            self.refresh_facets(render=False)
        self.size_button.configure(text=self.size_filter.get() if self.minimum_size else "File size")
        self.date_button.configure(text="Date modified" if self.active_modified == "All dates" else self.active_modified)
        status_names = ("File status", "Deleted", "Still in bin", "Raw scan")
        self.status_button.configure(text=status_names[FILE_STATUSES.index(self.active_status)])
        filters = [v for v in (self.extension, self.active_year)
                   if v not in ("All file types", "All dates")]
        self.filter_button.configure(text=" · ".join(filters) or "File type")
        keys = [key for key in self.files if self.matches_filter(key)]
        sort_key = {
            "Discovery order": int,
            "Name": lambda key: self.display_path(key).name.casefold(),
            "Type": lambda key: self.display_path(key).suffix.casefold(),
            "Size": lambda key: self.metadata[key].st_size,
            "File date": lambda key: self.file_date(key),
            "Recoverability": lambda key: CHECKS[self.check_label(key)][0],
        }[self.sort.get()]
        if self.sort.get() not in ("Discovery order", "Name"):
            keys.sort(key=lambda key: self.display_path(key).name.casefold())
        keys.sort(key=sort_key, reverse=self.direction.get() == "Descending")
        self.match_count = len(keys)
        self.match_bytes = sum(self.metadata[key].st_size for key in keys)
        self.page_number = max(0, min(self.page_number, (len(keys) - 1) // self.page_size))
        start = self.page_number * self.page_size
        if self.show_type_folders():
            self.render_type_folders(keys)
            return
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
        self.results_grid.layout_cards()

        self.tree.selection_set([key for key in keys if key in selected])
        if scroll:
            self.tree.yview_moveto(scroll[0])
        self.show_preview()

    def show_type_folders(self):
        scope = self.facet_selections["Type"]
        return (self.result_view.get() == "Details" and self.facet_mode.get() == "Type"
                and scope and len(scope) == 1 and self.group.get() == "None" and not self.query)

    def render_type_folders(self, keys):
        groups = {}
        for key in keys:
            suffix = self.facet_path(key, "Type")[-1]
            count, size = groups.get(suffix, (0, 0))
            groups[suffix] = count + 1, size + self.metadata[key].st_size
        ordered = sorted(groups, key=lambda scope: groups[scope][1] if self.sort.get() == "Size" else scope[-1],
                         reverse=self.direction.get() == "Descending")
        for index, scope in enumerate(ordered):
            count, size = groups[scope]
            row = f"ext-{index}"
            self.extension_rows[row] = scope
            self.tree.insert("", "end", iid=row, text=scope[-1].lower(), image=self.extension_icon(scope),
                             values=("", "", f"Folder · {count:,} {'file' if count == 1 else 'files'}", size_label(size), "", "", ""),
                             tags=("alternate",) if index % 2 == 0 else ())
        self.prev_page.configure(state="disabled")
        self.next_page.configure(state="disabled")
        self.shown_count = 0
        self.show_preview()

    def extension_icon(self, scope):
        count = self.facet_checked[scope]
        state = 0 if not count else 1 if count == self.facet_counts[scope] else 2
        return self.row_icons["Folder"][state]

    def check_focused(self, _event=None):
        key = self.tree.focus()
        if key in self.extension_rows:
            return self.toggle_facet_files(self.extension_rows[key])
        if key in self.files:
            self.set_checked(key)
        return "break"

    def toggle_checked(self, event):
        if self.tree.identify_column(event.x) != "#0" or "image" not in self.tree.identify_element(event.x, event.y):
            return
        key = self.tree.identify_row(event.y)
        if key in self.extension_rows:
            return self.toggle_facet_files(self.extension_rows[key])
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
        self.results_grid.invalidate()
        selected = self.selected_files()
        first = self.page_number * self.page_size
        self.footer.configure(text=f"Showing {first + bool(self.shown_count):,}–{first + self.shown_count:,} of {self.match_count:,}")
        if self.show_type_folders():
            for row, scope in self.extension_rows.items():
                self.tree.item(row, image=self.extension_icon(scope))
            self.footer.configure(text=f"{len(self.extension_rows):,} file types · Double-click a folder to browse")
        chosen = self.checked or selected
        required = sum(self.metadata[item].st_size for item in chosen)
        self.selection_summary.configure(text=f"{len(chosen):,} selected · {size_label(required)} needed"
                                         if chosen else "0 files selected")
        scope = self.facet_selections["Folder"] or self.facet_selections["Type"] or ("All results",)
        title = scope[-1]
        self.scope_title.configure(text=title if len(title) <= 26 else title[:23] + "…")
        self.scope_summary.configure(text=f"{self.match_count:,} files · {size_label(self.match_bytes)}")
        self.export_button.configure(state="normal" if chosen and not self.saving else "disabled")
        key = selected[0] if selected else None
        if key == self.preview_key:
            return
        self.preview_key = key
        if key is not None and not self.inspector_visible.get():
            self.inspector_visible.set(True)
            self.toggle_inspector()
        self.inspector_view.yview_moveto(0)
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
        bin_info = f"\n\nFile status\n{self.file_status(key)}"
        if self.bin_view:
            bin_info += f"\nDate deleted: {self.file_date(key)}"
        saved = self.materialized.get(key)
        if saved and saved.suffix.lower() != display.suffix.lower():
            bin_info += f"\nSaved as: {saved.name}"
        self.details.configure(text=f"Name\n{display.name}\n\nSize\n{size_label(self.metadata[key].st_size)}{bin_info}"
                                    f"\n\nType\n{display.suffix.upper().lstrip('.')} file\n\nPath\n{folder}\n\nRecovery evidence\n{self.check_label(key)}")
        saved_path = self.materialized.get(key)
        suffix = (saved_path or display).suffix.lower()
        media = suffix in IMAGE_TYPES or suffix in VIDEO_TYPES
        can_play = suffix in VIDEO_TYPES and (not isinstance(path, Candidate) or key in self.materialized)
        self.open_button.configure(text="Play" if can_play else "Open preview", state="normal" if can_play else "disabled")
        self.preview.configure(text="Loading preview…" if media else "No media preview for this file type.")
        if media:
            self.preview_pending = key, path, suffix
            self.start_preview()

    @staticmethod
    def read_media(path, suffix, bounds=(1600, 1600), budget=64 * 1024 * 1024):
        if isinstance(path, SectorFile):
            with path.open(budget) as stream:
                return decode_preview(stream, suffix, bounds)
        if isinstance(path, Candidate):
            with seekable_preview(path, budget) as stream:
                return decode_preview(stream, suffix, bounds)
        return decode_preview(path, suffix, bounds)

    def start_preview(self):
        if self.preview_active or self.preview_pending is None:
            return
        key, path, suffix = self.preview_pending
        self.preview_pending = None
        self.preview_active = True
        saved_path = self.materialized.get(key)
        generation = self.scan_generation

        def work():
            try:
                picture, check = self.read_media(saved_path or path, suffix)
                error = ""
            except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.SubprocessError,
                    Image.DecompressionBombError) as exc:
                picture, check = None, "Preview failed"
                error = str(exc)
            self.preview_events.put((generation, key, path, saved_path, picture, check, error))

        threading.Thread(target=work, daemon=True).start()

    def refresh_preview(self):
        try:
            generation, key, path, saved_path, bitmap, check, error = self.preview_events.get_nowait()
        except queue.Empty:
            return
        self.preview_active = False
        if generation == self.scan_generation and self.files.get(key) == path and self.materialized.get(key) == saved_path:
            self.checks[key] = check
            if error:
                self.preview_errors[key] = error
            else:
                self.preview_errors.pop(key, None)
            self.results_grid.invalidate()
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
                    if check == "Image decoded":
                        self.open_button.configure(text="Open preview")
                self.preview.configure(text="" if bitmap is not None else error or check)
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

    def open_result(self, _event=None, row=None):
        scope = self.extension_rows.get(row or self.tree.focus())
        if scope:
            self.set_scope("Type", scope)
            return "break"
        self.open_file()

    def open_clicked(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            return self.open_result(row=row)

    def preview_clicked(self):
        if self.preview_bitmap is None or self.preview_key is None:
            return self.open_file()
        path = self.materialized.get(self.preview_key, self.display_path(self.preview_key))
        if path.suffix.lower() in VIDEO_TYPES and self.open_button["text"] == "Play":
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
        saved = self.materialized.get(key)
        path = saved or self.files[key]
        if (saved or self.display_path(key)).suffix.lower() not in IMAGE_TYPES | VIDEO_TYPES:
            return
        try:
            if isinstance(path, SectorFile):
                if Path(path.name).suffix.lower() in VIDEO_TYPES:
                    def play():
                        try:
                            play_sector_video(path)
                        except OSError as exc:
                            self.play_errors.put(str(exc))
                    threading.Thread(target=play, daemon=True).start()
                elif self.preview_bitmap is not None and self.preview_key == key:
                    self.preview_clicked()
                return
            if isinstance(path, Candidate):
                output = self.materialized.get(key)
                if output is None:
                    if path.identity is None or path.st_size > 64 * 1024 * 1024:
                        raise OSError("Recover this file before opening it.")
                    folder = Path(tempfile.mkdtemp(dir=RUNS.name))
                    output = folder / output_name(path.original.name)
                    extract_ntfs(path, output)
                path = output
            elif key in self.originals and saved is None:
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

        keys = ([key for key, _, _ in self.results_grid.cards]
                if self.result_view.get() == "Grid" else list(file_rows()))
        if self.show_type_folders():
            keys = [key for key in self.files if self.matches_filter(key)]
            self.update_checked_counts(set(keys) - self.checked, 1)
            self.checked.update(keys)
            self.show_preview()
            return "break"
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
                report.writerow(("Path", "Size (bytes)", "Date modified", "Date deleted", "Evidence"))
                for key in self.files:
                    path = str(self.display_path(key))
                    # Keep recovered filenames from being interpreted as spreadsheet formulas.
                    if path.lstrip().startswith(("=", "+", "-", "@")):
                        path = "'" + path
                    report.writerow((path, self.metadata[key].st_size, self.modified_date(key), self.file_date(key) if self.bin_view else "", self.check_label(key)))
        except OSError as exc:
            messagebox.showerror("Save scan report", str(exc))

    def modal(self, title):
        dialog = tk.Toplevel(self)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(self)
        dialog.configure(bg="#353940")
        dialog.resizable(False, False)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        return dialog

    def wait_for_modal(self, dialog, width, height, focus):
        self.update_idletasks()
        left, top, right, bottom = self.monitor_work_area()
        width = min(width, right - left - 24)
        height = min(height, bottom - top - 56)
        owner_x, owner_y = self.winfo_rootx(), self.winfo_rooty()
        center_x = owner_x + self.winfo_width() // 2
        center_y = owner_y + self.winfo_height() // 2
        x = max(left + 12, min(center_x - width // 2, right - width - 12))
        y = max(top + 8, min(center_y - height // 2, bottom - height - 48))
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.deiconify()
        dialog.wait_visibility()
        dialog.lift()
        dialog.grab_set()
        focus.focus_force()
        self.wait_window(dialog)

    def monitor_work_area(self):
        if os.name == "nt":
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

            user = ctypes.windll.user32
            user.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MonitorInfo))
            monitor = user.MonitorFromWindow(self.winfo_id(), 2)
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if monitor and user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                area = info.work
                return area.left, area.top, area.right, area.bottom
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def partition_notice(self, groups):
        active = self.grab_current()
        if active is not None:
            def show_after_close(event):
                if event.widget == active:
                    self.after_idle(self.partition_notice, groups)
            active.bind("<Destroy>", show_after_close, add="+")
            return
        dialog = self.modal("Partitions found")
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
        close = ttk.Button(dialog, text="Got it", style="Digger.TButton", command=dialog.destroy)
        close.pack(pady=(0, 16))
        self.wait_for_modal(dialog, 452, 300, close)

    def remember_destination(self, folder):
        self.last_destination = str(folder)
        try:
            PREFERENCES.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=PREFERENCES.parent,
                                            delete=False) as pending:
                temporary = Path(pending.name)
                json.dump({"recovery_destination": self.last_destination}, pending)
            try:
                os.replace(temporary, PREFERENCES)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            # Keep the choice for this session even if settings cannot be written.
            pass

    def choose_recovery_folder(self, subtitle):
        dialog = self.modal("Recovery destination")
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
        tk.Label(panel, text="Available drives", bg="#424750", fg=TEXT, pady=5).pack(fill="x", pady=(0, 6))
        try:
            previous = self.last_destination if self.last_destination and Path(self.last_destination).is_dir() else ""
        except OSError:
            previous = ""
        choice = tk.StringVar(dialog, value=previous)
        drive_list = ttk.Frame(panel)
        drive_list.pack(fill="both", expand=True)
        drives = ttk.Treeview(drive_list, columns=("space",), show="tree", height=3, selectmode="browse")
        drives.column("#0", width=270, minwidth=105)
        drives.column("space", width=140, minwidth=100, anchor="e")
        drives.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(drive_list, orient="vertical", command=drives.yview)
        scrollbar.pack(side="right", fill="y")
        drives.configure(yscrollcommand=scrollbar.set)
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
        tk.Label(panel, text="Destination folder", bg="#282c32", fg=TEXT, anchor="w").pack(fill="x", pady=(8, 0))
        field = tk.Frame(panel, bg="#282c32")
        field.pack(fill="x", pady=(6, 0))
        def browse():
            initial = Path(choice.get())
            folder = filedialog.askdirectory(parent=dialog, title="Choose recovery folder",
                                             initialdir=str(initial) if initial.is_absolute() and initial.is_dir() else None)
            dialog.lift()
            if folder:
                drives.selection_remove(*drives.selection())
                choice.set(folder)
                save.focus_set()
            else:
                browse_button.focus_set()
        browse_button = ttk.Button(field, text="Choose folder…", command=browse)
        browse_button.pack(side="left", padx=(0, 6))
        entry = ttk.Entry(field, textvariable=choice)
        entry.pack(side="left", fill="x", expand=True)
        result = []
        def accept():
            path = Path(choice.get())
            if not path.is_absolute() or not path.is_dir():
                messagebox.showerror("Recovery destination", "Choose an existing folder.", parent=dialog)
                return
            result.append(choice.get())
            dialog.destroy()
        ttk.Button(footer, text="Cancel", command=dialog.destroy).pack(side="left")
        save = ttk.Button(footer, text="Save", width=12, style="Digger.TButton", command=accept,
                          state="normal" if previous else "disabled")
        save.pack(side="right")
        choice.trace_add("write", lambda *_: save.configure(state="normal" if choice.get().strip() else "disabled"))
        dialog.bind("<Return>", lambda _: save.invoke())
        focus = save if previous else drives if drives.get_children() else browse_button
        self.wait_for_modal(dialog, 562, 576, focus)
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
            self.remember_destination(folder)
        except OSError as exc:
            messagebox.showerror("Recover selected files", str(exc), parent=self)
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
            output = None
            try:
                if original is not None:
                    if not isinstance(source, Candidate) and not ordinary(source):
                        raise OSError("The item is no longer available.")
                    parts = [output_name(part) for part in original.parts[1:]]
                    output = (destination.joinpath(original.drive.rstrip(":"), *parts) if original.drive
                              else destination.joinpath(output_name(original.parts[0]), *parts))
                else:
                    output = (destination / "Reconstructed" / output_name(source.name) if isinstance(source, SectorFile)
                              else destination / source.parent.name / source.name)
                output.parent.mkdir(parents=True, exist_ok=True)
                output = unique_path(output)
                if isinstance(source, SectorFile):
                    evidence[key] = source.save(output)
                elif isinstance(source, Candidate):
                    evidence[key] = extract_ntfs(source, output)
                else:
                    with atomic_output(output, metadata_from=source) as staging:
                        shutil.copyfile(source, staging)
                    evidence[key] = "File copied"
                materialized[key] = correct_extension(output)
                saved += 1
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                evidence[key] = "Recovery failed"
                origin = f"Volume/image {source.source}, record {source.inode}" if isinstance(source, Candidate) else str(source)
                failures.append(f"{original or source}\nSource: {origin}\n{exc}")
                if output is not None:
                    for parent in output.parents:
                        if parent == destination:
                            break
                        try:
                            parent.rmdir()
                        except OSError:
                            break
        report_path = None
        report = f"Recovered {saved} of {len(entries)} selected files."
        if saved:
            report += f"\nRecovered files: {destination}"
        else:
            report += "\nNo files were written."
            try:
                destination.rmdir()
            except OSError as exc:
                report += f"\nCould not remove the unused output folder: {exc}"
        if failures:
            report += "\n\n" + "\n\n".join(failures)
            report_path = destination.with_name(destination.name + "-report.txt")
            try:
                with atomic_output(report_path) as staging:
                    staging.write_text(report + "\n", encoding="utf-8")
            except OSError as exc:
                report_path = None
                report += f"\n\nCould not save the report: {exc}"
        self.save_events.put((destination, len(entries), saved, report_path, report, materialized, evidence))

    def refresh_save(self):
        try:
            destination, count, saved, report_path, report, materialized, evidence = self.save_events.get_nowait()
        except queue.Empty:
            return
        self.materialized.update(materialized)
        self.thumb_tried.difference_update(materialized)
        for key in materialized:
            self.preview_errors.pop(key, None)
        self.recovery_checks.update(evidence)
        self.saving = False
        self.save_status.configure(text=f"Recovered {saved} of {count} selected files")
        self.demo.configure(state="disabled" if self.saving or self.job is not None else "normal")
        self.update_source()
        self.update_motion()
        self.preview_key = None
        self.render_files()
        if saved:
            self.output_folder = destination
            self.open_output.configure(state="normal")
            self.open_output.pack(side="right")
        if saved != count:
            self.last_recovery_report = report, report_path
            self.recovery_details_button.pack(side="left", padx=(12, 0))
            self.after_idle(self.show_recovery_report)
        else:
            messagebox.showinfo("Recovery complete", f"{report}\n\n{destination}", parent=self)

    def show_recovery_report(self):
        if self.last_recovery_report is None or self.grab_current() is not None:
            return
        report, path = self.last_recovery_report
        dialog = self.modal("Recovery details")
        toolbar = ttk.Frame(dialog, padding=12)
        toolbar.pack(side="bottom", fill="x")
        close = ttk.Button(toolbar, text="Done", style="Digger.TButton", command=dialog.destroy)
        close.pack(side="right")
        def copy():
            self.clipboard_clear()
            self.clipboard_append(report)
        ttk.Button(toolbar, text="Copy report", command=copy).pack(side="left")
        if path:
            ttk.Button(toolbar, text="Open saved report", command=lambda: os.startfile(path)).pack(side="left", padx=8)
            ttk.Label(dialog, text=str(path), wraplength=650, padding=12).pack(fill="x")
        panel = ttk.Frame(dialog, padding=(12, 8))
        panel.pack(fill="both", expand=True)
        details = tk.Text(panel, wrap="word", bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=12)
        scrollbar = ttk.Scrollbar(panel, command=details.yview)
        scrollbar.pack(side="right", fill="y")
        details.configure(yscrollcommand=scrollbar.set)
        details.pack(fill="both", expand=True)
        details.insert("1.0", report)
        details.configure(state="disabled")
        self.wait_for_modal(dialog, 700, 460, close)

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
