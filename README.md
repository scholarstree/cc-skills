# cc-skills

Claude Code skills, each self-contained and installable on its own. Windows, macOS and Linux.

| Skill | What it does | Needs |
|---|---|---|
| [video-context](skills/video-context/) | Loads a video (YouTube, Instagram, TikTok, any yt-dlp site, or a local file) into context: metadata, description, chapters, timestamped transcript, optional key frames | [uv](https://docs.astral.sh/uv/) |

## Install

Each skill is a folder under `skills/`. Install only the ones you want:

```bash
git clone <this repo> cc-skills
cd cc-skills
python install.py video-context      # or: python3 / py on Windows
```

| Command | Effect |
|---|---|
| `python install.py <name> ...` | symlink into `~/.claude/skills/` (updates with `git pull`) |
| `python install.py --all` | install every skill |
| `python install.py --list` | show skills and install state |
| `python install.py --uninstall <name>` | remove |
| `--copy` | copy instead of symlink (also the automatic fallback on Windows without symlink rights) |
| `--project` | install into `./.claude/skills/` of the current project |
| `--force` | replace an existing install (the old one is backed up, never deleted) |

Or install by hand: copy `skills/<name>/` to `~/.claude/skills/<name>/`.

An installed skill appears as `/<name>` in Claude Code, and Claude also uses it on its own when a
request matches its description. Start a new session after installing.

## Layout

```
skills/<name>/
├── SKILL.md      # what Claude reads: when to use the skill and how
├── README.md     # for humans: install, requirements, options
└── scripts/      # everything it runs; nothing is shared between skills
```
