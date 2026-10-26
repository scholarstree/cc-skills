# video-context

A Claude Code skill that loads a video into context: title, channel, description, chapters
and a timestamped transcript, plus optional key frames. Works with YouTube, Instagram, TikTok,
X, Vimeo and [everything else yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md),
and with local video and audio files. Windows, macOS and Linux.

```
/video-context https://youtu.be/kCc8FmEb1nY what does he say about layernorm?
/video-context https://www.instagram.com/reel/XXXX/ --frames
/video-context ~/Videos/meeting.mp4 summarize the decisions
```

## Install

1. Install **uv** (the only requirement):
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`)
   - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (or `winget install astral-sh.uv`)
2. Install the skill, either way works:
   - from the repo root: `python install.py video-context`
   - or copy this folder to `~/.claude/skills/video-context/`

On first use uv installs Python, yt-dlp and ffmpeg (~10–20 s). The first time a video has no
captions, it also installs faster-whisper and downloads the `small` model (~500 MB).
A system `ffmpeg` is used if present; otherwise a bundled one.

## What it does

| Step | Detail |
|---|---|
| Metadata | Title, channel, date, duration, views, description, chapters, tags (local files: from the file) |
| Transcript | First available: creator subtitles → auto-captions (original language, never machine-translated) → embedded or sidecar `.srt`/`.vtt` → speech-to-text |
| Speech-to-text | faster-whisper `small` int8. GPU if one has ≥1.5 GB free VRAM, else CPU (macOS: CPU). Saves as it goes; a re-run resumes. Asks first if over 20 minutes. |
| Frames (`--frames`) | 720p JPGs at chapter starts and scene changes, near-duplicates removed, ~6 for a reel up to 25 |
| SponsorBlock | YouTube sponsor segments are labelled in the transcript |
| Output | `~/Downloads/video-context/<site>-<id>/context.md` (printed, or indexed by line if large) |

Run `uv run scripts/video_context.py --help` for all flags.

## Logins and cookies

Public videos never touch your browser. If a site blocks the download (Instagram often does),
the skill checks which installed browsers are logged in to that site:

- one → uses its cookies **and tells you which browser it used**
- several → asks you which one
- none → stops and says so

Cookie values are never printed or saved. Notes:

- **Windows:** Chrome/Edge cookies are often unreadable (app-bound encryption) and the browser
  must be closed; Firefox always works.
- **macOS:** Chrome-family browsers trigger a Keychain prompt, so Firefox and Safari are checked
  first. Safari needs Full Disk Access for your terminal app.
- **Linux:** Chrome/Edge/Brave cookies are decrypted with the desktop keyring, which doesn't
  always work; Firefox always does.

Using your logged-in account with a download tool carries a small risk of the site rate-limiting
or flagging that account.

## Stopping

Ask Claude to stop: it stops the job (`--stop`) and keeps partial results. Running the same
request again continues where it stopped. (Esc in Claude Code does not stop a background command.)

## Respect the content

Only download what you're allowed to. Platform terms and copyright still apply.
