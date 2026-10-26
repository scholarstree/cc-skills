"""Caption track choice, VTT/SRT parsing, auto-caption de-duplication, paragraphing."""
from __future__ import annotations

import html
import re
from collections import deque

from .util import fmt_ts

# ---------------------------------------------------------------- track choice


def _base(lang: str | None) -> str | None:
    return lang.split("-")[0].lower() if lang else None


def _match(tracks: dict, lang: str | None) -> str | None:
    if not lang or not tracks:
        return None
    if lang in tracks:
        return lang
    for k in tracks:
        if _base(k) == _base(lang) and not k.endswith("-orig"):
            return k
    return None


def pick_track(info: dict, want: str | None) -> tuple[str, str, list] | None:
    """Return (kind, lang_key, formats) for the best caption track, or None.

    Order: creator subs in the requested language, original-language auto-captions in the
    requested language, creator subs in the video's language, creator subs in English,
    original-language auto-captions, any creator subs. YouTube's machine-translated
    auto-captions are never used.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat" and v}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}
    orig = info.get("language")

    # The original-language auto track. YouTube labels it "<lang>-orig"; every other
    # auto track there is a machine translation of it. Auto-dubbed videos have one
    # "-orig" track per dubbed audio language, so pick the one in the video's own language.
    auto_orig = None
    origs = [k for k in auto if k.endswith("-orig")]
    if origs:
        def orig_in(lang):
            return next((k for k in origs if lang and _base(k.removesuffix("-orig")) == _base(lang)), None)
        auto_orig = orig_in(orig) or orig_in("en") or origs[0]
    elif auto:
        auto_orig = _match(auto, orig) or (_match(auto, "en") if len(auto) <= 3 else None)
        if auto_orig is None and len(auto) <= 3:
            auto_orig = next(iter(auto))

    def auto_in(lang):
        if origs:
            return orig_in(lang)
        if auto_orig and lang and _base(auto_orig.removesuffix("-orig")) == _base(lang):
            return auto_orig
        return None

    for kind, key in (
        ("creator subtitles", _match(manual, want)),
        ("auto-captions", auto_in(want)),
        ("creator subtitles", _match(manual, orig)),
        ("creator subtitles", _match(manual, "en")),
        ("auto-captions", auto_orig),
        ("creator subtitles", next(iter(manual), None)),
    ):
        if key:
            tracks = manual if kind == "creator subtitles" else auto
            return kind, key, tracks[key]
    return None


def pick_format(formats: list) -> dict | None:
    by_ext = {f.get("ext"): f for f in formats if f.get("url")}
    for ext in ("vtt", "srt"):
        if ext in by_ext:
            return by_ext[ext]
    return None


# ---------------------------------------------------------------- parsing

_ARROW = re.compile(r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


def _ts(text: str) -> float:
    parts = text.replace(",", ".").split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def parse_cues(text: str) -> list[tuple[float, float, list[str]]]:
    """Parse WebVTT or SRT into (start, end, [clean lines])."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _ARROW.search(line)
            if m:
                body = []
                for raw in lines[i + 1:]:
                    clean = _SPACE.sub(" ", html.unescape(_TAG.sub("", raw))).strip()
                    if clean:
                        body.append(clean)
                cues.append((_ts(m.group(1)), _ts(m.group(2)), body))
                break
    cues.sort(key=lambda c: c[0])
    return cues


def cues_to_lines(cues, window: int = 3) -> list[tuple[float, str]]:
    """Flatten cues to (start, line), dropping the repeats rolling auto-captions produce
    (each YouTube auto cue repeats the previous line, plus 10 ms "snapshot" cues)."""
    out: list[tuple[float, str]] = []
    recent: deque[str] = deque(maxlen=window)
    for start, _end, lines in cues:
        for line in lines:
            if line in recent:
                continue
            out.append((start, line))
            recent.append(line)
    return out


# ---------------------------------------------------------------- layout

_SENTENCE_END = re.compile(r"[.!?…。！？]['\")\]]?$")


def render_transcript(
    lines: list[tuple[float, str]],
    chapters: list[dict] | None = None,
    segments: list[dict] | None = None,
) -> str:
    """Group lines into timestamped paragraphs (~30-45 s), with chapter headings and
    SponsorBlock labels. Each paragraph starts with [mm:ss]."""
    chapters = sorted(chapters or [], key=lambda c: c.get("start_time") or 0)
    segments = segments or []

    def label_at(t):
        for s in segments:
            if s["start"] <= t < s["end"]:
                return s["label"]
        return None

    out: list[str] = []
    buf: list[str] = []
    para_start = None
    para_label = None
    ci = 0

    def flush():
        nonlocal buf, para_start
        if buf:
            tag = f" [{para_label}]" if para_label else ""
            out.append(f"[{fmt_ts(para_start)}]{tag} " + " ".join(buf))
            out.append("")
        buf, para_start = [], None

    for t, text in lines:
        while ci < len(chapters) and (chapters[ci].get("start_time") or 0) <= t + 0.5:
            flush()
            ch = chapters[ci]
            out.append(f"#### [{fmt_ts(ch.get('start_time'))}] {ch.get('title') or 'Chapter'}")
            out.append("")
            ci += 1
        lab = label_at(t)
        if buf and lab != para_label:
            flush()
        para_label = lab
        if para_start is None:
            para_start = t
        buf.append(text)
        span = t - para_start
        if (span >= 25 and _SENTENCE_END.search(text)) or span >= 45:
            flush()
    flush()
    return "\n".join(out).rstrip() + "\n"
