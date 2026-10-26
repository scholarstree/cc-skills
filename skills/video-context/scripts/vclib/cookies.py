"""Find installed browsers/profiles and which of them are logged in to a site.

Cookie values are never printed or stored; only the browser/profile choice is recorded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp import cookies as ytc

# Cookies that only exist when logged in. Other sites: any cookie for the domain counts.
LOGIN_COOKIES = {
    "youtube.com": {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"},
    "google.com": {"SID", "__Secure-1PSID", "__Secure-3PSID"},
    "instagram.com": {"sessionid", "ds_user_id"},
    "facebook.com": {"c_user"},
    "tiktok.com": {"sessionid", "sessionid_ss", "sid_tt"},
    "x.com": {"auth_token"},
    "twitter.com": {"auth_token"},
    "vimeo.com": {"vimeo"},
    "reddit.com": {"reddit_session", "token_v2"},
    "twitch.tv": {"auth-token"},
}

_ALIASES = {"youtu.be": "youtube.com", "fb.watch": "facebook.com", "instagr.am": "instagram.com"}


def site_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host in _ALIASES:
        return _ALIASES[host]
    labels = host.split(".")
    # keep e.g. bbc.co.uk intact
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class _QuietLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _extra_chromium_dirs(browser: str) -> list[str]:
    """Snap/Flatpak installs that yt-dlp's default lookup doesn't cover (Linux)."""
    if not sys.platform.startswith("linux"):
        return []
    home = Path.home()
    extra = {
        "chromium": [home / "snap/chromium/common/chromium", home / ".var/app/org.chromium.Chromium/config/chromium"],
        "chrome": [home / ".var/app/com.google.Chrome/config/google-chrome"],
        "brave": [home / "snap/brave/current/.config/BraveSoftware/Brave-Browser",
                  home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"],
        "edge": [home / ".var/app/com.microsoft.Edge/config/microsoft-edge"],
        "vivaldi": [home / ".var/app/com.vivaldi.Vivaldi/config/vivaldi"],
    }
    return [str(p) for p in extra.get(browser, [])]


def find_profiles() -> list[dict]:
    """Installed browser profiles that have a cookie store, in check order:
    Firefox, Safari, then Chromium-family (on macOS these trigger a Keychain prompt)."""
    found: list[dict] = []

    # Firefox
    try:
        dbs = sorted(set(ytc._firefox_cookie_dbs(ytc._firefox_browser_dirs())))
    except Exception:
        dbs = []
    for db in dbs:
        prof = os.path.dirname(db)
        found.append({"browser": "firefox", "profile": prof,
                      "label": f"firefox ({os.path.basename(prof)})", "chromium": False})

    # Safari
    if sys.platform == "darwin":
        for p in ("~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
                  "~/Library/Cookies/Cookies.binarycookies"):
            if os.path.exists(os.path.expanduser(p)):
                found.append({"browser": "safari", "profile": None, "label": "safari", "chromium": False})
                break

    # Chromium family
    for browser in sorted(ytc.CHROMIUM_BASED_BROWSERS):
        try:
            settings = ytc._get_chromium_based_browser_settings(browser)
        except Exception:
            continue
        roots = [settings["browser_dir"]] + _extra_chromium_dirs(browser)
        for root in roots:
            if not os.path.isdir(root):
                continue
            if not settings["supports_profiles"]:
                if _has_chromium_cookies(root):
                    found.append({"browser": browser, "profile": root, "label": browser, "chromium": True})
                continue
            for sub in sorted(os.listdir(root)):
                prof = os.path.join(root, sub)
                if os.path.isdir(prof) and _has_chromium_cookies(prof):
                    found.append({"browser": browser, "profile": prof,
                                  "label": f"{browser} ({sub})", "chromium": True})
    return found


def _has_chromium_cookies(profile: str) -> bool:
    return os.path.isfile(os.path.join(profile, "Cookies")) or os.path.isfile(
        os.path.join(profile, "Network", "Cookies"))


def check_login(profile: dict, domain: str) -> str:
    """'yes' | 'no' | 'unreadable: <reason>'"""
    try:
        jar = ytc.extract_cookies_from_browser(profile["browser"], profile["profile"], _QuietLogger())
    except Exception as e:  # locked DB, app-bound encryption, keychain denied, ...
        return f"unreadable: {type(e).__name__}: {str(e)[:120]}"
    names = LOGIN_COOKIES.get(domain)
    all_cookies = list(jar)
    if len(all_cookies) >= 20 and sum(1 for c in all_cookies if not c.value) > len(all_cookies) / 2:
        return "unreadable: most cookies couldn't be decrypted (browser keyring locked or unsupported)"
    undecrypted = False
    for c in all_cookies:
        d = (c.domain or "").lstrip(".").lower()
        if (d == domain or d.endswith("." + domain)) and (not names or c.name in names):
            if c.value:
                return "yes"
            undecrypted = True
    if undecrypted:
        # yt-dlp keeps cookies it couldn't decrypt with an empty value (e.g. locked Linux
        # keyring, Chrome app-bound encryption on Windows). A login may well exist.
        return "unreadable: login cookies found but couldn't be decrypted (browser keyring locked or unsupported)"
    return "no"


def scan(domain: str) -> dict:
    """Check every profile for a login to `domain`.
    Returns {"logged_in": [...], "checked": [...], "unreadable": [...]}."""
    profiles = find_profiles()
    result = {"logged_in": [], "checked": [], "unreadable": []}

    def run(group):
        for p in group:
            state = check_login(p, domain)
            result["checked"].append(p["label"])
            if state == "yes":
                result["logged_in"].append(p)
            elif state.startswith("unreadable"):
                result["unreadable"].append(f"{p['label']}: {state[len('unreadable: '):]}")

    if sys.platform == "darwin":
        # Avoid Keychain popups unless Firefox/Safari have nothing.
        run([p for p in profiles if not p["chromium"]])
        if not result["logged_in"]:
            run([p for p in profiles if p["chromium"]])
    else:
        run(profiles)
    return result


def spec(profile: dict) -> str:
    """Value for --cookies-from-browser that selects exactly this profile."""
    return profile["browser"] + (f":{profile['profile']}" if profile.get("profile") else "")


def parse_spec(text: str) -> tuple:
    """'firefox' / 'chrome:/path/to/Profile 1' -> yt-dlp's cookiesfrombrowser tuple."""
    browser, _, profile = text.partition(":")
    browser = browser.strip().lower()
    if browser not in ytc.SUPPORTED_BROWSERS:
        raise ValueError(f"unsupported browser '{browser}' (supported: {', '.join(sorted(ytc.SUPPORTED_BROWSERS))})")
    return (browser, profile or None, None, None)


def describe(spec_text: str) -> str:
    browser, _, profile = spec_text.partition(":")
    if not profile:
        return browser
    name = os.path.basename(profile.rstrip("/\\"))
    return f"{browser} ({name})"
