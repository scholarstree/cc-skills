#!/usr/bin/env python3
"""Install skills from this repo into Claude Code. Works on Windows, macOS and Linux.

  python install.py <skill> [<skill> ...]   install (symlink) into ~/.claude/skills/
  python install.py --all                   install every skill in skills/
  python install.py --list                  list skills and whether they're installed
  python install.py --uninstall <skill>     remove an installed skill

Options:
  --copy      copy instead of symlink (default: symlink, so `git pull` updates it)
  --project   install into ./.claude/skills/ (current project) instead of ~/.claude/skills/
  --force     replace an existing different install (it's moved to <claude dir>/skills-backup/, not deleted)

The Claude directory is ~/.claude, or $CLAUDE_CONFIG_DIR if set (ignored with --project).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILLS = REPO / "skills"

for _s in (sys.stdout, sys.stderr):  # non-ASCII paths on Windows consoles/pipes
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def available() -> dict[str, Path]:
    return {p.name: p for p in sorted(SKILLS.iterdir()) if (p / "SKILL.md").is_file()}


def claude_dir(project: bool) -> Path:
    if project:
        return Path.cwd() / ".claude"
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def target_dir(project: bool) -> Path:
    return claude_dir(project) / "skills"


def backup_path(dest_root: Path, name: str) -> Path:
    """Outside the skills folder, so Claude Code doesn't load the backup as a second skill."""
    root = dest_root.parent / "skills-backup"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path, n = root / f"{name}.{stamp}", 1
    while path.exists():
        n += 1
        path = root / f"{name}.{stamp}-{n}"
    return path


def describe(dst: Path, src: Path) -> str:
    if dst.is_symlink():
        return "linked" if dst.resolve() == src.resolve() else f"linked elsewhere ({os.readlink(dst)})"
    if dst.is_dir():
        return "copied" if (dst / "SKILL.md").is_file() else "other folder"
    return "not installed"


def check_deps(name: str) -> None:
    if name == "video-context" and not shutil.which("uv"):
        print("  ! needs uv: https://docs.astral.sh/uv/getting-started/installation/")


def install(name: str, src: Path, dest_root: Path, copy: bool, force: bool) -> bool:
    dst = dest_root / name
    state = describe(dst, src)
    if state == "linked" and not copy:
        print(f"= {name}: already linked")
        return True
    if state != "not installed":
        if not force:
            print(f"! {name}: {dst} already exists ({state}). Use --force to replace it (it'll be backed up).")
            return False
        if dst.is_symlink():
            dst.unlink()
            print(f"  removed old link {dst}")
        else:
            backup = backup_path(dest_root, name)
            shutil.move(str(dst), str(backup))
            print(f"  backed up {dst} -> {backup}")

    dest_root.mkdir(parents=True, exist_ok=True)
    if not copy:
        try:
            os.symlink(src, dst, target_is_directory=True)
            print(f"+ {name}: linked {dst} -> {src}")
            check_deps(name)
            return True
        except OSError as e:
            # Windows without Developer Mode / admin can't create symlinks.
            print(f"  couldn't create a symlink ({e.__class__.__name__}); copying instead.")
            print("  (a copy doesn't update with `git pull`; re-run install after updating)")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"+ {name}: copied to {dst}")
    check_deps(name)
    return True


def uninstall(name: str, src: Path | None, dest_root: Path) -> bool:
    dst = dest_root / name
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_dir():
        if not (dst / "SKILL.md").is_file():
            print(f"! {name}: {dst} doesn't look like a skill; not touching it.")
            return False
        shutil.rmtree(dst)
    else:
        print(f"= {name}: not installed in {dest_root}")
        return True
    print(f"- {name}: removed {dst}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skills", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--copy", action="store_true")
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    skills = available()
    dest_root = target_dir(a.project)

    if a.list or not (a.skills or a.all):
        print(f"Skills in {SKILLS} (install target: {dest_root}):")
        for name, src in skills.items():
            print(f"  {name:28} {describe(dest_root / name, src)}")
        if not a.list:
            print("\nUsage: python install.py <skill> ... | --all | --list | --uninstall <skill>")
        return 0

    names = list(skills) if a.all else a.skills
    # Uninstalling works for skills that are no longer in the repo, too.
    unknown = [] if a.uninstall else [n for n in names if n not in skills]
    if unknown:
        print(f"Unknown skill(s): {', '.join(unknown)}. Available: {', '.join(skills)}")
        return 1

    ok = True
    for n in names:
        if a.uninstall:
            ok &= uninstall(n, skills.get(n), dest_root)
        else:
            ok &= install(n, skills[n], dest_root, a.copy, a.force)
    if ok and not a.uninstall:
        print("\nRestart Claude Code (or start a new session) to pick up new skills.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
