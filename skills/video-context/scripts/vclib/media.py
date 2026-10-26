"""ffmpeg/ffprobe helpers: local-file probing, embedded/sidecar subtitles, audio for STT,
scene detection and frame extraction."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .util import Failed, file_ts, fmt_ts

BITMAP_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"}
SUB_EXTS = (".vtt", ".srt")


@lru_cache(maxsize=None)
def ffmpeg() -> str:
    """System ffmpeg if present, else the static build bundled with imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise Failed(f"ffmpeg not found and the bundled copy failed to load: {e}")


def ffprobe() -> str | None:
    return shutil.which("ffprobe")


def run(cmd: list[str], *, text: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=text, encoding="utf-8" if text else None,
                       errors="replace" if text else None)
    if check and p.returncode != 0:
        err = (p.stderr or "")[-600:] if text else ""
        raise Failed(f"{Path(cmd[0]).name} failed ({p.returncode}): {err.strip()}")
    return p


# ---------------------------------------------------------------- local files

def probe(path: Path) -> dict:
    """Duration, title, resolution, chapters, subtitle streams of a local file."""
    if ffprobe():
        p = run([ffprobe(), "-v", "error", "-print_format", "json", "-show_format",
                 "-show_streams", "-show_chapters", str(path)])
        data = json.loads(p.stdout or "{}")
        fmt = data.get("format", {})
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not (s.get("disposition") or {}).get("attached_pic")), None)
        subs = [{"index": i, "codec": s.get("codec_name"),
                 "lang": (s.get("tags") or {}).get("language")}
                for i, s in enumerate(x for x in streams if x.get("codec_type") == "subtitle")]
        chapters = [{"start_time": float(c.get("start_time", 0)), "end_time": float(c.get("end_time", 0)),
                     "title": (c.get("tags") or {}).get("title")} for c in data.get("chapters", [])]
        return {
            "duration": float(fmt["duration"]) if fmt.get("duration") else None,
            "title": tags.get("title"),
            "artist": tags.get("artist") or tags.get("album_artist"),
            "date": tags.get("date") or tags.get("creation_time"),
            "comment": tags.get("comment") or tags.get("description"),
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "has_video": video is not None,
            "subtitle_streams": subs,
            "chapters": chapters,
        }
    return _probe_with_ffmpeg(path)


def _probe_with_ffmpeg(path: Path) -> dict:
    """Fallback when ffprobe isn't installed: parse `ffmpeg -i` output."""
    err = run([ffmpeg(), "-hide_banner", "-i", str(path)], check=False).stderr or ""
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration = int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3]) if dur else None
    vid = re.search(r"Stream #\S+.*?: Video: (?!.*attached pic).*?(\d{2,5})x(\d{2,5})", err)
    title = re.search(r"^\s{4}title\s*:\s*(.+)$", err, re.M)
    subs = []
    for i, m in enumerate(re.finditer(r"Stream #\d+:\d+(?:\((\w+)\))?.*?: Subtitle: (\w+)", err)):
        subs.append({"index": i, "codec": m[2], "lang": m[1]})
    chapters = []
    for m in re.finditer(r"Chapter #\S+ start (\d+\.\d+), end (\d+\.\d+)\s*\n\s*Metadata:\s*\n\s*title\s*:\s*(.*)", err):
        chapters.append({"start_time": float(m[1]), "end_time": float(m[2]), "title": m[3].strip()})
    return {"duration": duration, "title": title[1].strip() if title else None, "artist": None,
            "date": None, "comment": None, "width": int(vid[1]) if vid else None,
            "height": int(vid[2]) if vid else None, "has_video": vid is not None,
            "subtitle_streams": subs, "chapters": chapters}


def sidecar_subs(path: Path, want: str | None) -> Path | None:
    """video.srt / video.en.vtt / ... next to the file. Prefers the requested language."""
    cands = [p for p in path.parent.iterdir()
             if p.suffix.lower() in SUB_EXTS and (p.stem == path.stem or p.stem.startswith(path.stem + "."))]
    if not cands:
        return None
    if want:
        for p in cands:
            if p.stem.lower().split(".")[-1].split("-")[0] == want.lower().split("-")[0]:
                return p
    for p in cands:
        if p.stem == path.stem:
            return p
    for p in cands:
        if p.stem.lower().split(".")[-1] in ("en", "eng", "en-us", "en-gb"):
            return p
    return sorted(cands)[0]


def embedded_subs(path: Path, streams: list[dict], want: str | None) -> tuple[str, str] | None:
    """Extract the best text subtitle stream as WebVTT. Returns (vtt_text, lang)."""
    text_streams = [s for s in streams if s.get("codec") not in BITMAP_SUBS]
    if not text_streams:
        return None

    def lang_ok(s, lang):
        return lang and (s.get("lang") or "")[:2].lower() == lang[:2].lower()

    ordered = ([s for s in text_streams if lang_ok(s, want)] +
               [s for s in text_streams if lang_ok(s, "en")] + text_streams)
    for s in ordered:
        p = run([ffmpeg(), "-v", "error", "-i", str(path), "-map", f"0:s:{s['index']}",
                 "-f", "webvtt", "-"], check=False)
        if p.returncode == 0 and "-->" in (p.stdout or ""):
            return p.stdout, s.get("lang") or "und"
    return None


# ---------------------------------------------------------------- audio for STT

def to_wav(src: Path, dst: Path, start: float = 0.0, end: float | None = None) -> None:
    """16 kHz mono WAV of [start, end) for faster-whisper."""
    cmd = [ffmpeg(), "-v", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]  # a duration: unambiguous next to an input -ss
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dst)]
    run(cmd)


# ---------------------------------------------------------------- frames

def default_frame_count(duration: float) -> int:
    """~6 for a 30 s reel, rising linearly to 25 at 30 min."""
    frac = min(1.0, max(0.0, (duration - 30) / (1800 - 30)))
    return round(6 + 19 * frac)


def scene_changes(src: Path, threshold: float, start: float, end: float | None) -> list[float]:
    cmd = [ffmpeg(), "-hide_banner", "-nostats"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]  # a duration: unambiguous next to an input -ss
    cmd += ["-i", str(src), "-an", "-sn", "-vf",
            f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    err = run(cmd, check=False).stderr or ""
    return [start + float(m) for m in re.findall(r"pts_time:\s*([\d.]+)", err)]


def choose_times(duration: float, start: float, end: float | None, chapters: list[dict],
                 scenes: list[float], n: int) -> list[float]:
    """Chapter starts first, then scene changes, then even fill; spread out to n frames."""
    end = duration if end is None else min(end, duration)
    span = max(1.0, end - start)
    min_gap = max(1.0, span / (n * 3))
    inside = lambda t: start <= t < end - 0.5

    chosen: list[float] = []

    def add(t):
        if inside(t) and all(abs(t - c) >= min_gap for c in chosen):
            chosen.append(t)

    # Chapter starts (+1.5 s to land past the transition)
    for ch in chapters:
        if len(chosen) >= n:
            break
        add((ch.get("start_time") or 0) + 1.5)

    # Scene changes, spread evenly if there are more than we need
    remaining = n - len(chosen)
    scenes = sorted(t + 0.3 for t in scenes if inside(t + 0.3))
    if remaining > 0 and scenes:
        if len(scenes) > remaining:
            step = len(scenes) / remaining
            scenes = [scenes[int(i * step)] for i in range(remaining)]
        for t in scenes:
            add(t)

    # Fill the largest gaps
    while len(chosen) < n:
        pts = sorted([start] + chosen + [end])
        gap, a = max((b - a, a) for a, b in zip(pts, pts[1:]))
        if gap < 2 * min_gap:
            break
        chosen.append(a + gap / 2)
    if not chosen:
        chosen = [start + span * 0.1]
    return sorted(chosen)[:n] if len(chosen) > n else sorted(chosen)


def extract_frame(src: Path, t: float, dst: Path, height: int) -> bool:
    p = run([ffmpeg(), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
             "-vf", f"scale=-2:'min({height},ih)'", "-q:v", "3", str(dst)], check=False)
    return p.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def dhash(img: Path, size: int = 16) -> int | None:
    """Difference hash (size*size bits) via ffmpeg, no Pillow needed. 16x16 is fine-grained
    enough to tell apart two screens of code, coarse enough to merge a static talking head."""
    p = run([ffmpeg(), "-v", "error", "-i", str(img), "-vf", f"scale={size + 1}:{size},format=gray",
             "-f", "rawvideo", "-"], text=False, check=False)
    data = p.stdout
    w = size + 1
    if p.returncode != 0 or len(data) < w * size:
        return None
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (data[row * w + col] > data[row * w + col + 1])
    return bits


def frames(src: Path, outdir: Path, times: list[float], dedupe: bool, reporter, height: int) -> list[dict]:
    """Extract frames at `times`. With dedupe, a frame nearly identical to the previous kept
    one (<= ~3% of hash bits differ) is dropped."""
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("f_*.jpg"):
        old.unlink()
    kept: list[dict] = []
    last: int | None = None
    for i, t in enumerate(times):
        dst = outdir / f"f_{file_ts(t)}.jpg"
        if not extract_frame(src, t, dst, height):
            continue
        if dedupe:
            h = dhash(dst)
            if h is not None and last is not None and bin(h ^ last).count("1") <= 8:
                dst.unlink()
                continue
            last = h if h is not None else last
        kept.append({"t": round(t, 2), "ts": fmt_ts(t), "path": str(dst)})
        reporter.status(stage="frames", progress=round((i + 1) / len(times), 2))
    return kept


def cut(src: Path, dst: Path, start: float, end: float | None) -> None:
    """Copy [start, end) of a file without re-encoding (cuts land on keyframes)."""
    cmd = [ffmpeg(), "-v", "error", "-y", "-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]  # a duration: unambiguous next to an input -ss
    cmd += ["-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)]
    run(cmd)
