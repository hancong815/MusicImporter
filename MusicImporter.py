#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Music Importer v17 (macOS)
Batch music conversion, metadata/artwork staging, editable split preview, single-pass batch splitting, metadata/artwork staging, direct Apple Music import, and cancellable task control.
Original source files are never modified.
Dependencies: FFmpeg, Python 3, Mutagen (pip install mutagen)
"""

import os
import re
import subprocess
import threading
import tkinter as tk
import unicodedata
from tkinter import ttk, filedialog, messagebox
from pathlib import Path as P

SUPPORTED = {
    ".flac", ".wav", ".ape", ".aiff", ".aif", ".dsf", ".dff", ".wv",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus"
}

TAG_FIELDS = [
    ("title", "歌曲名称 / Title"),
    ("artist", "歌手 / Artist"),
    ("album", "专辑 / Album"),
    ("albumartist", "专辑艺术家 / Album Artist"),
    ("date", "年份 / Date"),
    ("genre", "流派 / Genre"),
    ("tracknumber", "曲序 / Track Number"),
    ("discnumber", "碟号 / Disc Number"),
]

# Conservative title-cleanup patterns: remove only common, clearly promotional/file-format labels.
REMOVE_PATTERNS = [
    r"\[(?:FLAC|WAV|APE|MP3|AAC|ALAC|DSF|DFF|无损(?:音乐|音质)?|高音质|免费下载|音乐下载)\]",
    r"\((?:FLAC|WAV|APE|MP3|AAC|ALAC|DSF|DFF|无损(?:音乐|音质)?|高音质|免费下载|音乐下载)\)",
    r"\[(?:\d{2,3}\s*kbps|\d{1,2}\s*bit(?:\s*/\s*\d{2,3}\s*kHz)?)\]",
    r"\((?:\d{2,3}\s*kbps|\d{1,2}\s*bit(?:\s*/\s*\d{2,3}\s*kHz)?)\)",
    r"\[(?:Official\s*(?:Audio|MV|Video)|Lyrics?\s*Video)\]",
    r"\((?:Official\s*(?:Audio|MV|Video)|Lyrics?\s*Video)\)",
]
TRACK_PREFIX = re.compile(r"^\s*(?:(?:track\s*)?(\d{1,3})\s*[-._、)]\s*|(\d{1,3})\s+|(\d{1,3})(?=[\u4e00-\u9fffA-Za-z]))", re.I)


def find_ffmpeg():
    candidates = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]
    for candidate in candidates:
        if candidate == "ffmpeg":
            try:
                subprocess.run([candidate, "-version"], capture_output=True, check=True)
                return candidate
            except Exception:
                pass
        elif os.path.isfile(candidate):
            return candidate
    return None


def read_tags(path):
    """Read supported tags through Mutagen; return normalized field dictionary."""
    empty = {key: "" for key, _ in TAG_FIELDS}
    try:
        from mutagen import File
        audio = File(path, easy=True)
        if not audio or not audio.tags:
            empty["title"] = P(path).stem
            return empty

        def val(*keys):
            for key in keys:
                item = audio.tags.get(key)
                if item:
                    if isinstance(item, (list, tuple)):
                        return str(item[0]) if item else ""
                    return str(item)
            return ""

        empty.update({
            "title": val("title") or P(path).stem,
            "artist": val("artist"),
            "album": val("album"),
            "albumartist": val("albumartist", "album artist"),
            "date": val("date", "year"),
            "genre": val("genre"),
            "tracknumber": val("tracknumber", "track"),
            "discnumber": val("discnumber", "disc"),
        })
    except Exception:
        empty["title"] = P(path).stem
    return empty


def normalize_track_number(raw):
    """Normalize common '03/12' forms to '3'; retain non-numeric content if unknown."""
    raw = (raw or "").strip()
    match = re.match(r"^(\d+)", raw)
    return str(int(match.group(1))) if match else raw


def parse_time_value(raw):
    """Parse split-preview time as seconds, MM:SS, or HH:MM:SS."""
    text = (raw or "").strip()
    if ":" not in text:
        return float(text)
    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError("时间格式请使用秒数、MM:SS 或 HH:MM:SS")


def clean_title(title, artist="", filename=""):
    """Clean a title. Prefer a clear track-number/artist/title structure in the filename."""
    text = (title or "").strip()
    track_num = ""

    # Normalize full-width characters and common dash variants before parsing.
    dash_chars = "-－–—−﹣ｰ゠"
    separator_chars = re.escape(dash_chars + ":：")
    if filename:
        stem = unicodedata.normalize("NFKC", P(filename).stem).strip()
        # Match: track number + any artist text + separator + title.
        # The separator must occur after the artist text; the title must not be empty.
        filename_match = re.match(
            rf"^([0-9]{{1,3}})\s*(.*?)\s*[{separator_chars}]\s*(.+?)\s*$", stem
        )
        if filename_match:
            candidate_track, candidate_artist, candidate_title = filename_match.groups()
            if candidate_artist.strip() and candidate_title.strip():
                track_num = candidate_track
                text = candidate_title.strip()

    # Fallback: extract track prefix from tag title if filename parsing did not succeed.
    if not track_num:
        normalized_title = unicodedata.normalize("NFKC", text)
        prefix_match = TRACK_PREFIX.match(normalized_title)
        if prefix_match:
            track_num = next((g for g in prefix_match.groups() if g), "")
            text = normalized_title[prefix_match.end():]

    # Remove artist prefix only when tag title itself clearly contains it.
    artist = unicodedata.normalize("NFKC", (artist or "").strip())
    text = unicodedata.normalize("NFKC", text)
    if artist:
        text = re.sub(r"^\s*" + re.escape(artist) + r"\s*[-－–—−﹣ｰ゠:：]\s*", "", text, flags=re.I)

    for pattern in REMOVE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"\.(?:flac|wav|ape|mp3|aac|alac|dsf|dff|aiff|aif|wv|ogg|opus)$", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:[-_.、]\s*)+", "", text)
    text = re.sub(r"\s*(?:[-_.])\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("[](){} _-.")
    return text, track_num


def safe_component(text, fallback):
    text = (text or fallback).strip()
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    return text[:160] or fallback



def _decode_cue_text(path):
    """Decode CUE text using strict, common encodings so Chinese tags are not silently corrupted."""
    raw = P(path).read_bytes()
    # BOMs first; UTF-8 is common, then Windows Chinese encodings used by older music releases.
    candidates = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp932")
    for encoding in candidates:
        try:
            text = raw.decode(encoding, errors="strict")
            # Reject obviously mojibake-heavy text only as a last resort heuristic.
            return text, encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1 (fallback)"


def parse_cue_file(cue_path):
    """Parse CUE FILE/TRACK/TITLE/PERFORMER/INDEX 01 with multi-file support."""
    text, encoding = _decode_cue_text(cue_path)
    tracks = []
    current = None
    album_title = ""
    album_artist = ""
    file_name = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r'^FILE\s+(?:"([^"]+)"|(.+?))\s+([A-Za-z0-9]+)\s*$', line, re.I)
        if m:
            file_name = (m.group(1) or m.group(2)).strip()
            continue
        m = re.match(r'^(TITLE|PERFORMER)\s+(?:"(.*)"|(.*))$', line, re.I)
        if m and current is None:
            val = (m.group(2) if m.group(2) is not None else m.group(3)).strip()
            if m.group(1).upper() == "TITLE": album_title = val
            else: album_artist = val
            continue
        m = re.match(r'^TRACK\s+(\d+)\s+([A-Za-z0-9]+)', line, re.I)
        if m:
            current = {"track": int(m.group(1)), "title": f"Track {int(m.group(1)):02d}",
                       "artist": album_artist, "start": None, "file": file_name}
            tracks.append(current)
            continue
        if current is not None:
            m = re.match(r'^(TITLE|PERFORMER)\s+(?:"(.*)"|(.*))$', line, re.I)
            if m:
                val = (m.group(2) if m.group(2) is not None else m.group(3)).strip()
                current["title" if m.group(1).upper() == "TITLE" else "artist"] = val
                continue
            m = re.match(r'^INDEX\s+01\s+(\d+):(\d+):(\d+)', line, re.I)
            if m:
                mm, ss, ff = map(int, m.groups())
                current["start"] = mm * 60 + ss + ff / 75.0
    if not tracks or any(t["start"] is None for t in tracks):
        raise ValueError("CUE 文件中未找到有效 TRACK / INDEX 01 信息。")
    # Group source file references. Most album CUE sheets reference one file, but split-file CUEs exist.
    cue_files = list(dict.fromkeys(t.get("file") for t in tracks if t.get("file")))
    return {"album": album_title, "artist": album_artist, "file": file_name,
            "files": cue_files, "encoding": encoding, "tracks": tracks}


class MusicImporter:
    def __init__(self, root):
        self.root = root
        root.title("Music Importer")
        root.geometry("1180x720")
        root.minsize(900, 580)

        self.ffmpeg = find_ffmpeg()
        self.output_dir = str(P.home() / "Music" / "MusicImporter")
        self.output_dir_customized = False
        self.output_format = tk.StringVar(value="ALAC")
        self.auto_import = tk.BooleanVar(value=False)
        self.items = []
        self.cue_files = {}  # Associate selected/imported CUE files with matching audio files.
        self.split_plans = {}  # Per-source edited split plans, prepared before splitting.
        self.busy = False
        self.cancel_requested = threading.Event()
        self.current_process = None
        self.closing_requested = False
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_status("就绪" if self.ffmpeg else "未检测到 FFmpeg：请安装后重新启动")

    def build_ui(self):
        # This version uses one consistent light theme; theme switching is removed.
        self.ui = self._palette("Light")
        c = self.ui
        self.root.title("Music Importer")
        self.root.geometry("1280x820")
        self.root.minsize(1020, 680)
        self.root.configure(bg=c["bg"])

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Pick installed fonts so the design looks polished without requiring font installs.
        available_fonts = set(self.root.tk.call("font", "families"))
        def font_family(candidates, fallback="Helvetica"):
            return next((name for name in candidates if name in available_fonts), fallback)
        font_ui = font_family(["SF Pro Text", "SF Pro", "Avenir Next", "Inter", "Helvetica Neue"])
        font_display = font_family(["SF Pro Display", "Avenir Next", "Inter", "Helvetica Neue"], font_ui)
        self.font_ui, self.font_display = font_ui, font_display
        style.configure("TFrame", background=c["bg"])
        style.configure("Panel.TFrame", background=c["panel"])
        style.configure("Panel2.TFrame", background=c["panel2"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"], font=(font_ui, 10))
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        style.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"])
        style.configure("Hero.TLabel", background=c["bg"], foreground=c["text"], font=(font_display, 24, "bold"))
        style.configure("HeroSub.TLabel", background=c["bg"], foreground=c["muted"], font=(font_ui, 10))
        style.configure("Accent.TLabel", background=c["bg"], foreground=c["text"], font=(font_ui, 10, "bold"))
        style.configure("Section.TLabel", background=c["panel"], foreground=c["text"], font=(font_ui, 10, "bold"))
        style.configure("TButton", background=c["panel2"], foreground=c["text"], bordercolor=c["line"],
                       padding=(12, 9), font=(font_ui, 10), relief="flat")
        style.map("TButton", background=[("active", c["line"]), ("disabled", c["panel"])],
                  foreground=[("disabled", c["muted"])], bordercolor=[("active", c["cyan"])])
        style.configure("Accent.TButton", background=c["panel2"], foreground=c["text"], bordercolor=c["cyan"],
                       padding=(15, 11), font=(font_ui, 10, "bold"), relief="flat")
        style.map("Accent.TButton", background=[("active", c["panel2"])], bordercolor=[("active", c["cyan"])])
        style.configure("Danger.TButton", background=c["panel2"], foreground=c["red"], bordercolor=c["red"],
                       padding=(12, 9), font=(font_ui, 10, "bold"), relief="flat")
        style.configure("Split.TButton", background=c["panel2"], foreground=c["text"], bordercolor=c["cyan"],
                       padding=(13, 10), font=(font_ui, 10, "bold"), relief="flat")
        style.map("Split.TButton", background=[("active", c["panel2"])], bordercolor=[("active", c["cyan"])])
        style.map("Danger.TButton", background=[("active", c["panel2"])], bordercolor=[("active", c["red"])])
        style.configure("TCheckbutton", background=c["panel"], foreground=c["text"], font=(font_ui, 10))
        style.map("TCheckbutton", background=[("active", c["panel"])], foreground=[("active", c["text"])])
        style.configure("TCombobox", fieldbackground=c["input"], background=c["panel2"], foreground=c["text"],
                       bordercolor=c["line"], arrowcolor=c["cyan"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", c["input"])], foreground=[("readonly", c["text"])])
        style.configure("Treeview", background=c["input"], fieldbackground=c["input"], foreground=c["text"],
                       rowheight=36, borderwidth=0, font=(font_ui, 10))
        style.configure("Treeview.Heading", background=c["panel2"], foreground=c["text"],
                       relief="flat", font=(font_ui, 9, "bold"), padding=(8, 10))
        style.map("Treeview", background=[("selected", "#173C50")], foreground=[("selected", "#FFFFFF")])
        style.map("Treeview.Heading", background=[("active", c["panel2"])])
        style.configure("Horizontal.TProgressbar", troughcolor=c["panel2"], background=c["cyan"],
                       bordercolor=c["panel2"], lightcolor=c["cyan"], darkcolor=c["cyan"], thickness=12)

        # Header: keep the product name plain, remove invented slogans/statuses.
        header = tk.Frame(self.root, bg=c["bg"])
        header.pack(fill="x", padx=22, pady=(16, 12))
        tk.Label(header, text="Music Importer", fg=c["text"], bg=c["bg"],
                 font=(font_display, 20, "bold")).pack(side="left")

        # Main work area: action rail and file table.
        workarea = tk.Frame(self.root, bg=c["bg"])
        workarea.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        rail = tk.Frame(workarea, bg=c["panel"], highlightbackground=c["line"], highlightthickness=1, width=208)
        rail.pack(side="left", fill="y", padx=(0, 12)); rail.pack_propagate(False)
        # Keep the left rail focused: file intake, removal, and a compact tools menu.
        tk.Label(rail, text="操作菜单", fg=c["text"], bg=c["panel"], font=(font_ui, 11, "bold")).pack(anchor="w", padx=14, pady=(16, 10))
        self.add_menu_button = ttk.Button(rail, text="＋  添加音频  ▾", command=lambda: self._show_action_menu(self.add_menu_button, [
            ("添加音乐文件…", self.add_files), ("添加整个文件夹…", self.add_folder)]), style="Accent.TButton")
        self.add_menu_button.pack(fill="x", padx=10, pady=4)
        ttk.Button(rail, text="⌫  移除选中项目", command=self.remove_selected).pack(fill="x", padx=10, pady=4)
        self.tools_menu_button = ttk.Button(rail, text="标签与封面工具  ▾", command=lambda: self._show_action_menu(self.tools_menu_button, [
            ("歌名智能清理…", self.preview_cleanup), ("批量编辑信息…", self.batch_edit),
            ("编辑单曲标签…", self.edit_one), ("设置专辑封面…", self.set_artwork)]), style="TButton")
        self.tools_menu_button.pack(fill="x", padx=10, pady=4)
        ttk.Button(rail, text="拆分预览 / 编辑曲目", command=self.preview_split_tracks, style="Accent.TButton").pack(fill="x", padx=10, pady=4)

        table_panel = tk.Frame(workarea, bg=c["panel"], highlightbackground=c["line"], highlightthickness=1)
        table_panel.pack(side="left", fill="both", expand=True)
        table_head = tk.Frame(table_panel, bg=c["panel"]); table_head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(table_head, text="TRACK LIBRARY", fg=c["text"], bg=c["panel"], font=(font_display, 14, "bold")).pack(side="left")
        tk.Label(table_head, text="文件列表与元数据", fg=c["muted"], bg=c["panel"], font=(font_ui, 9)).pack(side="left", padx=(8, 0))
        cols = ("filename", "title", "artist", "album", "albumartist", "date", "track", "format", "cover", "status")
        self.tree = ttk.Treeview(table_panel, columns=cols, show="headings", selectmode="extended")
        heading = {"filename": "SOURCE FILE", "title": "TRACK TITLE", "artist": "ARTIST", "album": "ALBUM", "albumartist": "ALBUM ARTIST",
                   "date": "YEAR", "track": "NO.", "format": "AUDIO / QUALITY", "cover": "ARTWORK", "status": "STATUS"}
        widths = {"filename": 160, "title": 190, "artist": 125, "album": 145, "albumartist": 125,
                  "date": 62, "track": 50, "format": 125, "cover": 110, "status": 95}
        for col in cols:
            self.tree.heading(col, text=heading[col])
            self.tree.column(col, width=widths[col], minwidth=45, stretch=(col in {"title", "album"}))
        tree_y = ttk.Scrollbar(table_panel, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(table_panel, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        tree_y.pack(side="right", fill="y", padx=(0, 8), pady=(0, 10))
        tree_x.pack(side="bottom", fill="x", padx=(10, 0), pady=(0, 8))
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        self.tree.tag_configure("odd", background=c["panel2"])
        self.tree.tag_configure("even", background=c["panel"])

        # Output path and format are secondary settings; keep them inside the left rail menu.
        self.output_path_display = tk.StringVar(value="")
        self.output_settings_button = ttk.Button(rail, text="输出与格式设置  ▾", command=self.show_output_settings_menu, style="TButton")
        self.output_settings_button.pack(fill="x", padx=10, pady=4)
        self.output_path_label = tk.Label(rail, textvariable=self.output_path_display, fg=c["muted"], bg=c["panel"],
                                         font=(font_ui, 8), justify="left", anchor="w", wraplength=175)
        # Hide default path; show only after the user explicitly changes it.

        controls = tk.Frame(self.root, bg=c["panel"], highlightbackground=c["line"], highlightthickness=1)
        controls.pack(fill="x", padx=22, pady=(0, 10))
        controls_inner = tk.Frame(controls, bg=c["panel"]); controls_inner.pack(fill="x", padx=12, pady=10)
        self.action_buttons = []
        # Persistent settings summary: users can see important defaults and changes at a glance.
        self.settings_summary = tk.Label(
            controls_inner, text="", anchor="w", bg=c["panel"], fg=c["muted"],
            font=(font_ui, 9), padx=2
        )
        self.settings_summary.pack(fill="x", pady=(0, 8))
        action_row = tk.Frame(controls_inner, bg=c["panel"])
        action_row.pack(fill="x")
        self.split_button = ttk.Button(action_row, text="拆分歌曲", command=lambda: self.start_work("split"), style="Accent.TButton")
        self.split_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.split_convert_button = ttk.Button(action_row, text="拆分并转换", command=lambda: self.start_work("split_convert"), style="Accent.TButton")
        self.split_convert_button.pack(side="left", fill="x", expand=True, padx=6)
        self.convert_button = ttk.Button(action_row, text="格式转换", command=lambda: self.start_work("convert"), style="Accent.TButton")
        self.convert_button.pack(side="left", fill="x", expand=True, padx=6)
        self.action_buttons.extend([self.split_button, self.split_convert_button, self.convert_button])
        self.stop_button = ttk.Button(action_row, text="停止任务", command=self.stop_work, state="disabled", style="Danger.TButton")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(6, 0))
        # Apple Music auto-add is a secondary preference, housed in the output settings menu.
        self._update_settings_summary()

        status_panel = tk.Frame(self.root, bg=c["bg"])
        status_panel.pack(fill="x", padx=22, pady=(0, 12))
        status_line = tk.Frame(status_panel, bg=c["bg"])
        status_line.pack(fill="x", pady=(0, 5))
        self.status = tk.Label(status_line, text="", anchor="w", bg=c["bg"], fg=c["muted"], font=(font_ui, 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.progress_percent = tk.Label(status_line, text="0%", bg=c["bg"], fg=c["text"], font=(font_ui, 9, "bold"))
        self.progress_percent.pack(side="right")
        progress_track = tk.Frame(status_panel, bg="#18263B", height=12, highlightbackground="#2C425D", highlightthickness=1)
        progress_track.pack(fill="x")
        progress_track.pack_propagate(False)
        self.progress_fill = tk.Frame(progress_track, bg=c["cyan"], height=10)
        self.progress_fill.place(x=0, y=0, relheight=1, width=0)
        self.progress = self._ProgressAdapter(self, progress_track, self.progress_fill)

    def _palette(self, mode):
        if mode == "Dark":
            return {"bg":"#17191D", "panel":"#22252B", "panel2":"#2C3037", "line":"#3A3F47",
                    "text":"#F0F2F4", "muted":"#A8AFB8", "cyan":"#27C7D9", "magenta":"#E55AB7",
                    "green":"#43C88A", "yellow":"#E7B85C", "red":"#E56C76", "input":"#191C21"}
        # Light theme: warm neutral surfaces with a teal accent, not sterile white.
        return {"bg":"#F4F6F8", "panel":"#FFFFFF", "panel2":"#E9EEF2", "line":"#D5DDE4",
                "text":"#202831", "muted":"#687581", "cyan":"#087F91", "magenta":"#087F91",
                "green":"#087F91", "yellow":"#687581", "red":"#C34450", "input":"#FFFFFF"}

    def _system_theme_mode(self):
        if os.name == "posix" and P("/System/Library").exists():
            try:
                result = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                       capture_output=True, text=True, timeout=2)
                return "Dark" if "Dark" in result.stdout else "Light"
            except Exception:
                return "Light"
        return "Light"

    def _prepare_popup(self, window, title, geometry, parent=None):
        """Apply the same light/dark surface, typography, spacing and modal behavior to every custom popup."""
        c = self.ui
        window.title(title)
        window.geometry(geometry)
        window.configure(bg=c["bg"])
        window.transient(parent or self.root)
        try:
            window.resizable(True, True)
        except tk.TclError:
            pass
        # Use shared popup styles; avoid standalone default Tk grey surfaces.
        style = ttk.Style(window)
        style.configure("Popup.TFrame", background=c["bg"])
        style.configure("PopupPanel.TFrame", background=c["panel"])
        style.configure("Popup.TLabel", background=c["bg"], foreground=c["text"], font=(self.font_ui, 10))
        style.configure("PopupMuted.TLabel", background=c["bg"], foreground=c["muted"], font=(self.font_ui, 9))
        style.configure("Popup.TButton", padding=(12, 7), font=(self.font_ui, 10))
        style.configure("PopupAccent.TButton", padding=(14, 8), font=(self.font_ui, 10, "bold"),
                       background=c["panel2"], foreground=c["text"], bordercolor=c["cyan"])
        return c

    def _popup_body(self, window, padding=14):
        frame = ttk.Frame(window, style="Popup.TFrame", padding=padding)
        frame.pack(fill="both", expand=True)
        return frame

    def show_output_settings_menu(self):
        """Compact output settings menu including Apple Music auto-add toggle."""
        button = self.output_settings_button
        c = self.ui
        menu = tk.Menu(button, tearoff=0, bg=c["panel"], fg=c["text"],
                      activebackground=c["panel2"], activeforeground=c["text"],
                      font=(self.font_ui, 10), relief="flat", borderwidth=1)
        menu.add_command(label="更改输出目录…", command=self.choose_output)
        menu.add_command(label="打开输出目录", command=self.open_output)
        menu.add_command(label="设置输出格式…", command=self.choose_format)
        menu.add_separator()
        menu.add_checkbutton(label="完成后自动加入 Apple Music", variable=self.auto_import,
                            command=self.toggle_auto_import, foreground=c["text"],
                            activeforeground=c["text"], background=c["panel"],
                            activebackground=c["panel2"])
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _show_action_menu(self, button, actions):
        """Show a compact popup menu for secondary actions, using the active theme."""
        c = self.ui
        menu = tk.Menu(button, tearoff=0, bg=c["panel"], fg=c["text"],
                      activebackground=c["panel2"], activeforeground=c["text"],
                      font=(self.font_ui, 10), relief="flat", borderwidth=1)
        for label, command in actions:
            menu.add_command(label=label, command=command)
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    class _ProgressAdapter:
        """Drop-in configure adapter for the custom glowing progress track."""
        def __init__(self, owner, track, fill):
            self.owner, self.track, self.fill = owner, track, fill
            self.maximum, self.value = 100, 0
        def configure(self, **kwargs):
            self.maximum = max(1, float(kwargs.get("maximum", self.maximum)))
            self.value = max(0, min(float(kwargs.get("value", self.value)), self.maximum))
            ratio = self.value / self.maximum
            width = max(0, int(self.track.winfo_width() * ratio))
            self.fill.place(x=0, y=0, relheight=1, width=width)
            self.fill.configure(bg=self.owner.ui["cyan"])
            self.owner.progress_percent.configure(text=f"{int(ratio * 100)}%")
            self.track.update_idletasks()
            self.track.bind("<Configure>", lambda _e: self.configure())

    @staticmethod
    def _audio_quality_badge(path):
        ext = P(path).suffix.lower().lstrip(".")
        lossless = {"flac", "wav", "ape", "aiff", "aif", "dsf", "dff", "wv", "m4a"}
        lossy = {"mp3", "aac", "ogg", "opus"}
        if ext in {"dsf", "dff"}:
            return f"{ext.upper()}  ·  DSD"
        if ext in lossless:
            return f"{ext.upper()}  ·  LOSSLESS"
        if ext in lossy:
            return f"{ext.upper()}  ·  LOSSY"
        return ext.upper() or "AUDIO"

    def _format_loss_label(self, fmt=None):
        fmt = (fmt or self.output_format.get()).upper()
        return "无损" if fmt in {"ALAC", "FLAC", "WAV", "AIFF"} else "有损"

    def _update_settings_summary(self):
        """Refresh a concise, always-visible summary of current/default settings."""
        if not hasattr(self, "settings_summary"):
            return
        output = self.output_dir if getattr(self, "output_dir_customized", False) else "默认：~/Music/MusicImporter"
        import_state = "开启" if self.auto_import.get() else "关闭"
        self.settings_summary.configure(
            text=f"当前设置  ·  输出格式：{self.output_format.get()}（{self._format_loss_label()}）  ·  输出位置：{output}  ·  Apple Music 自动加入：{import_state}"
        )

    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频与 CUE", "*.flac *.wav *.ape *.aiff *.aif *.dsf *.dff *.wv *.mp3 *.m4a *.aac *.ogg *.opus *.cue"), ("所有文件", "*.*")]
        )
        self.add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择音乐文件夹")
        if not folder:
            return
        paths = [str(p) for p in P(folder).rglob("*") if p.is_file() and (p.suffix.lower() in SUPPORTED or p.suffix.lower() == ".cue")]
        self.add_paths(paths)

    def add_paths(self, paths):
        # CUE files are auxiliary split metadata; associate them with audio by basename and folder.
        audio_paths = []
        cue_paths = []
        for path in paths:
            path = str(path)
            if P(path).suffix.lower() == ".cue":
                cue_paths.append(path)
                continue
            if P(path).suffix.lower() in SUPPORTED:
                audio_paths.append(path)
        # Match CUE to explicitly selected audio by its FILE reference, or same basename.
        for cue_path in cue_paths:
            cue_stem_key = str(P(cue_path).parent / P(cue_path).stem
            )
            try:
                cue_info = parse_cue_file(cue_path)
                references = {P(name).name.lower() for name in cue_info.get("files", [])}
            except Exception:
                references = set()
            associated = [audio for audio in audio_paths if P(audio).name.lower() in references]
            if not associated:
                same_stem = [audio for audio in audio_paths if P(audio).stem.lower() == P(cue_path).stem.lower()]
                associated = same_stem
            if associated:
                for audio in associated: self.cue_files[str(audio)] = cue_path
            else:
                # Save a stem-level association so a later add of the audio can discover it.
                self.cue_files[cue_stem_key] = cue_path
        # Auto-match sidecar CUEs (case-insensitive suffix, same stem).
        existing = {x["source"] for x in self.items}
        for path in audio_paths:
            path = str(path)
            if path in existing:
                continue
            if path not in self.cue_files:
                cue = P(path).with_suffix(".cue")
                if cue.exists(): self.cue_files[path] = str(cue)
                else:
                    cue_upper = P(path).with_suffix(".CUE")
                    if cue_upper.exists(): self.cue_files[path] = str(cue_upper)
            tags = read_tags(path)
            # If track tag is empty, extract a likely track number from the filename.
            match = TRACK_PREFIX.match(P(path).stem)
            if not tags["tracknumber"] and match:
                tags["tracknumber"] = normalize_track_number(match.group(1) or match.group(2))
            artwork = self.find_folder_artwork(path)
            self.items.append({
                "source": path, "tags": tags, "output": None, "status": "待转换",
                "artwork": artwork, "artwork_source": "文件夹自动识别" if artwork else "待检查"
            })
            existing.add(path)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for idx, item in enumerate(self.items):
            t = item["tags"]
            self.tree.insert("", "end", iid=str(idx), values=(
                P(item["source"]).name, t["title"], t["artist"], t["album"],
                t["albumartist"], t["date"], t["tracknumber"],
                self._audio_quality_badge(item["source"]),
                ("有：" + P(item["artwork"]).name if item.get("artwork") else "未识别"),
                item["status"]
            ), tags=("odd" if idx % 2 else "even",))

    def selected_indices(self):
        return [int(i) for i in self.tree.selection()]

    def remove_selected(self):
        for idx in sorted(self.selected_indices(), reverse=True):
            del self.items[idx]
        self.refresh()

    def preview_cleanup(self):
        indices = self.selected_indices() or list(range(len(self.items)))
        if not indices:
            messagebox.showinfo("提示", "请先添加音乐文件。")
            return

        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "歌名预处理预览", "760x440")
        body = self._popup_body(win, 12)
        table = ttk.Treeview(body, columns=("filename", "old", "new", "track"), show="headings")
        table.heading("filename", text="源文件名（不含路径）")
        table.heading("old", text="当前标签歌名")
        table.heading("new", text="清理后歌名")
        table.heading("track", text="提取曲序")
        table.column("filename", width=260)
        table.column("old", width=200)
        table.column("new", width=200)
        table.column("track", width=70)
        table.pack(fill="both", expand=True)

        proposals = {}
        changed = 0
        for idx in indices:
            old = self.items[idx]["tags"]["title"]
            new, extracted_track = clean_title(old, self.items[idx]["tags"].get("artist", ""), self.items[idx]["source"])
            existing_track = self.items[idx]["tags"]["tracknumber"]
            proposed_track = existing_track or extracted_track
            proposals[idx] = (new, proposed_track)
            source_name = P(self.items[idx]["source"]).name
            table.insert("", "end", iid=str(idx), values=(source_name, old, new, proposed_track))
            if new != old or (not existing_track and extracted_track):
                changed += 1

        ttk.Label(body, text=f"{len(indices)} 首歌曲 · {changed} 首预计修改", style="PopupMuted.TLabel").pack(anchor="w", pady=(8, 0))

        def apply_changes():
            for idx, (new, track) in proposals.items():
                self.items[idx]["tags"]["title"] = new
                if not self.items[idx]["tags"]["tracknumber"] and track:
                    self.items[idx]["tags"]["tracknumber"] = track
            self.refresh()
            win.destroy()

        row = ttk.Frame(body, style="Popup.TFrame")
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="取消", command=win.destroy, style="Popup.TButton").pack(side="right", padx=(6, 0))
        ttk.Button(row, text="应用", command=apply_changes, style="PopupAccent.TButton").pack(side="right")

    def batch_edit(self):
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("提示", "请先选中需要批量修改的歌曲。")
            return
        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "批量编辑歌曲信息", "460x330")
        body = self._popup_body(win, 14)

        editable = [
            ("artist", "歌手 Artist"),
            ("albumartist", "专辑艺术家 Album Artist"),
            ("album", "专辑 Album"),
            ("date", "年份 Date"),
            ("genre", "流派 Genre"),
        ]
        entries = {}
        for key, label in editable:
            frame = ttk.Frame(body, style="Popup.TFrame")
            frame.pack(fill="x", pady=5)
            ttk.Label(frame, text=label, width=20, style="Popup.TLabel").pack(side="left")
            entry = ttk.Entry(frame)
            entry.pack(side="left", fill="x", expand=True)
            entries[key] = entry

        def apply():
            values = {k: e.get().strip() for k, e in entries.items()}
            for idx in indices:
                for key, val in values.items():
                    if val:
                        self.items[idx]["tags"][key] = val
            self.refresh()
            win.destroy()

        ttk.Button(body, text="应用修改", command=apply, style="PopupAccent.TButton").pack(anchor="e", pady=(12, 0))

    def edit_one(self):
        indices = self.selected_indices()
        if len(indices) != 1:
            messagebox.showinfo("提示", "请选中一首歌曲再编辑。")
            return
        idx = indices[0]
        tags = self.items[idx]["tags"]
        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "编辑歌曲信息", "480x430")
        body = self._popup_body(win, 14)
        entries = {}
        for row, (key, label) in enumerate(TAG_FIELDS):
            ttk.Label(body, text=label, style="Popup.TLabel").grid(row=row, column=0, padx=(0, 10), pady=5, sticky="w")
            ent = ttk.Entry(body, width=42)
            ent.insert(0, tags.get(key, ""))
            ent.grid(row=row, column=1, padx=0, pady=5, sticky="ew")
            entries[key] = ent

        def save():
            for key, ent in entries.items():
                tags[key] = ent.get().strip()
            self.refresh()
            win.destroy()

        body.columnconfigure(1, weight=1)
        ttk.Button(body, text="保存", command=save, style="PopupAccent.TButton").grid(row=len(TAG_FIELDS), column=1, pady=(14, 0), sticky="e")

    def find_folder_artwork(self, audio_path):
        """Detect common cover-art filenames next to a source audio file."""
        folder = P(audio_path).parent
        preferred = ["cover.jpg", "cover.jpeg", "folder.jpg", "folder.jpeg",
                     "album.jpg", "album.jpeg", "front.jpg", "front.jpeg",
                     "cover.png", "folder.png", "album.png", "front.png",
                     "cover.webp", "folder.webp", "album.webp", "front.webp"]
        file_map = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
        for name in preferred:
            if name.lower() in file_map:
                return str(file_map[name.lower()])
        images = [p for p in folder.iterdir() if p.is_file()
                  and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        if len(images) == 1:
            return str(images[0])
        return None

    def set_artwork(self):
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("设置封面", "请先选中歌曲。")
            return
        image_path = filedialog.askopenfilename(
            title="选择专辑封面",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")]
        )
        if not image_path:
            return
        for idx in indices:
            self.items[idx]["artwork"] = image_path
            self.items[idx]["artwork_source"] = "手动指定"
        self.refresh()

    def choose_format(self):
        """Compact output-format selector, matching the application's light theme."""
        win = tk.Toplevel(self.root)
        c = self._prepare_popup(win, "输出格式", "360x180")
        body = self._popup_body(win, 18)
        ttk.Label(body, text="输出格式", style="Popup.TLabel").pack(anchor="w", pady=(0, 8))
        fmt = ttk.Combobox(body, state="readonly", values=("ALAC", "FLAC", "WAV", "AIFF", "MP3", "AAC", "Opus"), width=14)
        fmt.set(self.output_format.get()); fmt.pack(anchor="w")
        hint = ttk.Label(body, text="", style="PopupMuted.TLabel"); hint.pack(anchor="w", pady=(6, 0))
        def update_hint(_event=None):
            hint.configure(text="无损输出" if fmt.get() in {"ALAC", "FLAC", "WAV", "AIFF"} else "有损输出")
        fmt.bind("<<ComboboxSelected>>", update_hint); update_hint()
        actions = ttk.Frame(body, style="Popup.TFrame"); actions.pack(side="bottom", fill="x", pady=(14, 0))
        def apply():
            chosen = fmt.get().strip()
            self.output_format.set(chosen)
            # Close first so the user receives an immediate, visible confirmation.
            win.destroy()
            self._update_settings_summary()
            self.set_status(f"输出格式已应用：{chosen}（{self._format_loss_label(chosen)}）")
        ttk.Button(actions, text="取消", command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="应用", command=apply, style="PopupAccent.TButton").pack(side="right")
        win.transient(self.root); win.grab_set()

    def choose_output(self):
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_dir = folder
            self.output_dir_customized = True
            self.output_path_display.set("输出目录：\n" + folder)
            self.output_path_label.pack(anchor="w", padx=12, pady=(3, 8))
            self._update_settings_summary()
            self.set_status("输出目录已更新")

    def open_output(self):
        os.makedirs(self.output_dir, exist_ok=True)
        subprocess.Popen(["open", self.output_dir])

    def apple_music_auto_add_folder(self):
        """Legacy path retained only for display compatibility; importing no longer relies on it."""
        return None

    def toggle_auto_import(self):
        state = "开启" if self.auto_import.get() else "关闭"
        self._update_settings_summary()
        self.set_status(f"Apple Music 自动加入已{state}")


    def preview_split_tracks(self):
        indices = self.selected_indices()
        if len(indices) != 1:
            messagebox.showinfo("拆分预览", "请只选中一个合集音频，预览并编辑其曲目。")
            return
        idx = indices[0]
        item = self.items[idx]
        self.set_status(f"准备拆分预览：{P(item['source']).name}（可能需要分析静音）")
        def prepare():
            try:
                audio, info, tracks, mode = self._split_plan_for_item(item)
                # Use edited item-level album metadata as the authoritative values.
                info = dict(info)
                info['album'] = item['tags'].get('album') or info.get('album') or P(audio).stem
                info['artist'] = item['tags'].get('albumartist') or item['tags'].get('artist') or info.get('artist', '')
                for track in tracks:
                    if not track.get('artist'):
                        track['artist'] = item['tags'].get('artist') or info['artist']
                self.root.after(0, lambda: self._show_split_preview(idx, audio, info, tracks, mode))
            except Exception as exc:
                self.root.after(0, lambda e=exc: messagebox.showerror("拆分预览失败", str(e)))
                self.set_status("拆分预览失败")
        threading.Thread(target=prepare, daemon=True).start()

    def _show_split_preview(self, idx, audio, info, tracks, mode):
        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "拆分曲目预览", "860x500")
        body = self._popup_body(win, 12)
        ttk.Label(body, text=f"{info.get('album','')}  ·  {len(tracks)} 首", style="PopupMuted.TLabel").pack(anchor="w", pady=(0, 8))
        cols=('number','title','artist','start','end','duration')
        table=ttk.Treeview(body,columns=cols,show='headings',selectmode='browse')
        labels={'number':'曲序','title':'歌曲名称','artist':'歌手','start':'开始时间','end':'结束时间','duration':'时长'}
        widths={'number':65,'title':300,'artist':180,'start':100,'end':100,'duration':90}
        for c in cols:
            table.heading(c,text=labels[c]); table.column(c,width=widths[c],anchor='w')
        table.pack(fill='both',expand=True)
        def timefmt(v):
            v=float(v); m=int(v//60); sec=v-m*60
            return f"{m:02d}:{sec:05.2f}"
        def refill():
            table.delete(*table.get_children())
            for i,t in enumerate(tracks):
                table.insert('', 'end', iid=str(i), values=(t.get('track',i+1),t.get('title',''),t.get('artist',''),timefmt(t['start']),timefmt(t['end']),timefmt(t['end']-t['start'])))
        refill()
        ttk.Label(body,text="双击曲目或时间可编辑",style="PopupMuted.TLabel").pack(anchor="w", pady=(6, 0))
        def edit_cell(event):
            row=table.identify_row(event.y); col=table.identify_column(event.x)
            if not row or col not in ('#1','#2','#3','#4','#5'): return
            ix=int(row); field={'#1':'track','#2':'title','#3':'artist','#4':'start','#5':'end'}[col]
            old=tracks[ix].get(field,'')
            editor=tk.Toplevel(win); self._prepare_popup(editor, '编辑曲目信息', '380x170', parent=win)
            eb=self._popup_body(editor, 12)
            ttk.Label(eb,text={'track':'曲序','title':'歌曲名称','artist':'歌手','start':'开始时间','end':'结束时间'}[field],style="Popup.TLabel").pack(anchor="w",pady=(0,6))
            ent=ttk.Entry(eb,width=38); ent.pack(fill="x"); ent.insert(0,timefmt(old) if field in ('start','end') else str(old)); ent.focus_set()
            def save():
                value=ent.get().strip()
                if field=='track':
                    try: tracks[ix][field]=int(value)
                    except ValueError: messagebox.showerror('曲序无效','曲序必须是数字。',parent=editor); return
                elif field in ('start','end'):
                    try: new_time=parse_time_value(value)
                    except Exception as exc: messagebox.showerror('时间格式错误',str(exc),parent=editor); return
                    old_times=[(float(t['start']),float(t['end'])) for t in tracks]
                    if field=='start':
                        tracks[ix]['start']=new_time
                        if ix>0: tracks[ix-1]['end']=new_time
                    else:
                        tracks[ix]['end']=new_time
                        if ix+1<len(tracks): tracks[ix+1]['start']=new_time
                    invalid=any(t['start']<0 or t['end']<=t['start'] for t in tracks)
                    if invalid:
                        for t,(st,en) in zip(tracks,old_times): t['start'],t['end']=st,en
                        messagebox.showerror('时间边界无效','调整后存在负数或结束时间不晚于开始时间。',parent=editor); return
                else: tracks[ix][field]=value
                refill(); editor.destroy()
            ttk.Button(eb,text='保存',command=save,style="PopupAccent.TButton").pack(anchor="e",pady=(10,0))
        table.bind('<Double-1>',edit_cell)
        def apply_preview():
            self.split_plans[self.items[idx]['source']]={'audio':audio,'info':dict(info),'tracks':[dict(t) for t in tracks],'mode':mode}
            self.items[idx]['status']=f'已预览 {len(tracks)} 首'
            self.refresh(); win.destroy()
            self.set_status(f"拆分曲目已确认：{len(tracks)} 首；开始工作时将使用此预览")
        row=ttk.Frame(body,style="Popup.TFrame"); row.pack(fill="x",pady=(12,0))
        ttk.Button(row,text='取消',command=win.destroy,style="Popup.TButton").pack(side='right',padx=(6,0))
        ttk.Button(row,text='确认',command=apply_preview,style="PopupAccent.TButton").pack(side='right')

    def import_into_apple_music(self, files):
        """Import generated files directly into Music.app through AppleScript."""
        imported=[]; failures=[]
        # Use argv so filenames are not interpolated into AppleScript source.
        script='on run argv\n  tell application "Music" to add (POSIX file (item 1 of argv))\nend run'
        for file_path in files:
            if self.cancel_requested.is_set(): break
            try:
                proc=self._run_cancellable_process(['osascript','-e',script,file_path], timeout=60)
                if proc.returncode==0: imported.append(file_path)
                else: failures.append(f"{P(file_path).name}: {proc.stderr.strip() or proc.stdout.strip()}")
            except InterruptedError:
                break
            except Exception as exc:
                failures.append(f"{P(file_path).name}: {exc}")
        return imported, failures

    def _split_plan_for_item(self, item):
        """Build a split plan from a sidecar CUE, or estimate boundaries via silence detection."""
        audio = item["source"]
        cue = self.cue_files.get(audio) or self.cue_files.get(str(P(audio).parent / P(audio).stem))
        if not cue:
            sidecar = P(audio).with_suffix(".cue")
            if sidecar.exists(): cue = str(sidecar)
        if cue and P(cue).exists():
            info = parse_cue_file(cue)
            source_names = {P(x).name.lower() for x in info.get("files", [])}
            selected_name = P(audio).name.lower()
            # CUE FILE references may point to a sibling audio source. If exactly one exists, use it.
            if source_names and selected_name not in source_names:
                found = [P(cue).parent / name for name in info.get("files", []) if (P(cue).parent / name).exists()]
                if len(found) == 1:
                    audio = str(found[0])
                else:
                    raise ValueError(f"CUE 引用的音频与列表文件不匹配：{P(audio).name}")
            tracks = [t for t in info["tracks"] if not t.get("file") or P(t["file"]).name.lower() == P(audio).name.lower()]
            if not tracks: raise ValueError("CUE 没有包含所选音频对应的曲目")
            duration = self._audio_duration(audio)
            for i, track in enumerate(tracks):
                track["end"] = tracks[i+1]["start"] if i+1 < len(tracks) else duration
            return audio, info, tracks, "CUE"
        # Without CUE, use silence detection as an estimate. The status remains visible in the main window.
        self.set_status(f"检测静音分段：{P(audio).name}")
        proc = subprocess.run([self.ffmpeg, "-hide_banner", "-i", audio,
            "-af", "silencedetect=noise=-35dB:d=1.5", "-f", "null", "-"], capture_output=True, text=True)
        log = proc.stderr
        duration = self._audio_duration(audio)
        silence_starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
        silence_ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
        boundaries = [0.0]
        for st in silence_starts:
            en = next((x for x in silence_ends if x >= st), st)
            midpoint = (st + en) / 2
            if 3 < midpoint < duration - 3: boundaries.append(midpoint)
        boundaries.append(duration); boundaries = sorted(set(round(x, 3) for x in boundaries))
        tracks = [{"track": i+1, "title": f"Track {i+1:02d}", "artist": item["tags"].get("artist", ""),
                  "start": boundaries[i], "end": boundaries[i+1]} for i in range(len(boundaries)-1)]
        return audio, {"album": item["tags"].get("album") or P(audio).stem, "artist": item["tags"].get("albumartist") or item["tags"].get("artist", "")}, tracks, "静音检测估算"

    def _audio_duration(self, audio):
        proc = subprocess.run([self.ffmpeg, "-i", audio], capture_output=True, text=True)
        dm = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if not dm: raise RuntimeError("无法读取音频时长")
        h, m, sec = dm.groups(); return int(h)*3600 + int(m)*60 + float(sec)

    def _run_cancellable_process(self, cmd, timeout=None):
        """Run a child process while allowing the UI stop button to terminate it."""
        if self.cancel_requested.is_set():
            raise InterruptedError("任务已取消")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.current_process = proc
        started = __import__("time").monotonic()
        try:
            while proc.poll() is None:
                if self.cancel_requested.is_set():
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                    out, err = proc.communicate()
                    raise InterruptedError("任务已取消")
                if timeout is not None and __import__("time").monotonic() - started > timeout:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                    out, err = proc.communicate()
                    raise TimeoutError(f"子进程超过 {timeout} 秒，已终止")
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                out, err = proc.communicate()
            if self.cancel_requested.is_set():
                raise InterruptedError("任务已取消")
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        finally:
            self.current_process = None

    def stop_work(self):
        if not self.busy:
            return
        self.cancel_requested.set()
        self.set_status("正在停止任务…")
        self.stop_button.configure(state="disabled")
        proc = self.current_process
        if proc and proc.poll() is None:
            try: proc.terminate()
            except Exception: pass

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("任务仍在运行", "任务正在运行。停止任务并退出吗？"):
                return
            self.closing_requested = True
            self.cancel_requested.set()
            proc = self.current_process
            if proc and proc.poll() is None:
                try: proc.terminate()
                except Exception: pass
            self.set_status("正在停止任务并退出…")
            # The worker will finish cleanup and close the window on its own thread-safe callback.
            return
        self.root.destroy()

    def start_work(self, action="convert"):
        if self.busy: return
        if not self.items:
            messagebox.showinfo("提示", "请先添加音频文件。")
            return
        selected = self.selected_indices()
        if not selected:
            messagebox.showinfo("提示", "请先在列表中选中要处理的音频文件。")
            return
        if not self.ffmpeg:
            messagebox.showerror("缺少 FFmpeg", "请先安装 FFmpeg：brew install ffmpeg")
            return
        split_enabled = action in {"split", "split_convert"}
        convert_enabled = action in {"convert", "split_convert"}
        # Preview/edit is optional. If the user has not confirmed a plan, the worker
        # will auto-detect boundaries from CUE or silence detection. Silence detection
        # is approximate, so preview remains available as a correction step.
        fmt = self.output_format.get()
        if split_enabled and not convert_enabled:
            fallback = [P(self.items[i]['source']).name for i in selected if P(self.items[i]['source']).suffix.lower() in {'.ape', '.dsf', '.dff'}]
            if fallback and not messagebox.askyesno("部分源格式无法直接拆分", "以下源格式在常见 FFmpeg 构建中没有对应输出封装，拆分时会转为 FLAC：\n\n" + "\n".join(fallback[:8]) + "\n\n是否继续？"):
                return
        if split_enabled and not convert_enabled:
            # Keep source extension and codec when splitting without format conversion.
            pass
        elif not split_enabled:
            lossy = {".mp3", ".aac", ".ogg", ".opus"}
            lossless = {"ALAC", "FLAC", "WAV", "AIFF"}
            if any(P(self.items[i]["source"]).suffix.lower() in lossy for i in selected) and fmt in lossless:
                if not messagebox.askyesno("有损转无损", "有损源转换为无损格式不会恢复音质，仍要继续？"): return
        self.busy = True
        self.cancel_requested.clear()
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(maximum=len(selected), value=0)
        self.set_status("任务已启动…")
        threading.Thread(target=self.work_worker, args=(selected, split_enabled, convert_enabled, fmt), daemon=True).start()

    def work_worker(self, selected, split_enabled, convert_enabled, fmt):
        successes = 0; failures = []; generated_files = []
        for n, idx in enumerate(selected, start=1):
            if self.cancel_requested.is_set(): break
            item = self.items[idx]
            src = item["source"]
            self.root.after(0, lambda i=idx: self.items[i].update(status="处理中"))
            self.root.after(0, self.refresh)
            try:
                if split_enabled:
                    task_generated = []
                    plan = self.split_plans.get(src)
                    if not plan:
                        self.set_status(f"自动分析拆分边界：{P(src).name}（可提前预览/编辑以修正曲目）")
                        audio, info, tracks, mode = self._split_plan_for_item(item)
                        info = dict(info)
                        info['album'] = item['tags'].get('album') or info.get('album') or P(audio).stem
                        info['artist'] = item['tags'].get('albumartist') or item['tags'].get('artist') or info.get('artist', '')
                        for track in tracks:
                            if not track.get('artist'):
                                track['artist'] = item['tags'].get('artist') or info['artist']
                        plan = {'audio': audio, 'info': info, 'tracks': tracks, 'mode': mode}
                        self.split_plans[src] = plan
                    audio, info, tracks, mode = plan['audio'], plan['info'], plan['tracks'], plan['mode']
                    output = P(self.output_dir)
                    output.mkdir(parents=True, exist_ok=True)
                    if convert_enabled:
                        ext, codec = self._format_spec(fmt)
                    else:
                        (ext, codec), fallback_flac = self._split_source_spec(src)
                        # For APE/DSF/DFF, FLAC is the practical output because FFmpeg's
                        # commonly available muxers cannot write the original container.
                        if fallback_flac:
                            self.set_status(f"{P(src).suffix.upper()} 不支持直接分段输出，改为 FLAC：{P(src).name}")
                    album = safe_component(item["tags"].get("album") or info.get("album") or P(audio).stem, P(audio).stem)
                    album_artist = item["tags"].get("albumartist") or item["tags"].get("artist") or info.get("artist", "")
                    cover = item.get("artwork") or self.find_folder_artwork(audio)
                    date = item["tags"].get("date", "")
                    # v14: segment the complete album in one FFmpeg invocation.
                    # Stage all files outside the final output folder so cancellation/failure
                    # never leaves partial tracks where Apple Music can auto-import them.
                    import tempfile
                    first_start = float(tracks[0]["start"])
                    last_end = float(tracks[-1]["end"])
                    if first_start < 0 or last_end <= first_start:
                        raise ValueError("拆分时间范围无效")
                    for j, track in enumerate(tracks, start=1):
                        start_t, end_t = float(track["start"]), float(track["end"])
                        if start_t < first_start or end_t <= start_t or end_t > last_end + 0.01:
                            raise ValueError(f"第 {j} 首时间段无效")
                    # Segment timestamps are relative to the selected first-track start.
                    boundaries = [float(t["end"]) - first_start for t in tracks[:-1]]
                    boundaries = [b for b in boundaries if b > 0]
                    with tempfile.TemporaryDirectory(prefix="musicimporter_stage_") as stage_dir:
                        pattern = str(P(stage_dir) / f"segment_%03d.{ext}")
                        muxer = {"m4a":"mp4", "aiff":"aiff", "wav":"wav", "flac":"flac", "mp3":"mp3", "ogg":"ogg", "opus":"opus", "wv":"wv"}.get(ext, ext)
                        cmd = [self.ffmpeg, "-nostdin", "-y", "-ss", f"{first_start:.6f}", "-i", audio,
                               "-map", "0:a:0", *codec, "-f", "segment", "-segment_start_number", "0",
                               "-reset_timestamps", "1", "-segment_format", muxer]
                        if boundaries:
                            cmd += ["-segment_times", ",".join(f"{b:.6f}" for b in boundaries)]
                        cmd += [pattern]
                        self.set_status(f"批量拆分 {P(src).name} · {len(tracks)} 首（单次 FFmpeg）")
                        pr = self._run_cancellable_process(cmd, timeout=6*60*60)
                        if pr.returncode != 0:
                            raise RuntimeError(f"批量分段失败（FFmpeg）：\n{pr.stderr[-1500:]}")
                        staged = sorted(P(stage_dir).glob(f"segment_*.{ext}"))
                        if len(staged) != len(tracks):
                            raise RuntimeError(f"批量分段结果数量不匹配：预期 {len(tracks)} 首，实际 {len(staged)} 首。\n{pr.stderr[-800:]}")
                        # Write tags and artwork to staged tracks, then move into final
                        # names only after every segment has been produced successfully.
                        final_paths = []
                        for j, (track, staged_file) in enumerate(zip(tracks, staged), start=1):
                            self.set_status(f"整理标签与封面 {j}/{len(tracks)}：{track.get('title', '')}")
                            title = str(track.get("title") or f"Track {j:02d}")
                            number = int(track.get("track", j))
                            artist = track.get("artist") or item["tags"].get("artist") or album_artist
                            self._write_track_metadata(staged_file, title, artist, album, album_artist,
                                number, date, item["tags"].get("genre", ""), item["tags"].get("discnumber", ""), cover)
                            dest = output / f"{number:02d} - {safe_component(title, f'Track {j:02d}')}.{ext}"
                            if dest.exists():
                                counter = 2
                                base = dest.stem
                                while (output / f"{base} ({counter}).{ext}").exists(): counter += 1
                                dest = output / f"{base} ({counter}).{ext}"
                            final_paths.append((staged_file, dest))
                        # Commit staged outputs. If cancellation arrives before commit,
                        # no new final files are published.
                        if self.cancel_requested.is_set():
                            raise InterruptedError("任务已取消")
                        for staged_file, dest in final_paths:
                            if self.cancel_requested.is_set():
                                raise InterruptedError("任务已取消")
                            staged_file.replace(dest)
                            task_generated.append(str(dest))
                    generated_files.extend(task_generated)
                    self.root.after(0, lambda i=idx: self.items[i].update(status=f"拆分完成 ({mode})"))
                    generated_files.extend(task_generated)
                    self.root.after(0, lambda i=idx: self.items[i].update(status=f"拆分完成 ({mode})"))
                else:
                    # Standard format conversion: reuse existing conversion worker on selected items only.
                    self.convert_single_item(item, fmt)
                    if item.get('output'):
                        generated_files.append(item['output'])
                    self.root.after(0, lambda i=idx: self.items[i].update(status="转换完成"))
                successes += 1
            except InterruptedError:
                self.root.after(0, lambda i=idx: self.items[i].update(status="已取消"))
                self.root.after(0, self.refresh)
                break
            except Exception as exc:
                failures.append(f"{P(src).name}: {exc}")
                self.root.after(0, lambda i=idx: self.items[i].update(status="失败"))
            self.root.after(0, self.refresh)
            self.root.after(0, lambda v=n: self.progress.configure(value=v))
            self.set_status(f"任务进度 {n}/{len(selected)} · 成功 {successes} · 失败 {len(failures)}")
        if not self.cancel_requested.is_set():
            self.root.after(0, lambda: self.progress.configure(value=len(selected)))
        import_summary = ""
        if self.auto_import.get() and generated_files and not self.cancel_requested.is_set():
            self.set_status("正在导入 Apple Music…")
            imported, import_failures = self.import_into_apple_music(generated_files)
            import_summary = f"\nApple Music 导入成功：{len(imported)} 首；导入失败：{len(import_failures)} 首"
            if import_failures:
                import_summary += "\n导入错误：\n" + "\n".join(import_failures[:5])
        cancelled = self.cancel_requested.is_set()
        self.busy=False
        self.root.after(0, lambda: [button.configure(state="normal") for button in self.action_buttons])
        self.root.after(0, lambda: self.stop_button.configure(state="disabled"))
        msg=("任务已取消" if cancelled else "任务完成") + f"\n完成 {successes} 个任务 · 失败 {len(failures)}" + import_summary
        if failures: msg += "\n\n" + "\n".join(failures[:5])
        if not self.closing_requested:
            self.root.after(0, lambda: messagebox.showinfo("任务结果", msg))
        self.set_status(("任务已取消" if cancelled else "任务完成") + f"：成功 {successes} · 失败 {len(failures)}")
        if self.closing_requested:
            self.root.after(0, self.root.destroy)

    def _format_spec(self, fmt):
        return {"ALAC":("m4a",["-c:a","alac"]),"FLAC":("flac",["-c:a","flac"]),
            "WAV":("wav",["-c:a","pcm_s24le"]),"AIFF":("aiff",["-c:a","pcm_s24be"]),
            "MP3":("mp3",["-c:a","libmp3lame","-b:a","320k"]),"AAC":("m4a",["-c:a","aac","-b:a","256k"]),
            "Opus":("opus",["-c:a","libopus","-b:a","192k"])}[fmt]

    def _source_format_spec(self, src):
        ext=P(src).suffix.lower()
        return {".flac":("flac",["-c:a","flac"]),".wav":("wav",["-c:a","pcm_s24le"]),
            ".ape":("ape",["-c:a","ape"]),".aiff":("aiff",["-c:a","pcm_s24be"]),".aif":("aiff",["-c:a","pcm_s24be"]),
            ".mp3":("mp3",["-c:a","libmp3lame","-b:a","320k"]),".m4a":("m4a",["-c:a","aac","-b:a","256k"]),
            ".aac":("m4a",["-c:a","aac","-b:a","256k"]),".ogg":("ogg",["-c:a","libvorbis"]),".opus":("opus",["-c:a","libopus","-b:a","192k"]),
            ".wv":("wv",["-c:a","wavpack"]),".dsf":("dsf",["-c:a","pcm_s24le"]),".dff":("dff",["-c:a","pcm_s24le"])}.get(ext, (ext.lstrip("."),["-c:a","flac"]))

    def _split_source_spec(self, src):
        """Choose a stream-copy container when FFmpeg can mux it; otherwise fallback to FLAC."""
        ext = P(src).suffix.lower()
        copy_specs = {
            ".flac": ("flac", ["-c:a", "copy"]),
            ".wav": ("wav", ["-c:a", "copy"]),
            ".aiff": ("aiff", ["-c:a", "copy"]),
            ".aif": ("aiff", ["-c:a", "copy"]),
            ".mp3": ("mp3", ["-c:a", "copy"]),
            ".m4a": ("m4a", ["-c:a", "copy"]),
            ".aac": ("m4a", ["-c:a", "copy"]),
            ".ogg": ("ogg", ["-c:a", "copy"]),
            ".opus": ("opus", ["-c:a", "copy"]),
            ".wv": ("wv", ["-c:a", "copy"]),
        }
        if ext in copy_specs:
            return copy_specs[ext], False
        # FFmpeg can decode these source formats, but it does not provide a matching
        # output muxer in common builds. Split them into FLAC rather than failing.
        return ("flac", ["-c:a", "flac"]), True

    def _write_track_metadata(self, path, title, artist, album, album_artist, track, date, genre, disc, cover):
        """Best-effort cross-format tags and artwork using Mutagen after batch splitting."""
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
        if audio is None:
            raise RuntimeError(f"无法写入输出文件标签：{P(path).name}")
        if audio.tags is None:
            try: audio.add_tags()
            except Exception: pass
        tags = audio.tags
        values = {"title": title, "artist": artist, "album": album, "albumartist": album_artist,
                  "date": date, "genre": genre, "tracknumber": str(track), "discnumber": str(disc)}
        # Easy tags cover the common FLAC/MP3/MP4/Ogg/Opus/AIFF cases.
        try:
            easy = MutagenFile(str(path), easy=True)
            if easy is not None and easy.tags is not None:
                for key, value in values.items():
                    if value not in (None, ""):
                        try: easy[key] = [str(value)]
                        except Exception: pass
                easy.save()
        except Exception:
            pass
        if cover and os.path.isfile(cover):
            ext = P(path).suffix.lower()
            try:
                from mutagen.flac import FLAC, Picture
                if ext == ".flac":
                    f = FLAC(path); pic = Picture(); pic.type = 3; pic.mime = "image/jpeg" if P(cover).suffix.lower() in {".jpg", ".jpeg"} else "image/png"; pic.desc = "Cover"; pic.data = P(cover).read_bytes(); f.clear_pictures(); f.add_picture(pic); f.save()
                elif ext == ".mp3":
                    from mutagen.id3 import ID3, APIC, error
                    m = ID3(path); m.delall("APIC"); m.add(APIC(encoding=3, mime="image/jpeg" if P(cover).suffix.lower() in {".jpg", ".jpeg"} else "image/png", type=3, desc="Cover", data=P(cover).read_bytes())); m.save()
                elif ext in {".aiff", ".aif"}:
                    from mutagen.id3 import ID3, APIC
                    m = ID3(path); m.delall("APIC"); m.add(APIC(encoding=3, mime="image/jpeg" if P(cover).suffix.lower() in {".jpg", ".jpeg"} else "image/png", type=3, desc="Cover", data=P(cover).read_bytes())); m.save()
                elif ext in {".m4a", ".aac"}:
                    from mutagen.mp4 import MP4, MP4Cover
                    m = MP4(path); fmt = MP4Cover.FORMAT_JPEG if P(cover).suffix.lower() in {".jpg", ".jpeg"} else MP4Cover.FORMAT_PNG; m.tags["covr"] = [MP4Cover(P(cover).read_bytes(), imageformat=fmt)]; m.save()
            except Exception as e:
                # Cover embedding is non-fatal for formats where Mutagen lacks support.
                self.set_status(f"封面写入未完成：{P(path).name}（{e}）")

    def convert_single_item(self, item, fmt):
        src=item["source"]; tags=item["tags"]
        ext,codec=self._format_spec(fmt)
        out_dir=P(self.output_dir)/safe_component(tags.get("album"),"Unknown Album"); out_dir.mkdir(parents=True,exist_ok=True)
        out=out_dir/(safe_component(tags.get("title"),P(src).stem)+"."+ext)
        if out.exists():
            suffix=2
            while (out_dir/(safe_component(tags.get("title"),P(src).stem)+f" ({suffix})."+ext)).exists(): suffix+=1
            out=out_dir/(safe_component(tags.get("title"),P(src).stem)+f" ({suffix})."+ext)
        art=item.get("artwork")
        embed_cover=bool(art and os.path.isfile(art) and ext in {"m4a","flac","mp3","aiff"})
        # Declare all inputs before output-side mapping and codec options (FFmpeg option-order requirement).
        cmd=[self.ffmpeg,"-nostdin","-y","-i",src]
        if embed_cover:
            cmd += ["-loop", "1", "-i",art]
        cmd += ["-map","0:a:0",*codec,"-map_metadata","0"]
        if embed_cover:
            cmd += ["-map","1:v:0","-c:v","mjpeg","-disposition:v","attached_pic","-shortest"]
        elif ext not in {"wav","opus"}:
            cmd += ["-map","0:v?","-c:v","copy","-disposition:v","attached_pic"]
        for key,val in [("title","title"),("artist","artist"),("album","album"),("album_artist","albumartist"),("date","date"),("genre","genre"),("track","tracknumber"),("disc","discnumber")]:
            cmd += ["-metadata",f"{key}={tags.get(val,'')}"]
        cmd.append(str(out))
        try:
            pr=self._run_cancellable_process(cmd, timeout=6*60*60)
        except InterruptedError:
            try:
                if out.exists(): out.unlink()
            except OSError: pass
            raise
        if pr.returncode != 0 or not out.exists() or not out.stat().st_size:
            try:
                if out.exists(): out.unlink()
            except OSError: pass
            if self.cancel_requested.is_set(): raise InterruptedError("任务已取消")
            raise RuntimeError(pr.stderr[-600:])
        item["output"]=str(out)



def main():
    root = tk.Tk()
    app = MusicImporter(root)
    root.mainloop()


if __name__ == "__main__":
    main()
