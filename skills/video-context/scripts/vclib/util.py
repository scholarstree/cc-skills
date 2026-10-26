"""Time helpers, status/log files, the per-video lock, and process-tree control."""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
import _thread
from datetime import datetime, timezone
from pathlib import Path

import psutil


class Cancelled(Exception):
    """Raised in the main thread when the run is stopped (signal, --stop, --max-time)."""


class NeedsInput(Exception):
    """The run needs a decision from the user. Exit code 3; the message lists the options."""


class Failed(Exception):
    """A clear, user-facing failure. Exit code 1."""


# ---------------------------------------------------------------- time

def fmt_ts(seconds: float | None) -> str:
    """12.3 -> '00:12', 3725 -> '1:02:05'."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def file_ts(seconds: float) -> str:
    """Timestamp safe for filenames on every OS (no ':')."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"


def parse_ts(text: str) -> float:
    """'1:02:05', '12:34', '754', '754s', '12m34s', '1h2m3s', '1.5' -> seconds."""
    text = text.strip().lower()
    if not text:
        raise ValueError("empty timestamp")
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if m and any(m.groups()):
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp: {text}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def parse_range(text: str) -> tuple[float, float | None]:
    """'10:00-25:00' -> (600, 1500); '10:00-' -> (600, None)."""
    if "-" not in text:
        raise ValueError("range must look like START-END, e.g. 10:00-25:00")
    a, b = text.split("-", 1)
    start = parse_ts(a) if a.strip() else 0.0
    end = parse_ts(b) if b.strip() else None
    if end is not None and end <= start:
        raise ValueError("range end must be after start")
    return start, end


def parse_duration(text: str) -> float:
    """'30m', '90s', '1h', '1h30m', '45' (minutes) -> seconds."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text) * 60
    return parse_ts(text)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- files

def write_json(path: Path, data) -> None:
    """Atomic JSON write (readers never see a half-written file)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Reporter:
    """Progress to stderr + progress.log, machine-readable state to status.json."""

    def __init__(self, outdir: Path | None = None):
        self.outdir = outdir
        self.state: dict = {}

    def attach(self, outdir: Path) -> None:
        self.outdir = outdir

    def log(self, msg: str) -> None:
        line = f"[video-context] {msg}"
        print(line, file=sys.stderr, flush=True)
        if self.outdir:
            with open(self.outdir / "progress.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} {msg}\n")

    def status(self, **fields) -> None:
        self.state.update(fields)
        self.state["updated"] = now_iso()
        self.state["pid"] = os.getpid()
        if self.outdir:
            write_json(self.outdir / "status.json", self.state)


# ---------------------------------------------------------------- lock & processes

LOCK_NAME = ".lock"


def _lock_owner_alive(info: dict) -> bool:
    try:
        p = psutil.Process(int(info["pid"]))
        return abs(p.create_time() - float(info["create_time"])) < 1.0
    except (psutil.Error, KeyError, ValueError, TypeError):
        return False


class Lock:
    """One run per output folder. Stale locks (dead or reused pid) are taken over."""

    def __init__(self, outdir: Path, label: str):
        self.path = outdir / LOCK_NAME
        self.label = label
        self.held = False

    def acquire(self) -> None:
        info = read_json(self.path)
        if info and _lock_owner_alive(info):
            raise Failed(
                f"Another run is already processing this video (pid {info['pid']}, "
                f"started {info.get('started')}). Stop it with --stop, or wait for it to finish."
            )
        me = psutil.Process()
        write_json(self.path, {
            "pid": me.pid, "create_time": me.create_time(),
            "started": now_iso(), "input": self.label,
        })
        self.held = True

    def release(self) -> None:
        if self.held:
            info = read_json(self.path)
            if info and info.get("pid") == os.getpid():
                self.path.unlink(missing_ok=True)
            self.held = False


def kill_children(timeout: float = 3.0) -> None:
    """Terminate every process this run started (yt-dlp/ffmpeg/STT), then force-kill leftovers."""
    try:
        kids = psutil.Process().children(recursive=True)
    except psutil.Error:
        return
    for k in kids:
        try:
            k.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(kids, timeout=timeout)
    for k in alive:
        try:
            k.kill()
        except psutil.Error:
            pass


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        raise Cancelled(f"received signal {signum}")

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, handler)


def start_deadline(seconds: float, reporter: Reporter) -> None:
    """--max-time: stop the run after `seconds`. Killing children unblocks any wait in the main thread."""

    def fire():
        reporter.log(f"--max-time reached ({fmt_ts(seconds)}), stopping")
        _thread.interrupt_main()  # KeyboardInterrupt in the main thread
        kill_children(timeout=1.0)

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()


def cleanup_temp(outdir: Path) -> None:
    """Remove half-finished downloads and temp audio/video. Partial results are kept."""
    tmp = outdir / "tmp"
    if tmp.exists():
        for p in sorted(tmp.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() or p.is_symlink() else p.rmdir()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass
    for p in outdir.glob("*.part"):
        p.unlink(missing_ok=True)
    for p in outdir.glob("*.ytdl"):
        p.unlink(missing_ok=True)


def stop_run(outdir: Path, reporter: Reporter, grace: float = 10.0) -> bool:
    """--stop: end the run that holds this folder's lock, then clean up after it.
    Returns True if a live run was found."""
    lock = outdir / LOCK_NAME
    info = read_json(lock)
    found = False
    if info and _lock_owner_alive(info):
        found = True
        main = psutil.Process(int(info["pid"]))
        try:
            procs = [main] + main.children(recursive=True)
        except psutil.Error:
            procs = [main]
        # POSIX: SIGTERM lets the run clean up itself. Windows: terminate() is a hard kill.
        try:
            main.terminate()
        except psutil.Error:
            pass
        _, alive = psutil.wait_procs([main], timeout=grace)
        for p in procs:
            try:
                if p.is_running():
                    p.kill()
            except psutil.Error:
                pass
    lock.unlink(missing_ok=True)
    cleanup_temp(outdir)
    status = read_json(outdir / "status.json", {}) or {}
    if status.get("state") in ("running", "waiting", None) and (outdir / "status.json").exists():
        status.update(state="cancelled", message="stopped with --stop", updated=now_iso())
        write_json(outdir / "status.json", status)
    return found
