# cc-skills

Skills for [Claude Code](https://claude.com/claude-code). Each one is self-contained and installs
on its own. Windows, macOS and Linux.

| Skill | What it does | Needs |
|---|---|---|
| [video-context](skills/video-context/) | Loads a video into Claude's context: metadata, chapters, a timestamped transcript (captions, or speech-to-text when there are none) and optional key frames. YouTube, Instagram, TikTok, any [yt-dlp site](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), or a local file. | [uv](https://docs.astral.sh/uv/) |

## Quick start

```bash
git clone https://github.com/scholarstree/cc-skills.git
cd cc-skills
python install.py video-context       # python3 on macOS/Linux, py on Windows
```

Start a new Claude Code session, then:

```
/video-context https://www.youtube.com/watch?v=CBYhVcO4WgI what are the four areas of success?
```

Or just paste a video link and ask about it; Claude uses the skill on its own.

Instagram reels work the same way. They have no captions to download, so the skill transcribes the
audio locally with speech-to-text: in testing, a 1-minute public reel took about 6 seconds on a
GPU, with no login needed.

## Install

Each skill is a folder under `skills/`. Install only the ones you want:

| Command | Effect |
|---|---|
| `python install.py <name> ...` | Symlink into `~/.claude/skills/` (updates with `git pull`) |
| `python install.py --all` | Install every skill |
| `python install.py --list` | Show skills and whether they're installed |
| `python install.py --uninstall <name>` | Remove |
| `--copy` | Copy instead of symlink (also the automatic fallback on Windows without symlink rights) |
| `--project` | Install into `./.claude/skills/` of the current project |
| `--force` | Replace an existing install; the old one is moved to `~/.claude/skills-backup/`, never deleted |

`CLAUDE_CONFIG_DIR` is respected if you've set it. Or install by hand: copy `skills/<name>/` to
`~/.claude/skills/<name>/`.

An installed skill appears as `/<name>` in Claude Code, and Claude also uses it whenever a request
matches its description.

## Privacy and safety

- **video-context** downloads only what you ask for and keeps it in `~/Downloads/video-context/`.
- It uses your browser's login cookies only when a site refuses without one, and it tells you which
  browser it used. If several browsers are logged in, it asks. Cookie values are never printed or
  saved.
- Video titles, descriptions, comments and transcripts are written by strangers. They're marked as
  untrusted so Claude treats them as data, not instructions.
- Only download what you're allowed to; platform terms and copyright still apply.

## Status

Developed and tested on Linux (Ubuntu 22.04, NVIDIA GPU). Windows and macOS are supported by
design but not yet tested; reports are welcome.

## Layout

```
skills/<name>/
├── SKILL.md      what Claude reads: when to use the skill and how
├── README.md     for people: install, requirements, options, examples
└── scripts/      everything it runs; nothing is shared between skills
```

## License

[MIT](LICENSE)
