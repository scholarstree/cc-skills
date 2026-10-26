# /// script
# requires-python = ">=3.10"
# dependencies = ["faster-whisper>=1.1"]
# ///
"""Speech-to-text for video-context: faster-whisper `small`, int8.

Appends one JSON line per segment to --out as it goes (so a stopped run keeps its work),
and a final {"done": true, ...} line. Progress goes to stdout as `PROGRESS <seconds>`.
Run by video_context.py; on CUDA it is launched with the nvidia-cublas/cudnn wheels added.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import sysconfig

MODEL = "small"
COMPUTE = "int8"


def nvidia_lib_dirs() -> list[str]:
    """lib (Linux) / bin (Windows) folders of the pip-installed nvidia-* wheels."""
    site = sysconfig.get_paths()["purelib"]
    sub = "bin" if sys.platform == "win32" else "lib"
    return sorted(glob.glob(os.path.join(site, "nvidia", "*", sub)))


def ensure_cuda_libs() -> None:
    dirs = nvidia_lib_dirs()
    if not dirs:
        return
    if sys.platform == "win32":
        for d in dirs:
            os.add_dll_directory(d)
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    elif sys.platform.startswith("linux"):
        # The dynamic loader reads LD_LIBRARY_PATH only at startup, so re-exec once.
        current = os.environ.get("LD_LIBRARY_PATH", "")
        if not all(d in current.split(os.pathsep) for d in dirs):
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + ([current] if current else []))
            os.execv(sys.executable, [sys.executable] + sys.argv)


def say(*parts) -> None:
    print(*parts, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out", required=True)
    ap.add_argument("--offset", type=float, default=0.0, help="added to every timestamp")
    ap.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    ap.add_argument("--device-index", type=int, default=0)
    ap.add_argument("--strict-device", action="store_true", help="fail instead of falling back to CPU")
    ap.add_argument("--lang", default=None)
    a = ap.parse_args()

    if a.device == "cuda":
        ensure_cuda_libs()

    say("STAGE loading model (first run downloads ~500 MB)")
    from faster_whisper import WhisperModel

    def load(device):
        return WhisperModel(MODEL, device=device, device_index=a.device_index if device == "cuda" else 0,
                            compute_type=COMPUTE)

    def transcribe(model):
        return model.transcribe(a.audio, language=a.lang, vad_filter=True, beam_size=5)

    device = a.device
    try:
        model = load(device)
        segments, info = transcribe(model)
        segments = iter(segments)
        first = next(segments, None)  # CUDA library problems surface on the first decode
    except Exception as e:
        if device != "cuda" or a.strict_device:
            raise
        say(f"DEVICE cpu (CUDA failed: {type(e).__name__}: {str(e)[:200]})")
        device = "cpu"
        model = load(device)
        segments, info = transcribe(model)
        segments = iter(segments)
        first = next(segments, None)
    else:
        say(f"DEVICE {device}")
    say(f"LANG {info.language} {info.language_probability:.2f}")

    with open(a.out, "a", encoding="utf-8") as f:
        seg = first
        while seg is not None:
            text = seg.text.strip()
            if text:
                f.write(json.dumps({"start": round(seg.start + a.offset, 2),
                                    "end": round(seg.end + a.offset, 2), "text": text},
                                   ensure_ascii=False) + "\n")
                f.flush()
            say(f"PROGRESS {seg.end + a.offset:.1f}")
            seg = next(segments, None)
        f.write(json.dumps({"done": True, "language": info.language, "device": device,
                            "model": f"{MODEL} {COMPUTE}"}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
