"""Compr - Football broadcast event clipper. See SPEC.txt."""
from __future__ import annotations

import os
import re
import sys
import time
import queue
import shutil
import threading
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import Tk, StringVar, IntVar, BooleanVar, Text, END, filedialog, messagebox
from tkinter import ttk

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
THEME = {
    "bg":         "#1e1e1e",
    "panel":      "#2a2a2a",
    "text":       "#ffffff",
    "muted":      "#9a9a9a",
    "green":      "#3fb950",
    "green_hi":   "#4ec463",
    "purple":     "#a371f7",
    "error":      "#f85149",
    "border":     "#3a3a3a",
    "mono":       ("Consolas", 10),
    "ui":         ("Segoe UI", 10),
    "ui_bold":    ("Segoe UI", 10, "bold"),
    "ui_h":       ("Segoe UI", 11, "bold"),
}

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
FFMPEG = SCRIPT_DIR / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")

INPUT_EXTS = (".mp4", ".mkv", ".mov")

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
EVENT_RE = re.compile(
    r"(H[12])\s*(\d+)'(\d+)\"\s*(?:·|\.)\s*(.*?)(?=H[12]\s*\d+'|\Z)",
    re.DOTALL,
)
KICKOFF_RE = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})\s*$")


@dataclass
class Event:
    half: str          # "H1" / "H2"
    minutes: int
    seconds: int
    label: str
    offset: float = 0.0  # seconds into source video, computed later

    @property
    def clock(self) -> str:
        return f"{self.minutes}'{self.seconds:02d}"


def parse_kickoff(text: str) -> float | None:
    m = KICKOFF_RE.match(text)
    if not m:
        return None
    h = int(m.group(1)) if m.group(1) else 0
    mm = int(m.group(2))
    ss = int(m.group(3))
    if mm >= 60 or ss >= 60:
        return None
    return h * 3600 + mm * 60 + ss


def fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_events(blob: str) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    errors: list[str] = []
    if not blob.strip():
        return events, errors
    for m in EVENT_RE.finditer(blob):
        half = m.group(1)
        mins = int(m.group(2))
        secs = int(m.group(3))
        label = m.group(4).strip().rstrip("·").strip()
        if not label:
            label = "Event"
        if secs >= 60:
            errors.append(f"{half} {mins}'{secs:02d}\": seconds >= 60")
            continue
        if half == "H2" and mins < 45:
            errors.append(f"H2 {mins}'{secs:02d}\": match clock < 45'")
            continue
        events.append(Event(half=half, minutes=mins, seconds=secs, label=label))
    return events, errors


def compute_offsets(events: list[Event], h1_kick: float, h2_kick: float) -> None:
    for e in events:
        clock_secs = e.minutes * 60 + e.seconds
        if e.half == "H1":
            e.offset = h1_kick + clock_secs
        else:
            e.offset = h2_kick + (clock_secs - 45 * 60)


def sanitize_label(label: str) -> str:
    out = re.sub(r"\s+", "_", label.strip())
    out = re.sub(r"[^A-Za-z0-9_]", "", out)
    return out or "Event"


# ---------------------------------------------------------------------------
# Grouping (multi-event merging)
# ---------------------------------------------------------------------------
@dataclass
class Group:
    indices: list[int]      # 1-based input indices
    events: list[Event]

    @property
    def first(self) -> Event:
        return self.events[0]

    @property
    def last(self) -> Event:
        return self.events[-1]


def group_events(events: list[Event], clip_length: float) -> list[Group]:
    """Greedy chain merge: consecutive events in the same half, gap <= clip_length."""
    groups: list[Group] = []
    cur: Group | None = None
    for i, e in enumerate(events, 1):
        if cur is None:
            cur = Group(indices=[i], events=[e])
            continue
        prev = cur.events[-1]
        if prev.half == e.half and (e.offset - prev.offset) <= clip_length:
            cur.indices.append(i)
            cur.events.append(e)
        else:
            groups.append(cur)
            cur = Group(indices=[i], events=[e])
    if cur is not None:
        groups.append(cur)
    return groups


def group_filename(g: Group) -> str:
    if len(g.events) == 1:
        e = g.events[0]
        return f"{g.indices[0]:02d}_{sanitize_label(e.label)}_{e.half}_{e.clock}.mp4"

    idx_str = f"{g.indices[0]:02d}-{g.indices[-1]:02d}"
    labels = [sanitize_label(e.label) for e in g.events]
    if all(l == labels[0] for l in labels):
        label_str = f"{labels[0]}_x{len(labels)}"
    else:
        label_str = "+".join(labels)
    tc = f"{g.first.clock}-{g.last.clock}"
    return f"{idx_str}_{label_str}_{g.first.half}_{tc}.mp4"


def group_window(g: Group, clip_length: float, duration: float) -> tuple[float, float]:
    half = clip_length / 2
    start = max(0.0, g.first.offset - half)
    end = min(duration, g.last.offset + half)
    return start, max(0.1, end - start)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(FFMPEG), *args],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )


def probe_duration(path: Path, timeout: float = 30.0) -> tuple[float | None, str]:
    """Return (duration_seconds_or_None, reason_string).

    Streams ffmpeg stderr and kills the process as soon as Duration appears.
    On failure, returns a short reason so the GUI can surface it instead of
    a silent 'unknown'.
    """
    if not FFMPEG.exists():
        return None, f"ffmpeg not found at {FFMPEG}"
    if not Path(path).exists():
        return None, "file does not exist"

    args = [str(FFMPEG), "-hide_banner", "-i", str(path)]
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, bufsize=1, creationflags=CREATE_NO_WINDOW,
        )
    except OSError as e:
        return None, f"ffmpeg launch failed: {e}"

    duration: float | None = None
    tail: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)
            m = DUR_RE.search(line)
            if m:
                duration = (int(m.group(1)) * 3600
                            + int(m.group(2)) * 60
                            + float(m.group(3)))
                break
            if time.monotonic() > deadline:
                break
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    if duration is not None:
        return duration, ""
    if time.monotonic() > deadline:
        return None, "probe timed out (30s)"
    return None, "Duration not found in ffmpeg output: " + "".join(tail[-5:]).strip()[:200]


def encode_clip(src: Path, start: float, length: float, dst: Path,
                keep_audio: bool = True) -> tuple[bool, str]:
    args = [
        "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{length:.3f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
    ]
    args += (["-c:a", "aac", "-b:a", "160k"] if keep_audio else ["-an"])
    args.append(str(dst))
    cp = _run_ffmpeg(args)
    if cp.returncode != 0:
        return False, "\n".join((cp.stderr or "").splitlines()[-15:])
    return True, ""


def encode_with_fade(src: Path, dst: Path, duration: float,
                     fade_dur: float = 0.5,
                     keep_audio: bool = True) -> tuple[bool, str]:
    """Re-encode src into dst with fade-in from black and fade-out to black."""
    fd = max(0.05, min(fade_dur, duration / 3.0))
    out_start = max(0.0, duration - fd)
    vf = f"fade=t=in:st=0:d={fd:.3f},fade=t=out:st={out_start:.3f}:d={fd:.3f}"
    args = ["-y", "-i", str(src), "-vf", vf]
    if keep_audio:
        af = f"afade=t=in:st=0:d={fd:.3f},afade=t=out:st={out_start:.3f}:d={fd:.3f}"
        args += ["-af", af, "-c:a", "aac", "-b:a", "160k"]
    else:
        args += ["-an"]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
        str(dst),
    ]
    cp = _run_ffmpeg(args)
    if cp.returncode != 0:
        return False, "\n".join((cp.stderr or "").splitlines()[-15:])
    return True, ""


def concat_clips(clip_paths: list[Path], dst: Path) -> tuple[bool, str]:
    list_file = dst.with_suffix(".concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in clip_paths:
            esc = str(p).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{esc}'\n")
    args = [
        "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    cp = _run_ffmpeg(args)
    try:
        list_file.unlink()
    except OSError:
        pass
    if cp.returncode != 0:
        return False, "\n".join((cp.stderr or "").splitlines()[-15:])
    return True, ""


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: Tk):
        self.root = root
        root.title("Compr")
        root.configure(bg=THEME["bg"])
        root.geometry("820x880")
        root.minsize(720, 800)

        self.video_path: Path | None = None
        self.video_duration: float | None = None

        self.ind_parent: Path | None = None
        self.compiled_path: Path | None = None

        self.events: list[Event] = []
        self.parse_errors: list[str] = []

        self.msg_q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_style()
        self._build_ui()
        self._poll_queue()
        self._refresh_state()

    # --- styling ------------------------------------------------------------
    def _build_style(self) -> None:
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        bg = THEME["bg"]; panel = THEME["panel"]; text = THEME["text"]
        green = THEME["green"]; purple = THEME["purple"]; border = THEME["border"]

        s.configure(".", background=bg, foreground=text, font=THEME["ui"])
        s.configure("TFrame", background=bg)
        s.configure("Panel.TFrame", background=panel)
        s.configure("TLabel", background=bg, foreground=text, font=THEME["ui"])
        s.configure("Header.TLabel", background=bg, foreground=purple, font=THEME["ui_h"])
        s.configure("Muted.TLabel", background=bg, foreground=THEME["muted"])
        s.configure("Error.TLabel", background=bg, foreground=THEME["error"])

        s.configure("TButton", background=panel, foreground=text,
                    bordercolor=border, focusthickness=1, padding=6)
        s.map("TButton",
              background=[("active", border), ("disabled", panel)],
              foreground=[("disabled", THEME["muted"])])

        s.configure("Primary.TButton", background=green, foreground="#0b1a0d",
                    font=THEME["ui_bold"], padding=10)
        s.map("Primary.TButton",
              background=[("active", THEME["green_hi"]), ("disabled", border)],
              foreground=[("disabled", THEME["muted"])])

        s.configure("TEntry", fieldbackground=panel, foreground=text,
                    bordercolor=border, insertcolor=text)
        s.map("TEntry", bordercolor=[("focus", purple)])

        s.configure("TCheckbutton", background=bg, foreground=text,
                    indicatorcolor=panel, focusthickness=0)
        s.map("TCheckbutton",
              background=[("active", bg)],
              indicatorcolor=[("selected", purple), ("!selected", panel)],
              foreground=[("disabled", THEME["muted"])])

        s.configure("TSpinbox", fieldbackground=panel, foreground=text,
                    background=panel, bordercolor=border, arrowcolor=text)

        s.configure("Compr.Horizontal.TProgressbar",
                    background=green, troughcolor=panel, bordercolor=border,
                    lightcolor=green, darkcolor=green)

    # --- layout -------------------------------------------------------------
    def _build_ui(self) -> None:
        wrap = ttk.Frame(self.root)
        wrap.pack(fill="both", expand=True, padx=14, pady=14)

        # 1. file row
        ttk.Label(wrap, text="Source video", style="Header.TLabel").pack(anchor="w")
        row1 = ttk.Frame(wrap); row1.pack(fill="x", pady=(2, 10))
        ttk.Button(row1, text="Browse…", command=self.on_browse_video).pack(side="left")
        self.path_var = StringVar(value="No file selected")
        ttk.Label(row1, textvariable=self.path_var, style="Muted.TLabel").pack(side="left", padx=10)
        self.dur_var = StringVar(value="")
        ttk.Label(row1, textvariable=self.dur_var, style="Muted.TLabel").pack(side="right")

        # 2. kickoffs
        ttk.Label(wrap, text="Kickoff times (HH:MM:SS or MM:SS)", style="Header.TLabel").pack(anchor="w")
        row2 = ttk.Frame(wrap); row2.pack(fill="x", pady=(2, 10))
        ttk.Label(row2, text="H1").pack(side="left")
        self.h1_var = StringVar()
        self.h1_entry = ttk.Entry(row2, textvariable=self.h1_var, width=12)
        self.h1_entry.pack(side="left", padx=(6, 18))
        self.h1_var.trace_add("write", lambda *_: self._refresh_state())
        ttk.Label(row2, text="H2").pack(side="left")
        self.h2_var = StringVar()
        self.h2_entry = ttk.Entry(row2, textvariable=self.h2_var, width=12)
        self.h2_entry.pack(side="left", padx=6)
        self.h2_var.trace_add("write", lambda *_: self._refresh_state())
        self.kick_err = StringVar()
        ttk.Label(row2, textvariable=self.kick_err, style="Error.TLabel").pack(side="left", padx=12)

        # 3. events textbox
        ttk.Label(wrap, text="Event timecodes (paste raw)", style="Header.TLabel").pack(anchor="w")
        self.events_text = Text(wrap, height=7, bg=THEME["panel"], fg=THEME["text"],
                                insertbackground=THEME["text"], font=THEME["mono"],
                                relief="flat", borderwidth=1, highlightthickness=1,
                                highlightbackground=THEME["border"],
                                highlightcolor=THEME["purple"], wrap="word")
        self.events_text.pack(fill="x", pady=(2, 8))
        self.events_text.bind("<KeyRelease>", lambda e: self._refresh_state())

        # 4. parsed preview
        ttk.Label(wrap, text="Parsed clips (with merging)", style="Header.TLabel").pack(anchor="w")
        prev_frame = ttk.Frame(wrap); prev_frame.pack(fill="both", expand=True, pady=(2, 8))
        self.preview = Text(prev_frame, height=10, bg=THEME["panel"], fg=THEME["text"],
                            font=THEME["mono"], relief="flat", borderwidth=1,
                            highlightthickness=1, highlightbackground=THEME["border"],
                            state="disabled", wrap="none")
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.tag_configure("err", foreground=THEME["error"])
        self.preview.tag_configure("ok", foreground=THEME["text"])
        self.preview.tag_configure("merge", foreground=THEME["purple"])
        self.preview.tag_configure("muted", foreground=THEME["muted"])
        sb = ttk.Scrollbar(prev_frame, command=self.preview.yview)
        sb.pack(side="right", fill="y")
        self.preview.config(yscrollcommand=sb.set)

        # 5. clip length + audio
        row5 = ttk.Frame(wrap); row5.pack(fill="x", pady=(2, 8))
        ttk.Label(row5, text="Clip length (s)").pack(side="left")
        self.len_var = IntVar(value=20)
        self.len_spin = ttk.Spinbox(row5, from_=2, to=120, increment=1, width=6,
                                    textvariable=self.len_var,
                                    command=self._refresh_state)
        self.len_spin.pack(side="left", padx=8)
        self.len_var.trace_add("write", lambda *_: self._refresh_state())
        self.audio_var = BooleanVar(value=True)
        ttk.Checkbutton(row5, text="Include audio",
                        variable=self.audio_var).pack(side="left", padx=20)

        # 6. output checkboxes
        ttk.Label(wrap, text="Output", style="Header.TLabel").pack(anchor="w")
        row6 = ttk.Frame(wrap); row6.pack(fill="x", pady=(2, 4))
        self.individual_var = BooleanVar(value=True)
        self.compiled_var = BooleanVar(value=False)
        ttk.Checkbutton(row6, text="Individual clips (folder)",
                        variable=self.individual_var,
                        command=self._on_output_toggle).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(row6, text="Compiled video (single file)",
                        variable=self.compiled_var,
                        command=self._on_output_toggle).pack(side="left")

        # 7a. individual destination
        self.ind_frame = ttk.Frame(wrap)
        ind_row1 = ttk.Frame(self.ind_frame); ind_row1.pack(fill="x", pady=(2, 2))
        ttk.Button(ind_row1, text="Parent folder…",
                   command=self.on_choose_ind_parent).pack(side="left")
        self.ind_parent_var = StringVar(value="No parent folder selected")
        ttk.Label(ind_row1, textvariable=self.ind_parent_var,
                  style="Muted.TLabel").pack(side="left", padx=10)
        ind_row2 = ttk.Frame(self.ind_frame); ind_row2.pack(fill="x", pady=(2, 6))
        ttk.Label(ind_row2, text="New folder name").pack(side="left")
        self.ind_name_var = StringVar(value="")
        self.ind_name_entry = ttk.Entry(ind_row2, textvariable=self.ind_name_var, width=40)
        self.ind_name_entry.pack(side="left", padx=8)
        self.ind_name_var.trace_add("write", lambda *_: self._refresh_state())

        # 7b. compiled destination
        self.cmp_frame = ttk.Frame(wrap)
        cmp_row = ttk.Frame(self.cmp_frame); cmp_row.pack(fill="x", pady=(2, 2))
        ttk.Button(cmp_row, text="Output file…",
                   command=self.on_choose_compiled).pack(side="left")
        self.cmp_path_var = StringVar(value="No file selected")
        ttk.Label(cmp_row, textvariable=self.cmp_path_var,
                  style="Muted.TLabel").pack(side="left", padx=10)
        cmp_row2 = ttk.Frame(self.cmp_frame); cmp_row2.pack(fill="x", pady=(0, 6))
        self.fade_var = BooleanVar(value=False)
        ttk.Checkbutton(cmp_row2,
                        text="Fade to/from black between clips (0.5s)",
                        variable=self.fade_var).pack(side="left")

        # 8. process
        self.process_btn = ttk.Button(wrap, text="Process",
                                      style="Primary.TButton",
                                      command=self.on_process)
        self.process_btn.pack(fill="x", pady=(8, 4))

        # 9. progress + status
        self.progress = ttk.Progressbar(wrap, mode="determinate",
                                        style="Compr.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(8, 4))
        self.status_var = StringVar(value="Idle.")
        ttk.Label(wrap, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")

        self._on_output_toggle()

    # --- file/output handlers ----------------------------------------------
    def on_browse_video(self) -> None:
        types = [("Video files", " ".join(f"*{e}" for e in INPUT_EXTS)),
                 ("All files", "*.*")]
        path = filedialog.askopenfilename(title="Select source video", filetypes=types)
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() not in INPUT_EXTS:
            messagebox.showerror("Compr", f"Unsupported extension: {p.suffix}")
            return
        self.video_path = p
        self.path_var.set(str(p))
        self.dur_var.set("Probing…")
        if not self.ind_name_var.get().strip():
            self.ind_name_var.set(f"{p.stem}_clips")
        threading.Thread(target=self._probe_thread, args=(p,), daemon=True).start()

    def _probe_thread(self, p: Path) -> None:
        dur, reason = probe_duration(p)
        self.msg_q.put(("duration", dur, reason))

    def _on_output_toggle(self) -> None:
        if self.individual_var.get():
            self.ind_frame.pack(fill="x", pady=(0, 4), after=self._ind_anchor())
        else:
            self.ind_frame.pack_forget()
        if self.compiled_var.get():
            self.cmp_frame.pack(fill="x", pady=(0, 4), after=self._cmp_anchor())
        else:
            self.cmp_frame.pack_forget()
        self._refresh_state()

    def _ind_anchor(self):
        # pack after the checkbox row; simplest is to repack at right position
        # by relying on natural ordering — already constructed in correct order.
        return None

    def _cmp_anchor(self):
        return None

    def on_choose_ind_parent(self) -> None:
        d = filedialog.askdirectory(title="Choose parent folder for clips")
        if d:
            self.ind_parent = Path(d)
            self.ind_parent_var.set(d)
            self._refresh_state()

    def on_choose_compiled(self) -> None:
        f = filedialog.asksaveasfilename(title="Save compiled video as",
                                          defaultextension=".mp4",
                                          filetypes=[("MP4 video", "*.mp4")])
        if f:
            self.compiled_path = Path(f)
            self.cmp_path_var.set(f)
            self._refresh_state()

    # --- live state refresh -------------------------------------------------
    def _refresh_state(self) -> None:
        h1 = parse_kickoff(self.h1_var.get())
        h2 = parse_kickoff(self.h2_var.get())
        kick_msg = ""
        if self.h1_var.get() and h1 is None:
            kick_msg = "H1 invalid"
        elif self.h2_var.get() and h2 is None:
            kick_msg = "H2 invalid"
        elif h1 is not None and h2 is not None and h2 <= h1:
            kick_msg = "H2 must be after H1"
        self.kick_err.set(kick_msg)

        try:
            length = int(self.len_var.get())
        except Exception:
            length = 0

        blob = self.events_text.get("1.0", END)
        self.events, self.parse_errors = parse_events(blob)
        if h1 is not None and h2 is not None:
            compute_offsets(self.events, h1, h2)

        self.groups = (
            group_events(self.events, float(length))
            if self.events and length > 0 else []
        )
        self._render_preview()

        ind_ok = (
            self.individual_var.get() is False
            or (self.ind_parent is not None and self.ind_name_var.get().strip() != "")
        )
        cmp_ok = self.compiled_var.get() is False or self.compiled_path is not None
        any_output = self.individual_var.get() or self.compiled_var.get()

        ready = (
            self.video_path is not None
            and self.video_duration is not None
            and h1 is not None and h2 is not None and h2 > h1
            and self.events and not self.parse_errors
            and 2 <= length <= 120
            and any_output and ind_ok and cmp_ok
            and (self.worker is None or not self.worker.is_alive())
        )
        self.process_btn.state(["!disabled"] if ready else ["disabled"])

    def _render_preview(self) -> None:
        self.preview.config(state="normal")
        self.preview.delete("1.0", END)
        if not self.events and not self.parse_errors:
            self.preview.insert(END, "(no events parsed)\n", "muted")
        for g in self.groups:
            if len(g.events) == 1:
                e = g.first
                line = (f"{g.indices[0]:02d}     {e.half} {e.clock:>7}  · "
                        f"{e.label:<22} → {fmt_hms(e.offset)}\n")
                tag = "ok"
            else:
                idx = f"{g.indices[0]:02d}-{g.indices[-1]:02d}"
                labels = ", ".join(e.label for e in g.events)
                line = (f"{idx}  {g.first.half} "
                        f"{g.first.clock}-{g.last.clock}  · "
                        f"[{len(g.events)}] {labels} → "
                        f"{fmt_hms(g.first.offset)}–{fmt_hms(g.last.offset)}\n")
                tag = "merge"
            if self.video_duration is not None and (
                g.first.offset < 0 or g.last.offset > self.video_duration
            ):
                tag = "err"
            self.preview.insert(END, line, tag)
        for err in self.parse_errors:
            self.preview.insert(END, f"!! {err}\n", "err")
        self.preview.config(state="disabled")

    # --- processing ---------------------------------------------------------
    def on_process(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not FFMPEG.exists():
            messagebox.showerror("Compr", f"ffmpeg not found at {FFMPEG}")
            return

        length = int(self.len_var.get())
        do_ind = self.individual_var.get()
        do_cmp = self.compiled_var.get()

        ind_dir: Path | None = None
        if do_ind:
            assert self.ind_parent is not None
            name = self.ind_name_var.get().strip()
            ind_dir = self.ind_parent / name
            if ind_dir.exists() and any(ind_dir.iterdir()):
                if not messagebox.askyesno("Compr",
                        f"Folder exists and is not empty:\n{ind_dir}\n\nContinue (existing files may be overwritten)?"):
                    return

        if do_cmp:
            assert self.compiled_path is not None
            if self.compiled_path.exists():
                if not messagebox.askyesno("Compr",
                        f"Overwrite {self.compiled_path.name}?"):
                    return

        self.progress["value"] = 0
        self.progress["maximum"] = max(1, len(self.groups))
        self.status_var.set("Starting…")
        self._set_inputs_enabled(False)

        do_fade = bool(do_cmp and self.fade_var.get())
        keep_audio = bool(self.audio_var.get())
        self.worker = threading.Thread(
            target=self._worker_run,
            args=(self.video_path, list(self.groups), self.video_duration,
                  float(length), do_ind, ind_dir, do_cmp, self.compiled_path,
                  do_fade, keep_audio),
            daemon=True,
        )
        self.worker.start()

    def _worker_run(self, src: Path, groups: list[Group], duration: float,
                    length: float, do_ind: bool, ind_dir: Path | None,
                    do_cmp: bool, cmp_path: Path | None, do_fade: bool,
                    keep_audio: bool) -> None:
        try:
            tmp_dir: Path | None = None
            if do_ind and ind_dir is not None:
                ind_dir.mkdir(parents=True, exist_ok=True)
                clip_dir = ind_dir
            else:
                assert cmp_path is not None
                tmp_dir = cmp_path.parent / f".compr_tmp_{os.getpid()}"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                clip_dir = tmp_dir

            produced: list[Path] = []
            recent: deque[float] = deque(maxlen=5)

            for i, g in enumerate(groups, 1):
                start, seg = group_window(g, length, duration)
                fname = group_filename(g)
                dst = clip_dir / fname

                label_summary = (g.first.label if len(g.events) == 1
                                 else f"[{len(g.events)}] {g.first.label}…")
                self.msg_q.put(("status",
                    f"Clip {i}/{len(groups)}  {g.first.half} {g.first.clock} · {label_summary}"))

                t0 = time.monotonic()
                ok, err = encode_clip(src, start, seg, dst, keep_audio=keep_audio)
                if not ok:
                    self.msg_q.put(("error", f"ffmpeg failed on clip {i}:\n{err}"))
                    return
                recent.append(time.monotonic() - t0)
                produced.append(dst)

                avg = sum(recent) / len(recent)
                remaining = (len(groups) - i) * avg
                self.msg_q.put(("progress", i, len(groups), remaining))

            fade_dir: Path | None = None
            if do_cmp:
                assert cmp_path is not None
                concat_inputs = produced
                if do_fade:
                    fade_dir = cmp_path.parent / f".compr_fade_{os.getpid()}"
                    fade_dir.mkdir(parents=True, exist_ok=True)
                    faded: list[Path] = []
                    for i, p in enumerate(produced, 1):
                        self.msg_q.put(("status",
                            f"Fading {i}/{len(produced)} for compilation…"))
                        pd, _ = probe_duration(p)
                        d = pd if pd is not None else length
                        fp = fade_dir / p.name
                        ok, err = encode_with_fade(p, fp, d, keep_audio=keep_audio)
                        if not ok:
                            self.msg_q.put(("error", f"Fade pass failed on clip {i}:\n{err}"))
                            return
                        faded.append(fp)
                    concat_inputs = faded

                self.msg_q.put(("status", "Concatenating…"))
                ok, err = concat_clips(concat_inputs, cmp_path)
                if not ok:
                    self.msg_q.put(("error", f"Concat failed:\n{err}"))
                    return

            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if fade_dir is not None:
                shutil.rmtree(fade_dir, ignore_errors=True)

            outputs = []
            if do_ind and ind_dir is not None:
                outputs.append(f"{len(produced)} clip(s) in {ind_dir}")
            if do_cmp and cmp_path is not None:
                outputs.append(f"compiled video at {cmp_path}")
            self.msg_q.put(("done", "\n".join(outputs)))
        except Exception as e:  # noqa: BLE001
            self.msg_q.put(("error", f"Unexpected: {e!r}"))

    # --- queue polling ------------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                self._handle_msg(self.msg_q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_msg(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "duration":
            dur = msg[1]
            reason = msg[2] if len(msg) > 2 else ""
            self.video_duration = dur
            if dur is None:
                self.dur_var.set(f"duration unknown — {reason}" if reason else "duration unknown")
            else:
                self.dur_var.set(f"duration {fmt_hms(dur)}")
            self._refresh_state()
        elif kind == "status":
            self.status_var.set(msg[1])
        elif kind == "progress":
            _, i, total, remaining = msg
            self.progress["value"] = i
            self.status_var.set(f"Clip {i}/{total} done — ETA {fmt_hms(remaining)}")
        elif kind == "error":
            self._set_inputs_enabled(True)
            self.status_var.set("Failed.")
            messagebox.showerror("Compr", msg[1])
        elif kind == "done":
            self._set_inputs_enabled(True)
            self.status_var.set("Done.")
            messagebox.showinfo("Compr", f"Finished.\n\n{msg[1]}")

    def _set_inputs_enabled(self, enabled: bool) -> None:
        state = "!disabled" if enabled else "disabled"
        for w in (self.h1_entry, self.h2_entry, self.len_spin,
                  self.ind_name_entry):
            try:
                w.state([state])
            except Exception:
                w.config(state="normal" if enabled else "disabled")
        self.events_text.config(state="normal" if enabled else "disabled")
        if enabled:
            self._refresh_state()
        else:
            self.process_btn.state(["disabled"])


def main() -> None:
    if not FFMPEG.exists():
        print(f"WARNING: ffmpeg not found at {FFMPEG}", file=sys.stderr)
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
