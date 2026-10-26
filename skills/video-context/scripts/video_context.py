# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yt-dlp[default,deno,curl-cffi]",
#   "imageio-ffmpeg",
#   "psutil",
#   "secretstorage; sys_platform == 'linux'",  # yt-dlp needs it to decrypt Chromium-family cookies on Linux
# ]
# ///
"""video-context: put a video (URL or local file) into an LLM's context.

Metadata, description, chapters and a transcript (creator subs > auto-captions >
embedded/sidecar subs > speech-to-text) go into context.md, which is printed.
Optional: frames, saved video/audio, top comments.

Exit codes: 0 ok, 1 failed, 3 needs a user decision (message says what), 130 stopped.
Run with uv:  uv run video_context.py <url-or-file> [options]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vclib import captions, cookies, media  # noqa: E402
from vclib.util import (  # noqa: E402
    Cancelled, Failed, Lock, NeedsInput, Reporter, cleanup_temp, fmt_ts, install_signal_handlers,
    kill_children, now_iso, parse_duration, parse_range, parse_ts, read_json, start_deadline,
    stop_run, write_json,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASE = Path.home() / "Downloads" / "video-context"
PRINT_LIMIT_TOKENS = 20_000
STT_ASK_SECONDS = 20 * 60
GPU_MIN_FREE_MB = 1536
FRAME_HEIGHT = 720
STT_SPEED = {"cuda": 40.0, "cpu": 6.0}  # x realtime, rough; for the up-front estimate only

BLOCKED = re.compile(
    r"log ?in|sign ?in|cookies|not a bot|rate.?limit|429|too many requests|age.?restrict|confirm your age|"
    r"members.?only|join this channel|authenticat|inappropriate|requested content is not available|"
    r"private", re.I)
BROKEN = re.compile(
    r"unable to extract|nsig|signature extraction|failed to parse json|unable to download (api|webpage)|"
    r"no video formats found|precondition check failed", re.I)
UNAVAILABLE = re.compile(r"video unavailable|has been removed|not available in your country|geo.?restrict|"
                         r"does not exist|404|this video is private|private video", re.I)

rep = Reporter()


class UpgradeRetry(Exception):
    """yt-dlp looks broken for this site: upgrade it and run again (once)."""


# ============================================================ input / folders

def is_url(text: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I))


def normalize_url(url: str) -> str:
    """Single video only: drop YouTube playlist params from watch URLs."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host.endswith("youtube.com") and u.path == "/watch":
        qs = parse_qs(u.query)
        keep = {k: v for k, v in qs.items() if k in ("v", "t", "start")}
        return urlunparse(u._replace(query=urlencode(keep, doseq=True)))
    return url


def url_timestamp(url: str) -> float | None:
    u = urlparse(url)
    qs = parse_qs(u.query)
    frag = parse_qs(u.fragment)
    for key in ("t", "start", "time_continue"):
        val = (qs.get(key) or frag.get(key) or [None])[0]
        if val:
            try:
                return parse_ts(val)
            except ValueError:
                pass
    return None


def safe(text: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:n] or "x"


def static_id(url: str) -> tuple[str, str] | None:
    """(site, id) from the URL alone, without network. None if unknown."""
    from yt_dlp.extractor import gen_extractor_classes
    for ie in gen_extractor_classes():
        key = ie.ie_key()
        if key == "Generic":
            continue
        try:
            if ie.suitable(url):
                vid = ie.get_temp_id(url)
                return (key.lower(), vid) if vid else None
        except Exception:
            continue
    return None


def local_folder(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:8]
    return f"local-{safe(path.stem, 40)}-{h}"


# ============================================================ yt-dlp

class _YdlLogger:
    def __init__(self):
        self.lines: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.lines.append(msg)

    def error(self, msg):
        self.lines.append(msg)


def _progress_hook(d):
    if d.get("status") == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        if total:
            rep.status(progress=round(done / total, 3), eta_s=d.get("eta"))


def ydl(job, cookie_spec: str | None, **extra):
    from yt_dlp import YoutubeDL
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "logger": _YdlLogger(), "progress_hooks": [_progress_hook],
        "ffmpeg_location": media.ffmpeg(), "socket_timeout": 30, "retries": 3,
    }
    if cookie_spec:
        opts["cookiesfrombrowser"] = cookies.parse_spec(cookie_spec)
    opts.update(extra)
    return YoutubeDL(opts)


def explain(msg: str) -> str:
    msg = re.sub(r"^ERROR:\s*", "", msg.strip())
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)  # "[instagram] C1a2B3: " prefix
    if re.search(r"unsupported url", msg, re.I):
        return f"yt-dlp doesn't support this URL. ({msg[:200]})"
    if re.search(r"rate.?limit|429|too many requests", msg, re.I):
        return f"The site is rate-limiting requests. Wait a few minutes and try again. ({msg[:200]})"
    if UNAVAILABLE.search(msg):
        return f"The video isn't available (private, removed or region-blocked). ({msg[:200]})"
    return msg[:500]


def with_login(job, fn, what: str):
    """Run fn(cookie_spec). If the site blocks it, find a logged-in browser and retry once."""
    from yt_dlp.utils import DownloadError
    try:
        return fn(job["cookies"])
    except DownloadError as e:
        msg = str(e)
        if job["cookies"] or job["args"].no_cookies or not BLOCKED.search(msg):
            if BROKEN.search(msg) and not job["args"]._upgraded:
                raise UpgradeRetry(msg)
            raise Failed(f"{what} failed: {explain(msg)}")
        if UNAVAILABLE.search(msg) and not re.search(r"log ?in|sign ?in|cookies|rate", msg, re.I):
            raise Failed(f"{what} failed: {explain(msg)}")
        job["cookies"] = choose_cookies(job, msg)
        try:
            return fn(job["cookies"])
        except DownloadError as e2:
            raise Failed(f"{what} failed even with {cookies.describe(job['cookies'])} cookies: {explain(str(e2))}")


def choose_cookies(job, msg: str) -> str:
    dom = cookies.site_domain(job["url"])
    rep.log(f"{dom} blocked the request without a login ({explain(msg)[:120]}). "
            f"Checking installed browsers for a {dom} login...")
    rep.status(stage="checking browser logins")
    res = cookies.scan(dom)
    hints = []
    if sys.platform == "win32" and any(u.split(" ")[0] in ("chrome", "edge", "brave") for u in res["unreadable"]):
        hints.append("Chrome/Edge/Brave cookies on Windows are often unreadable: close the browser fully, or log in with Firefox.")
    if sys.platform.startswith("linux") and res["unreadable"]:
        hints.append("On Linux, Chrome/Edge/Brave cookies are decrypted with the desktop keyring, which doesn't always work. "
                     "Firefox cookies are always readable.")
    if sys.platform == "darwin" and any(u.startswith("safari") for u in res["unreadable"]):
        hints.append("Safari cookies need Full Disk Access for the terminal app (System Settings > Privacy & Security).")
    unread = ("\nCould not read: " + "; ".join(res["unreadable"])) if res["unreadable"] else ""
    hint = ("\n" + "\n".join(hints)) if hints else ""
    if not res["logged_in"]:
        raise Failed(
            f"{dom} needs a login, and no {'readable ' if res['unreadable'] else ''}browser is logged in to {dom}.\n"
            f"Checked: {', '.join(res['checked']) or 'no browsers found'}.{unread}{hint}\n"
            f"Log in to {dom} in a browser and run again, or download the video yourself and pass the file.")
    if len(res["logged_in"]) > 1:
        opts = "\n".join(f'  --cookies-from-browser "{cookies.spec(p)}"   # {p["label"]}' for p in res["logged_in"])
        raise NeedsInput(
            f"{dom} needs a login, and these browsers are logged in to {dom}. "
            f"Ask the user which one to use, then run again with:\n{opts}")
    p = res["logged_in"][0]
    rep.log(f"Using {p['label']} cookies for {dom} (the only browser logged in to {dom}).")
    return cookies.spec(p)


META_KEYS = ("id", "title", "fulltitle", "uploader", "uploader_id", "channel", "channel_url", "uploader_url",
             "upload_date", "timestamp", "duration", "view_count", "like_count", "comment_count", "description",
             "chapters", "tags", "categories", "webpage_url", "extractor_key", "language", "is_live",
             "was_live", "live_status", "age_limit", "width", "height")


def fetch_info(job) -> dict:
    a = job["args"]
    extra = {"skip_download": True, "extract_flat": "in_playlist"}
    if a.comments:
        extra["getcomments"] = True
        n = str(a.comments)
        extra["extractor_args"] = {"youtube": {"max_comments": [n, n, "0", "0"], "comment_sort": ["top"]}}

    def go(spec):
        with ydl(job, spec, **extra) as y:
            return y.sanitize_info(y.extract_info(job["url"], download=False))

    rep.status(stage="fetching metadata")
    info = with_login(job, go, "Fetching the video page")
    if info.get("_type") in ("playlist", "multi_video"):
        entries = info.get("entries") or []
        if len(entries) == 1 and entries[0].get("url"):
            job["url"] = entries[0]["url"]
            return fetch_info(job)
        raise Failed(f"This URL is a playlist / multi-video post ({len(entries)} items). "
                     "video-context handles a single video only; pass one video's URL.")
    if info.get("is_live"):
        raise Failed("This is a live stream that's still running. Try again after it ends.")
    meta = {k: info.get(k) for k in META_KEYS if info.get(k) not in (None, [], "")}
    meta["_subtitles"] = info.get("subtitles") or {}
    meta["_automatic_captions"] = info.get("automatic_captions") or {}
    if a.comments and info.get("comments"):
        top = [c for c in info["comments"] if c.get("parent") in (None, "root")]
        top.sort(key=lambda c: c.get("like_count") or 0, reverse=True)
        meta["comments"] = [{"author": c.get("author"), "likes": c.get("like_count"), "text": c.get("text")}
                            for c in top[: a.comments]]
    return meta


def download(job, fmt: str, dest_stem: Path, what: str, **extra) -> Path:
    """Download the whole file. (yt-dlp's section downloads stream through ffmpeg at
    roughly real-time speed; a full native download and a local cut is far faster.)"""
    opts = {"format": fmt, "outtmpl": str(dest_stem) + ".%(ext)s", "skip_download": False,
            "overwrites": True, "continuedl": False}
    opts.update(extra)
    rep.log(what[0].upper() + what[1:] + "...")

    def go(spec):
        with ydl(job, spec, **opts) as y:
            info = y.extract_info(job["url"], download=True)
            reqs = info.get("requested_downloads") or []
            path = reqs[0].get("filepath") if reqs else None
            return Path(path) if path else None

    rep.status(stage=what, progress=0)
    path = with_login(job, go, what.capitalize())
    if not path or not path.exists():
        cands = sorted(dest_stem.parent.glob(dest_stem.name + ".*"), key=lambda p: p.stat().st_mtime)
        cands = [c for c in cands if c.suffix not in (".part", ".ytdl")]
        if not cands:
            raise Failed(f"{what.capitalize()} finished but no file was written.")
        path = cands[-1]
    return path


def sponsor_segments(video_id: str) -> list[dict]:
    """SponsorBlock (no account needed). Failures are silent."""
    import urllib.request
    cats = ["sponsor", "selfpromo", "interaction"]
    labels = {"sponsor": "SPONSOR", "selfpromo": "SELF-PROMO", "interaction": "SUBSCRIBE-REMINDER"}
    url = ("https://sponsor.ajay.app/api/skipSegments?" +
           urlencode({"videoID": video_id, "categories": json.dumps(cats)}))
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    return [{"start": s["segment"][0], "end": s["segment"][1], "label": labels.get(s.get("category"), "SPONSOR")}
            for s in data if s.get("segment")]


# ============================================================ transcript

def transcript_from_captions(job) -> dict | None:
    a, meta = job["args"], job["meta"]
    if job["kind"] == "local":
        path = job["path"]
        side = media.sidecar_subs(path, a.lang)
        if side:
            lines = captions.cues_to_lines(captions.parse_cues(side.read_text(encoding="utf-8", errors="replace")))
            if lines:
                return {"source": f"sidecar subtitles ({side.name})", "lang": None, "lines": lines}
        emb = media.embedded_subs(path, meta.get("subtitle_streams") or [], a.lang)
        if emb:
            lines = captions.cues_to_lines(captions.parse_cues(emb[0]))
            if lines:
                return {"source": f"embedded subtitles ({emb[1]})", "lang": emb[1], "lines": lines}
        return None

    track = captions.pick_track({"subtitles": meta.get("_subtitles"),
                                 "automatic_captions": meta.get("_automatic_captions"),
                                 "language": meta.get("language")}, a.lang)
    if not track:
        return None
    kind, key, formats = track
    fmt = captions.pick_format(formats)
    if not fmt:
        return None
    rep.status(stage="downloading captions")

    def go(spec):
        with ydl(job, spec) as y:
            return y.urlopen(fmt["url"]).read().decode("utf-8", errors="replace")

    text = with_login(job, go, "Downloading captions")
    lines = captions.cues_to_lines(captions.parse_cues(text))
    if not lines:
        return None
    lang = key.removesuffix("-orig")
    site = meta.get("extractor_key") or job["site"]
    return {"source": f"{site} {kind} ({lang})", "lang": lang, "lines": lines}


def pick_device(forced: str | None) -> tuple[str, int, str]:
    if forced:
        return forced, 0, "forced with --device"
    if sys.platform == "darwin":
        return "cpu", 0, "macOS (faster-whisper has no Apple GPU support)"
    smi = shutil.which("nvidia-smi")
    if not smi:
        return "cpu", 0, "no NVIDIA GPU found"
    try:
        out = subprocess.run([smi, "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15).stdout
        gpus = [(int(i), int(f)) for i, f in (l.split(",") for l in out.strip().splitlines() if "," in l)]
    except Exception as e:
        return "cpu", 0, f"nvidia-smi failed ({e})"
    if not gpus:
        return "cpu", 0, "no NVIDIA GPU found"
    idx, free = max(gpus, key=lambda g: g[1])
    if free < GPU_MIN_FREE_MB:
        return "cpu", 0, f"GPU {idx} has only {free} MB free (others are using it)"
    return "cuda", idx, f"GPU {idx}, {free} MB free"


def read_stt(jsonl: Path) -> tuple[list[dict], dict | None]:
    segs, done = [], None
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # half-written line from a killed run
            if obj.get("done"):
                done = obj
            elif "start" in obj:
                segs.append(obj)
    return segs, done


def transcript_from_stt(job) -> dict | None:
    a, meta, outdir = job["args"], job["meta"], job["outdir"]
    duration = meta.get("duration")
    start, end = a.range or (0.0, None)
    end_eff = min(end, duration) if (end and duration) else (end or duration)
    jsonl = outdir / f"stt_{int(start)}_{int(end_eff or 0)}.jsonl"
    segs, done = read_stt(jsonl)
    if not done:
        resume = max(start, segs[-1]["end"] if segs else start)
        remaining = (end_eff - resume) if end_eff else None
        device, idx, why = pick_device(a.device)
        if remaining is not None and remaining < 1:
            done = {"language": None, "device": device, "model": "small int8"}
        else:
            if remaining and remaining > STT_ASK_SECONDS and not a.yes:
                est = remaining / STT_SPEED[device]
                raise NeedsInput(
                    f"No captions, so this needs speech-to-text on {fmt_ts(remaining)} of audio. "
                    f"Estimated time on {device.upper()} ({why}): about {max(1, round(est / 60))} min "
                    f"(+ a one-time ~500 MB model download). Ask the user, then run again with one of:\n"
                    f"  --yes                      # transcribe it all\n"
                    f"  --range 10:00-25:00        # only a section (any START-END)\n"
                    f"  --no-stt                   # skip speech-to-text")
            if segs:
                rep.log(f"Resuming speech-to-text from {fmt_ts(resume)}")
            done = run_stt(job, resume, end_eff, jsonl, device, idx, why)
            segs, done = read_stt(jsonl)
            if done is None:
                raise Failed(f"Speech-to-text stopped early. See {outdir / 'stt.log'}")
    if not segs:
        return {"source": f"speech-to-text (faster-whisper small int8, {done.get('device')}) — no speech detected",
                "lang": done.get("language"), "lines": [], "no_speech": True}
    return {"source": f"speech-to-text (faster-whisper small int8, {done.get('device')}, "
                      f"language {done.get('language')})",
            "lang": done.get("language"), "lines": [(s["start"], s["text"]) for s in segs]}


def run_stt(job, resume: float, end: float | None, jsonl: Path, device: str, idx: int, why: str):
    a, outdir = job["args"], job["outdir"]
    tmp = outdir / "tmp"
    tmp.mkdir(exist_ok=True)
    wav = tmp / "audio16k.wav"
    if job["kind"] == "local":
        media.to_wav(job["path"], wav, resume, end)
    else:
        src = download(job, "ba/b", tmp / "audio", "downloading audio")
        media.to_wav(src, wav, resume, end)
        src.unlink(missing_ok=True)
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise Failed("uv not found on PATH (needed to run speech-to-text).")
    cmd = [uv, "run", "--quiet"]
    if device == "cuda":
        cmd += ["--with", "nvidia-cublas-cu12", "--with", "nvidia-cudnn-cu12>=9,<10"]
    cmd += [str(SCRIPT_DIR / "stt.py"), str(wav), "--out", str(jsonl), "--offset", f"{resume:.3f}",
            "--device", device, "--device-index", str(idx)]
    if a.device == "cuda":
        cmd.append("--strict-device")
    if a.lang:
        cmd += ["--lang", a.lang.split("-")[0]]
    rep.log(f"Speech-to-text on {device.upper()} ({why}). First run installs faster-whisper "
            f"and downloads the model (~500 MB) — this can take a few minutes.")
    rep.status(stage="speech-to-text (setting up)", progress=0)
    total = (end - resume) if end else None
    t0 = None
    with open(outdir / "stt.log", "a", encoding="utf-8") as log:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                             errors="replace")
        for line in p.stdout:
            line = line.strip()
            if line.startswith("PROGRESS "):
                pos = float(line.split()[1]) - resume
                t0 = t0 or time.time()
                if total:
                    frac = min(1.0, pos / total)
                    rate = pos / max(1e-6, time.time() - t0)
                    eta = (total - pos) / rate if rate > 0 else None
                    rep.status(stage="speech-to-text", progress=round(frac, 3),
                               eta_s=round(eta) if eta else None)
            elif line.startswith(("DEVICE", "LANG", "STAGE")):
                rep.log("stt: " + line)
                if line.startswith("DEVICE"):
                    rep.status(stage="speech-to-text", device=line.split()[1])
        rc = p.wait()
    if rc != 0:
        tail = (outdir / "stt.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
        raise Failed("Speech-to-text failed:\n" + "\n".join(tail))
    wav.unlink(missing_ok=True)


# ============================================================ frames & media files

def do_frames(job) -> dict | None:
    a, meta, outdir = job["args"], job["meta"], job["outdir"]
    params = json.loads(json.dumps(  # same shape as after a round-trip through info.json
        {"at": a.at, "every": a.every, "max": a.max_frames, "scene": a.scene, "range": a.range}))
    prev = job["state"].get("frames")
    if prev and prev.get("params") == params and all(Path(f["path"]).exists() for f in prev["items"]):
        return prev
    duration = meta.get("duration")
    if not duration:
        raise Failed("Can't take frames: the video's duration is unknown.")
    start, end = a.range or (0.0, None)
    end = min(end, duration) if end else duration

    tmp_video = None
    if job["kind"] == "local":
        if not meta.get("has_video"):
            return {"params": params, "items": [], "note": "audio-only file, no frames"}
        src = job["path"]
    elif job.get("saved_video"):
        src = Path(job["saved_video"])
    else:
        (outdir / "tmp").mkdir(exist_ok=True)
        src = tmp_video = download(job, f"bv*[height<={FRAME_HEIGHT}]/b[height<={FRAME_HEIGHT}]/wv*/w",
                                   outdir / "tmp" / "frames_src", f"downloading {FRAME_HEIGHT}p video for frames")

    if a.at:
        times = [parse_ts(x) for x in a.at.split(",") if x.strip()]
        times = [t for t in times if 0 <= t <= duration]
        dedupe = False
    elif a.every:
        cap = a.max_frames or 200
        times = [start + i * a.every for i in range(int((end - start) / a.every) + 1)][:cap]
        dedupe = True
    else:
        n = a.max_frames or media.default_frame_count(end - start)
        rep.status(stage="finding scene changes", progress=None)
        scenes = media.scene_changes(src, a.scene, start, end if a.range else None)
        times = media.choose_times(duration, start, end, meta.get("chapters") or [], scenes, n)
        dedupe = True

    rep.status(stage="frames", progress=0)
    items = media.frames(src, outdir / "frames", times, dedupe, rep, FRAME_HEIGHT)
    if tmp_video and tmp_video.exists():
        tmp_video.unlink()
    return {"params": params, "items": items}


def save_media(job) -> dict:
    """--video / --audio. With --range, the full file is downloaded and cut locally."""
    a, outdir = job["args"], job["outdir"]
    saved = {}
    if job["kind"] == "local":
        if a.video or a.audio:
            rep.log("--video/--audio ignored for local files (you already have the file).")
        return saved
    tmp = outdir / "tmp"
    tmp.mkdir(exist_ok=True)

    def place(src: Path, name: str) -> Path:
        dst = outdir / f"{name}{src.suffix}"
        if a.range:
            media.cut(src, dst, *a.range)
            src.unlink(missing_ok=True)
        else:
            os.replace(src, dst)
        return dst

    if a.video:
        p = place(download(job, "bv*+ba/b", tmp / "video", "downloading video", merge_output_format="mp4"),
                  "video")
        saved["video"] = str(p)
        if not a.range:
            job["saved_video"] = str(p)
    if a.audio:
        p = download(job, "ba/b", tmp / "audio", "downloading audio",
                     postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}])
        mp3 = p.with_suffix(".mp3")
        saved["audio"] = str(place(mp3 if mp3.exists() else p, "audio"))
    return saved


# ============================================================ context.md

def fence(text: str) -> str:
    return (text or "").replace("untrusted-content", "untrusted_content")


def build_context(job) -> tuple[str, int]:
    """Write context.md. Returns (text, line number where the transcript starts)."""
    meta, st, a = job["meta"], job["state"], job["args"]
    L: list[str] = []
    title = meta.get("title") or (job["path"].name if job["kind"] == "local" else meta.get("id"))
    L.append(f"# {title}")
    L.append("")
    if job["kind"] == "local":
        L.append(f"- **File:** {job['path']}")
    else:
        L.append(f"- **URL:** {meta.get('webpage_url') or job['url']}")
        L.append(f"- **Site:** {meta.get('extractor_key') or job['site']}")
    who = meta.get("channel") or meta.get("uploader") or meta.get("artist")
    if who:
        L.append(f"- **By:** {who}" + (f" ({meta['channel_url']})" if meta.get("channel_url") else ""))
    if meta.get("upload_date"):
        d = meta["upload_date"]
        L.append(f"- **Uploaded:** {d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else f"- **Uploaded:** {d}")
    elif meta.get("date"):
        L.append(f"- **Date:** {meta['date']}")
    if meta.get("duration"):
        L.append(f"- **Duration:** {fmt_ts(meta['duration'])}")
    stats = [f"{meta[k]:,} {n}" for k, n in (("view_count", "views"), ("like_count", "likes"),
                                             ("comment_count", "comments")) if isinstance(meta.get(k), int)]
    if stats:
        L.append(f"- **Stats:** {', '.join(stats)}")
    if meta.get("width") and meta.get("height"):
        L.append(f"- **Resolution:** {meta['width']}x{meta['height']}")
    if job.get("linked_at") is not None:
        L.append(f"- **User linked to:** {fmt_ts(job['linked_at'])} (focus around this point)")
    tr = st.get("transcript")
    L.append(f"- **Transcript:** {tr['source'] if tr else 'none available'}")
    if a.range:
        L.append(f"- **Range:** {fmt_ts(a.range[0])}–{fmt_ts(a.range[1]) if a.range[1] else 'end'} "
                 f"(speech-to-text and frames only)")
    if st.get("cookies"):
        L.append(f"- **Login cookies used:** {cookies.describe(st['cookies'])} for {cookies.site_domain(job['url'])}")
    for k, v in (st.get("saved") or {}).items():
        if k != "video_full":
            L.append(f"- **Saved {k}:** {v}")
    L.append(f"- **Folder:** {job['outdir']}")
    if meta.get("tags"):
        L.append(f"- **Tags:** {', '.join(meta['tags'][:20])}")
    L.append("")

    if meta.get("chapters"):
        L.append("## Chapters")
        L.append("")
        for c in meta["chapters"]:
            L.append(f"- [{fmt_ts(c.get('start_time'))}] {fence(c.get('title') or '')}")
        L.append("")

    desc = meta.get("description") or meta.get("comment")
    if desc:
        L.append("## Description")
        L.append("")
        L.append('<untrusted-content source="description">')
        L.append(fence(desc.strip()))
        L.append("</untrusted-content>")
        L.append("")

    if meta.get("comments"):
        L.append(f"## Top comments ({len(meta['comments'])})")
        L.append("")
        L.append('<untrusted-content source="comments">')
        for c in meta["comments"]:
            likes = f" ({c['likes']:,} likes)" if isinstance(c.get("likes"), int) else ""
            text = fence((c.get("text") or "").strip()).replace("\n", "\n  ")
            L.append(f"- **{fence(c.get('author') or '?')}**{likes}: {text}")
        L.append("</untrusted-content>")
        L.append("")

    fr = st.get("frames")
    if fr:
        L.append(f"## Frames ({len(fr['items'])})")
        L.append("")
        if fr.get("note"):
            L.append(fr["note"])
        for f in fr["items"]:
            L.append(f"- [{f['ts']}] {f['path']}")
        L.append("")

    L.append("## Transcript")
    L.append("")
    transcript_line = "\n".join(L).count("\n") + 2  # entries can span several lines
    if tr and tr["lines"]:
        body = captions.render_transcript([tuple(x) for x in tr["lines"]], meta.get("chapters"),
                                          st.get("sponsor"))
        L.append('<untrusted-content source="transcript">')
        L.append(fence(body).rstrip())
        L.append("</untrusted-content>")
    else:
        msg = "No transcript."
        if tr and tr.get("no_speech"):
            msg = "Speech-to-text found no speech (music or silence?)."
        elif not tr and a.no_stt:
            msg = "No captions, and speech-to-text was turned off (--no-stt)."
        if not st.get("frames"):
            msg += " Run again with --frames to see what's on screen."
        L.append(msg)
    L.append("")
    text = "\n".join(L)
    (job["outdir"] / "context.md").write_text(text, encoding="utf-8")
    return text, transcript_line


def print_context(job, text: str, transcript_line: int) -> None:
    tokens = int(len(text) / 3.5)
    path = job["outdir"] / "context.md"
    if tokens <= PRINT_LIMIT_TOKENS:
        print(text)
        print(f"\nCONTEXT_FILE: {path}")
        return
    lines = text.splitlines()
    print("\n".join(lines[: transcript_line - 1]))
    print(f"[Transcript too long to print (~{tokens:,} tokens). It is in {path}, "
          f"from line {transcript_line} to {len(lines)}. Read it in parts.]")
    index = [(i + 1, l) for i, l in enumerate(lines) if l.startswith("#### [")]
    if index:
        print("\nTranscript chapter index (line numbers in context.md):")
        for n, l in index:
            print(f"  line {n}: {l[5:]}")
    print(f"\nCONTEXT_FILE: {path}")


# ============================================================ commands

def find_runs(base: Path) -> list[Path]:
    return [p for p in base.iterdir() if p.is_dir()] if base.exists() else []


def cmd_stop(a, base: Path) -> int:
    targets = []
    for d in find_runs(base):
        lock = read_json(d / ".lock")
        if not lock:
            continue
        given = lock.get("input") or ""
        same_file = (a.stop and Path(a.stop).expanduser().exists() and Path(given).exists()
                     and Path(a.stop).expanduser().resolve() == Path(given).resolve())
        if a.stop_all or (a.stop and (a.stop in d.name or a.stop in given or same_file)):
            targets.append(d)
    if not targets:
        print("No running video-context job matched.")
        return 0
    for d in targets:
        found = stop_run(d, rep)
        print(f"{'Stopped' if found else 'Cleaned up stale lock in'}: {d}")
    return 0


def cmd_clean(a, base: Path, outdir: Path | None) -> int:
    dirs = [outdir] if outdir else [d for d in find_runs(base) if (d / "info.json").exists()]
    for d in dirs:
        if d and d.exists():
            lock = read_json(d / ".lock")
            if lock:
                from vclib.util import _lock_owner_alive
                if _lock_owner_alive(lock):
                    print(f"Skipped (running): {d}")
                    continue
            shutil.rmtree(d, ignore_errors=True)
            print(f"Removed: {d}")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="video_context.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="video URL (anything yt-dlp supports) or local file path")
    ap.add_argument("--lang", help="preferred transcript language, e.g. en, de, ja")
    g = ap.add_argument_group("frames (off by default)")
    g.add_argument("--frames", action="store_true", help="extract key frames")
    g.add_argument("--max-frames", type=int, help="max frames (default: 6 for a short reel up to 25)")
    g.add_argument("--every", type=float, help="one frame every N seconds instead of scene/chapter picks")
    g.add_argument("--at", help="frames at exact times, e.g. 1:23,4:56")
    g.add_argument("--scene", type=float, default=0.3, help="scene-change threshold 0-1 (default 0.3)")
    g = ap.add_argument_group("speech-to-text (only used when there are no captions)")
    g.add_argument("--stt", action="store_true", help="force speech-to-text even if captions exist")
    g.add_argument("--no-stt", action="store_true", help="never run speech-to-text")
    g.add_argument("--device", choices=["cpu", "cuda"], help="force STT device (default: auto)")
    g.add_argument("--yes", action="store_true", help="don't stop to ask before long speech-to-text")
    g = ap.add_argument_group("scope & extras")
    g.add_argument("--range", help="START-END for speech-to-text, frames and saved files, e.g. 10:00-25:00")
    g.add_argument("--video", action="store_true", help="also save the video (mp4)")
    g.add_argument("--audio", action="store_true", help="also save the audio (mp3)")
    g.add_argument("--comments", type=int, metavar="N", help="include the top N comments")
    g.add_argument("--no-sponsorblock", action="store_true", help="don't mark YouTube sponsor segments")
    g = ap.add_argument_group("login")
    g.add_argument("--cookies-from-browser", metavar="BROWSER[:PROFILE]",
                   help="use this browser's login cookies from the start")
    g.add_argument("--no-cookies", action="store_true", help="never use browser cookies")
    g = ap.add_argument_group("run control")
    g.add_argument("--out", type=Path, default=DEFAULT_BASE, help=f"base output folder (default {DEFAULT_BASE})")
    g.add_argument("--refresh", action="store_true", help="ignore cached results and fetch again")
    g.add_argument("--clean", action="store_true", help="delete cached output (this video, or all if no input)")
    g.add_argument("--max-time", help="give up after this long, e.g. 30m")
    g.add_argument("--stop", metavar="URL|ID|PATH", help="stop a running job for this video")
    g.add_argument("--stop-all", action="store_true", help="stop all running jobs")
    g.add_argument("--update", action="store_true", help="upgrade yt-dlp and exit")
    g.add_argument("--version", action="store_true", help="print tool versions and exit")
    ap.add_argument("--_upgraded", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        a.range = parse_range(a.range) if a.range else None
        a.max_time = parse_duration(a.max_time) if a.max_time else None
        if a.cookies_from_browser:
            cookies.parse_spec(a.cookies_from_browser)
    except ValueError as e:
        ap.error(str(e))
    if a.stt and a.no_stt:
        ap.error("--stt and --no-stt conflict")
    if (a.max_frames or a.every or a.at) and not a.frames:
        a.frames = True
    if not a.input and not (a.stop or a.stop_all or a.clean or a.update or a.version):
        ap.error("give a video URL or a local file path")
    return a


def rerun_upgraded(argv) -> int:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise Failed("uv not found on PATH.")
    rep.log("yt-dlp may be out of date for this site; upgrading it and retrying once...")
    return subprocess.call([uv, "run", "--quiet", "--upgrade-package", "yt-dlp", str(Path(__file__).resolve()),
                            *argv, "--_upgraded"])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    a = parse_args(argv)
    base = a.out.expanduser().resolve()

    if a.version:
        import yt_dlp
        print(f"yt-dlp {yt_dlp.version.__version__}\nffmpeg {media.ffmpeg()}\npython {sys.version.split()[0]}")
        return 0
    if a.update:
        uv = os.environ.get("UV") or shutil.which("uv")
        return subprocess.call([uv, "run", "--quiet", "--upgrade-package", "yt-dlp",
                                str(Path(__file__).resolve()), "--version"])
    if a.stop or a.stop_all:
        return cmd_stop(a, base)

    if a.clean and not a.input:
        return cmd_clean(a, base, None)

    # ---- resolve the input and the output folder
    job = {"args": a, "cookies": a.cookies_from_browser, "state": {}, "meta": {}}
    looks_like_domain = re.match(r"^[\w-]+(\.[\w-]+)+/", a.input) and not Path(a.input).expanduser().exists()
    if not is_url(a.input) and not looks_like_domain:
        path = Path(a.input).expanduser().resolve()
        if not path.is_file():
            raise Failed(f"File not found: {path}")
        job.update(kind="local", path=path, url=None, site="local", folder=local_folder(path))
    else:
        url = normalize_url(a.input if is_url(a.input) else "https://" + a.input)
        job.update(kind="url", url=url, path=None, linked_at=url_timestamp(url))
        sid = static_id(url)
        job["site"] = sid[0] if sid else cookies.site_domain(url).split(".")[0]
        job["folder"] = f"{safe(sid[0], 20)}-{safe(sid[1])}" if sid else None

    if a.clean:
        out = (base / job["folder"]) if job.get("folder") else None
        return cmd_clean(a, base, out)

    base.mkdir(parents=True, exist_ok=True)
    install_signal_handlers()
    if a.max_time:
        start_deadline(a.max_time, rep)

    lock = None
    try:
        if job["folder"] is None:  # site needs the page to know the id
            meta = fetch_info(job)
            job["meta"] = meta
            job["folder"] = f"{safe((meta.get('extractor_key') or job['site']).lower(), 20)}-{safe(str(meta.get('id')))}"
        outdir = base / job["folder"]
        outdir.mkdir(parents=True, exist_ok=True)
        job["outdir"] = outdir
        rep.attach(outdir)
        lock = Lock(outdir, a.input)
        lock.acquire()
        cleanup_temp(outdir)  # leftovers of a killed run
        rep.status(state="running", stage="starting", started=now_iso(), input=a.input, progress=None,
                   eta_s=None, message=None)

        state = {} if a.refresh else (read_json(outdir / "info.json", {}) or {})
        if a.refresh:
            for p in list(outdir.glob("stt_*.jsonl")) + [outdir / "context.md"]:
                p.unlink(missing_ok=True)
            shutil.rmtree(outdir / "frames", ignore_errors=True)
        job["state"] = state
        if state.get("cookies") and not job["cookies"] and not a.no_cookies:
            job["cookies"] = state["cookies"]

        def save():
            state["cookies"] = job["cookies"]
            state["updated"] = now_iso()
            write_json(outdir / "info.json", state)

        # ---- metadata
        if not job["meta"]:
            cached = state.get("meta")
            if cached and not (a.comments and "comments" not in cached):
                job["meta"] = cached
            elif job["kind"] == "local":
                rep.status(stage="reading file")
                job["meta"] = media.probe(job["path"])
            else:
                job["meta"] = fetch_info(job)
        state["meta"] = {k: v for k, v in job["meta"].items() if not k.startswith("_")}
        state["input"] = a.input
        save()
        meta = job["meta"]

        if (job["kind"] == "url" and (meta.get("extractor_key") or "").lower() == "youtube"
                and not a.no_sponsorblock and "sponsor" not in state):
            state["sponsor"] = sponsor_segments(meta["id"])
            save()
        elif a.no_sponsorblock:
            state.pop("sponsor", None)

        # ---- saved files (before frames, so frames can reuse a saved video)
        if a.video or a.audio:
            state["saved"] = {**(state.get("saved") or {}), **save_media(job)}
            state["saved"].pop("video_full", None)
            if job.get("saved_video"):
                state["saved"]["video_full"] = job["saved_video"]
            save()
        elif (state.get("saved") or {}).get("video_full") and Path(state["saved"]["video_full"]).exists():
            job["saved_video"] = state["saved"]["video_full"]

        # ---- transcript
        tr = state.get("transcript")
        want_stt = a.stt
        if tr and (tr.get("requested_lang") != a.lang or (want_stt and not tr["source"].startswith("speech"))
                   or (a.range and tr["source"].startswith("speech") and tr.get("range") != list(a.range))):
            tr = None
        if not tr:
            if not want_stt:
                rep.status(stage="looking for captions")
                if job["kind"] == "url" and "_subtitles" not in job["meta"]:
                    job["meta"] = {**fetch_info(job), **{k: v for k, v in job["meta"].items() if k == "comments"}}
                tr = transcript_from_captions(job)
            if tr is None and not a.no_stt:
                tr = transcript_from_stt(job)
                if tr is not None and a.range:
                    tr["range"] = list(a.range)
            if tr is not None:
                tr["requested_lang"] = a.lang
        state["transcript"] = tr
        save()

        # ---- frames
        if a.frames:
            state["frames"] = do_frames(job)
            save()

        text, tline = build_context(job)
        cleanup_temp(outdir)
        rep.status(state="done", stage="done", progress=1.0, eta_s=None)
        print_context(job, text, tline)
        return 0

    except NeedsInput as e:
        rep.status(state="waiting", message=str(e))
        print(f"NEEDS_INPUT\n{e}")
        return 3
    except UpgradeRetry:
        if lock:
            lock.release()
            lock = None
        return rerun_upgraded(argv)
    except (Cancelled, KeyboardInterrupt) as e:
        kill_children()
        if job.get("outdir"):
            cleanup_temp(job["outdir"])
        rep.status(state="cancelled", message=f"stopped ({e or 'interrupted'}); partial results kept")
        print("STOPPED: partial results are kept; run the same command again to continue.")
        return 130
    except Failed as e:
        kill_children()
        rep.status(state="failed", message=str(e))
        print(f"FAILED: {e}")
        return 1
    finally:
        kill_children(timeout=1.0)
        if lock:
            lock.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failed as e:  # failures before the run started (bad input etc.)
        print(f"FAILED: {e}")
        sys.exit(1)
