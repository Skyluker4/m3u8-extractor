#!/usr/bin/env python3
# Copyright (C) 2026 Luke Andrew Simmons
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Luke Andrew Simmons is designated as the proxy who can decide whether
# future versions of the GNU Affero General Public License can be used,
# as described in Section 14 of version 3 of the license.

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import yt_dlp
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
class _ProgressTracker:
    """Thread-safe live progress tracker for batch downloads."""

    def __init__(self, total, speed_unit="bytes", max_active=1):
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self.total = total
        self.completed = 0
        self.failed = 0
        self.bytes_downloaded = 0
        self.bytes_total_known = 0  # sum of known totals from yt-dlp
        self._active_totals = {}  # url -> total_bytes for current download
        self._active_downloaded = {}  # url -> downloaded_bytes for current download
        self._active_info = {}  # url -> dict with per-download display info
        self._prev_bytes = 0  # for speed calculation
        self._prev_time = 0.0  # for speed calculation
        self._speed = 0.0  # smoothed bytes/sec
        self._speed_unit = str(speed_unit).strip().lower()  # "bytes" or "bits"
        self._max_active = max(1, int(max_active))
        self._reserved_lines = 1 + self._max_active  # download lines + summary bar
        self.start_time = time.time()
        self._prev_time = self.start_time
        self._enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        self._last_bar = ""
        self._scroll_region_set = False
        self._output_buffer = []

    def setup_scroll_region(self):
        """Reserve the bottom terminal lines for download progress + summary bar."""
        if not self._enabled:
            return
        term_h = shutil.get_terminal_size((80, 24)).lines
        scroll_end = max(1, term_h - self._reserved_lines)
        sys.stdout.write("\033[s")
        sys.stdout.write(f"\033[1;{scroll_end}r")
        sys.stdout.write("\033[u")
        sys.stdout.flush()
        self._scroll_region_set = True

    def reset_scroll_region(self):
        """Restore the full terminal scroll region."""
        if not self._enabled or not self._scroll_region_set:
            return
        self.clear_bar()
        sys.stdout.write("\033[r")
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        self._scroll_region_set = False

    def buffer_line(self, msg):
        """Append a message to the output buffer (thread-safe)."""
        with self._lock:
            self._output_buffer.append(msg)

    def print_live(self, msg):
        """Buffer a message, print it in the scroll region, and redraw the bar.

        All terminal I/O is serialised via ``_io_lock`` so output from
        yt-dlp's internal threads never interleaves with the progress bar.
        """
        with self._lock:
            self._output_buffer.append(msg)
        with self._io_lock:
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
        self.draw_bar()

    def flush_buffer(self):
        """Replay all buffered output lines as normal terminal text."""
        with self._lock:
            lines = list(self._output_buffer)
            self._output_buffer.clear()
        if not lines:
            return
        for line in lines:
            print(line)

    @property
    def remaining(self):
        return self.total - self.completed - self.failed

    def record_success(self):
        with self._lock:
            self.completed += 1

    def record_failure(self):
        with self._lock:
            self.failed += 1

    def update_bytes(self, url, downloaded, total, info=None):
        """Called from yt-dlp progress hook with per-URL byte counts.

        info: optional dict with keys like 'filename', 'speed', 'eta'.
        """
        with self._lock:
            self._active_downloaded[url] = downloaded or 0
            if total and total > 0:
                self._active_totals[url] = total
            if info is not None:
                self._active_info[url] = info

    def finish_bytes(self, url):
        """Mark a URL's bytes as finalised (move active → completed)."""
        with self._lock:
            final = self._active_downloaded.pop(url, 0)
            self._active_totals.pop(url, None)
            self._active_info.pop(url, None)
            self.bytes_downloaded += final

    def _format_bytes(self, n):
        for unit in ("", "K", "M", "G", "T"):
            if abs(n) < 1024:
                return f"{n:.1f}{unit}B" if unit else f"{int(n)}B"
            n /= 1024
        return f"{n:.1f}PB"

    def _format_speed(self, bytes_per_sec):
        """Format speed as bytes/s or bits/s according to config."""
        if self._speed_unit == "bits":
            return self._format_rate(bytes_per_sec * 8, 1000, "bps")
        return self._format_rate(bytes_per_sec, 1024, "B/s")

    @staticmethod
    def _format_rate(val, base, suffix):
        for prefix in ("", "K", "M", "G", "T"):
            if abs(val) < base:
                return f"{val:.1f} {prefix}{suffix}" if prefix else f"{int(val)} {suffix}"
            val /= base
        return f"{val:.1f} P{suffix}"

    def _update_speed(self, total_downloaded):
        """Calculate smoothed download speed."""
        now = time.time()
        dt = now - self._prev_time
        if dt >= 0.5:  # update speed every 0.5s to avoid jitter
            delta_bytes = total_downloaded - self._prev_bytes
            instant = delta_bytes / dt if dt > 0 else 0
            # Exponential moving average (α = 0.3)
            self._speed = 0.3 * instant + 0.7 * self._speed
            self._prev_bytes = total_downloaded
            self._prev_time = now

    def clear_bar(self):
        """Clear all reserved bottom lines (download lines + summary bar)."""
        if not self._enabled or not self._last_bar:
            return
        term_h = shutil.get_terminal_size((80, 24)).lines
        first_row = term_h - self._reserved_lines + 1
        buf = "\033[s"  # save cursor
        for row in range(first_row, term_h + 1):
            buf += f"\033[{row};0H\033[K"
        buf += "\033[u"  # restore cursor
        with self._io_lock:
            sys.stdout.write(buf)
            sys.stdout.flush()
        self._last_bar = ""

    def _build_download_line(self, url, term_width):
        """Build a single per-download progress line."""
        downloaded = self._active_downloaded.get(url, 0)
        total = self._active_totals.get(url)
        info = self._active_info.get(url, {})

        # Filename (truncate to fit)
        fname = info.get("filename", "")
        if fname:
            fname = os.path.basename(fname)

        # Percentage
        if total and total > 0:
            pct = min(downloaded / total * 100, 100)
            pct_str = f"{pct:5.1f}%"
        else:
            pct_str = "  ???"

        # Size
        total_str = self._format_bytes(total) if total else "?"
        dl_str = self._format_bytes(downloaded)

        # Speed from yt-dlp hook
        speed = info.get("speed")
        if speed and speed > 0:
            speed_str = self._format_speed(speed)
        else:
            speed_str = "--- B/s"

        # ETA from yt-dlp hook
        eta = info.get("eta")
        if eta is not None and eta >= 0:
            eta_str = f"ETA {self._format_time(eta)}"
        else:
            eta_str = "ETA --:--"

        # Fragment info
        frag = info.get("fragment_index")
        frag_count = info.get("fragment_count")
        frag_str = f"frag {frag}/{frag_count}" if frag and frag_count else ""

        parts = [pct_str, f"{dl_str}/{total_str}", speed_str, eta_str]
        if frag_str:
            parts.append(frag_str)
        detail = " │ ".join(parts)

        if fname:
            # Truncate filename if the line is too long
            max_fname = term_width - len(detail) - 6  # 6 for "  ↳  " + padding
            if max_fname > 10 and len(fname) > max_fname:
                fname = fname[: max_fname - 1] + "…"
            return f"  \033[2m↳\033[0m {fname}  {detail}"
        return f"  \033[2m↳\033[0m {detail}"

    def draw_bar(self):
        """Draw per-download progress lines + summary bar at the terminal bottom."""
        if not self._enabled:
            return
        with self._lock:
            elapsed = time.time() - self.start_time
            done = self.completed + self.failed

            # Fractional progress: count finished items + partial progress
            active_frac = 0.0
            for url, downloaded in self._active_downloaded.items():
                total = self._active_totals.get(url)
                if total and total > 0:
                    active_frac += min(downloaded / total, 1.0)
            effective_done = done + active_frac
            pct = (effective_done / self.total * 100) if self.total else 0

            # ETA calculation
            if effective_done > 0 and effective_done < self.total:
                eta_secs = (elapsed / effective_done) * (self.total - effective_done)
                eta = self._format_time(eta_secs)
            else:
                eta = "--:--"

            # Bytes: sum completed + active in-progress
            active_bytes = sum(self._active_downloaded.values())
            total_downloaded = self.bytes_downloaded + active_bytes

            # Speed
            self._update_speed(total_downloaded)
            speed_str = self._format_speed(self._speed)

            # Build the summary bar
            term_size = shutil.get_terminal_size((80, 24))
            term_width = term_size.columns
            term_h = term_size.lines
            bar_width = max(10, min(30, term_width - 80))
            filled = int(bar_width * effective_done / self.total) if self.total else 0
            bar = "█" * filled + "░" * (bar_width - filled)

            status_parts = [
                f"\033[96m{bar}\033[0m",
                f"{pct:5.1f}%",
                f"\033[92m{self.completed}\033[0m done",
            ]
            if self.failed:
                status_parts.append(f"\033[91m{self.failed}\033[0m fail")
            status_parts.extend(
                [
                    f"{self.remaining} left",
                    f"{self._format_bytes(total_downloaded)}",
                    speed_str,
                    f"{self._format_time(elapsed)}",
                    f"ETA {eta}",
                ]
            )
            summary_line = " │ ".join(status_parts)

            # Build per-download progress lines
            active_urls = list(self._active_info.keys())
            download_lines = []
            for url in active_urls[: self._max_active]:
                download_lines.append(self._build_download_line(url, term_width))

        # Render: download lines, then summary bar, all at the bottom
        first_row = term_h - self._reserved_lines + 1
        buf = "\033[s"  # save cursor
        row = first_row
        # Write download lines (fill unused slots with blanks)
        for i in range(self._max_active):
            buf += f"\033[{row};0H\033[K"
            if i < len(download_lines):
                buf += download_lines[i]
            row += 1
        # Write summary bar on the last row
        buf += f"\033[{term_h};0H\033[K{summary_line}"
        buf += "\033[u"  # restore cursor

        self._last_bar = summary_line
        with self._io_lock:
            sys.stdout.write(buf)
            sys.stdout.flush()

    def _format_time(self, seconds):
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


# Global tracker instance (set during batch downloads)
_tracker = None


class _Style:
    """ANSI escape helpers — degrades gracefully when not a TTY."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"

    _enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    @classmethod
    def _c(cls, code, text):
        return f"{code}{text}{cls.RESET}" if cls._enabled else text

    @classmethod
    def _print(cls, msg):
        """Print a message, handling progress bar if active."""
        global _tracker
        if _tracker is not None:
            _tracker.print_live(msg)
        else:
            print(msg)

    @classmethod
    def info(cls, msg):
        cls._print(f"{cls._c(cls.CYAN, 'ℹ')}  {msg}")

    @classmethod
    def success(cls, msg):
        cls._print(f"{cls._c(cls.GREEN, '✔')}  {msg}")

    @classmethod
    def warn(cls, msg):
        cls._print(f"{cls._c(cls.YELLOW, '⚠')}  {msg}")

    @classmethod
    def error(cls, msg):
        cls._print(f"{cls._c(cls.RED, '✖')}  {msg}")

    @classmethod
    def step(cls, msg):
        cls._print(f"{cls._c(cls.BLUE, '▸')}  {msg}")

    @classmethod
    def detail(cls, msg):
        cls._print(f"   {cls._c(cls.DIM, msg)}")

    @classmethod
    def header(cls, msg):
        if cls._enabled:
            cls._print(f"\n{cls.BOLD}{msg}{cls.RESET}")
        else:
            cls._print(f"\n{msg}")

    @classmethod
    def list_item(cls, index, text):
        idx = cls._c(cls.DIM, f"[{index}]")
        cls._print(f"   {idx} {text}")


log = _Style


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------
APP_NAME = "m3u8-extractor"


def _default_config_dir():
    """Return the XDG-compliant config directory for the app.

    Search order: $XDG_CONFIG_HOME/m3u8-extractor, ~/.config/m3u8-extractor.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(xdg, APP_NAME)


def _resolve_default_file(filename):
    """Resolve a config/data file: CWD first, then the user config dir."""
    if os.path.isfile(filename):
        return filename
    user_path = os.path.join(_default_config_dir(), filename)
    if os.path.isfile(user_path):
        return user_path
    # Fall back to CWD name (will produce a clear error if missing)
    return filename


def _expand_paths(paths, extensions, max_depth=None):
    """Expand a list of file/directory paths into individual file paths.

    For each entry in *paths*:
    - If it is a file, include it as-is.
    - If it is a directory, recursively include every file whose extension
      (lower-cased) is in *extensions*.  Files are sorted alphabetically at
      each directory level so that numeric prefixes like ``01-``, ``02-``
      control ordering.
    - Otherwise, include it as-is (downstream code will report the error).

    *extensions* should be a set of lowercased suffixes including the dot,
    e.g. ``{".txt"}`` or ``{".toml"}``.

    *max_depth* controls how deep into subdirectories to recurse:
    - ``None``  – no limit (fully recursive)
    - ``0``     – directory itself only (no subdirectories, same as before)
    - ``1``     – direct children directories, etc.
    """
    expanded = []
    for p in paths:
        if os.path.isdir(p):
            found = _collect_from_dir(p, extensions, max_depth, 0)
            if not found:
                exts = ", ".join(sorted(extensions))
                log.warn(f"No {exts} files found in directory: {p}")
            else:
                expanded.extend(found)
        else:
            expanded.append(p)
    return expanded


def _collect_from_dir(dirpath, extensions, max_depth, current_depth):
    """Recursively collect matching files from *dirpath*.

    Returns a sorted list of absolute/relative file paths.
    """
    results = []
    children = sorted(os.listdir(dirpath))
    for name in children:
        if name.startswith("."):
            continue
        full = os.path.join(dirpath, name)
        if os.path.isfile(full):
            if os.path.splitext(name)[1].lower() in extensions:
                results.append(full)
        elif os.path.isdir(full):
            if max_depth is None or current_depth < max_depth:
                results.extend(_collect_from_dir(full, extensions, max_depth, current_depth + 1))
    return results


DEFAULT_CONFIG_FILE = "config.toml"
DEFAULT_URLS_FILE = "urls.txt"

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------
DEFAULTS = {
    "urls_file": DEFAULT_URLS_FILE,
    "output_path": None,
    "title_prefix": "",
    "title_postfix": "",
    "referrer": None,
    "use_base_url_as_referrer": False,
    "cookies": None,
    "headers": None,  # custom HTTP headers (list or dict)
    "auth": None,  # HTTP basic auth "user:pass"
    "quality": None,
    "transcode": None,
    "yt_dlp_path": None,  # custom path to yt-dlp binary
    "use_system_ytdlp": False,  # use system yt-dlp instead of Python library
    "parallel": "all",  # "all", number, "cores", "logical_cores"
    "m3u8_select": "first",  # "first", "last", "all", or "interactive"
    "m3u8_filter": None,  # regex pattern to filter m3u8 URLs
    "video_filter": None,  # regex pattern to filter direct video URLs
    "stream_type": "both",  # "both", "m3u8", or "video"
    "adblock": False,  # load an adblocker extension in Chrome
    "adblock_extension": None,  # path to a custom .crx adblocker extension
    "adblock_strictness": "complete",  # "basic", "optimal", or "complete"
    "proxy": None,  # proxy for yt-dlp downloads (e.g. socks5://127.0.0.1:1080)
    "browser_proxy": None,  # proxy for the Selenium browser
    "ignore_ssl_errors": False,  # ignore SSL certificate errors
    "user_agent": None,  # custom User-Agent string for requests and browser
    "localstorage": None,  # localStorage key=value pairs to set before page load
    "extractor": "auto",  # "auto", "ytdlp", or "m3u8"
    "extractors": None,  # comma-separated allowlist of yt-dlp extractors
    "use_selenium_session_for_download": False,  # reuse Selenium req headers/cookies for stream URL
    "generic_impersonate": False,  # pass --extractor-args "generic:impersonate"
    # Download-mode flags (all False = default yt-dlp behaviour)
    "thumbnail": False,  # download thumbnail alongside video
    "thumbnail_only": False,
    "captions": False,  # download captions alongside video
    "captions_only": False,
    "audio_only": False,
    "video_only": False,
    "video_and_captions_only": False,
    "overwrite": True,  # overwrite existing files (set False to skip)
    "ytdlp_args": None,  # extra raw arguments forwarded to yt-dlp
    "speed_unit": "bytes",  # "bytes" (KB/s, MB/s) or "bits" (Kbps, Mbps)
    "scan_depth": 0,  # max directory recursion depth (0 = no recursion, None = unlimited)
}

# Map config keys -> environment variable names
ENV_MAP = {
    "urls_file": "M3U8_URLS_FILE",
    "output_path": "M3U8_OUTPUT_PATH",
    "title_prefix": "M3U8_TITLE_PREFIX",
    "title_postfix": "M3U8_TITLE_POSTFIX",
    "referrer": "M3U8_REFERRER",
    "use_base_url_as_referrer": "M3U8_USE_BASE_URL_AS_REFERRER",
    "cookies": "M3U8_COOKIES",
    "headers": "M3U8_HEADERS",
    "auth": "M3U8_AUTH",
    "quality": "M3U8_QUALITY",
    "transcode": "M3U8_TRANSCODE",
    "yt_dlp_path": "M3U8_YT_DLP_PATH",
    "use_system_ytdlp": "M3U8_USE_SYSTEM_YTDLP",
    "parallel": "M3U8_PARALLEL",
    "m3u8_select": "M3U8_SELECT",
    "m3u8_filter": "M3U8_FILTER",
    "video_filter": "M3U8_VIDEO_FILTER",
    "stream_type": "M3U8_STREAM_TYPE",
    "adblock": "M3U8_ADBLOCK",
    "adblock_extension": "M3U8_ADBLOCK_EXTENSION",
    "adblock_strictness": "M3U8_ADBLOCK_STRICTNESS",
    "proxy": "M3U8_PROXY",
    "browser_proxy": "M3U8_BROWSER_PROXY",
    "ignore_ssl_errors": "M3U8_IGNORE_SSL_ERRORS",
    "user_agent": "M3U8_USER_AGENT",
    "localstorage": "M3U8_LOCALSTORAGE",
    "extractor": "M3U8_EXTRACTOR",
    "extractors": "M3U8_EXTRACTORS",
    "use_selenium_session_for_download": "M3U8_USE_SELENIUM_SESSION_FOR_DOWNLOAD",
    "generic_impersonate": "M3U8_GENERIC_IMPERSONATE",
    "thumbnail": "M3U8_THUMBNAIL",
    "thumbnail_only": "M3U8_THUMBNAIL_ONLY",
    "captions": "M3U8_CAPTIONS",
    "captions_only": "M3U8_CAPTIONS_ONLY",
    "audio_only": "M3U8_AUDIO_ONLY",
    "video_only": "M3U8_VIDEO_ONLY",
    "video_and_captions_only": "M3U8_VIDEO_AND_CAPTIONS_ONLY",
    "overwrite": "M3U8_OVERWRITE",
    "ytdlp_args": "M3U8_YTDLP_ARGS",
    "speed_unit": "M3U8_SPEED_UNIT",
    "scan_depth": "M3U8_SCAN_DEPTH",
}

BOOL_KEYS = {
    "use_base_url_as_referrer",
    "use_system_ytdlp",
    "adblock",
    "ignore_ssl_errors",
    "use_selenium_session_for_download",
    "generic_impersonate",
    "thumbnail",
    "thumbnail_only",
    "captions",
    "captions_only",
    "audio_only",
    "video_only",
    "video_and_captions_only",
    "overwrite",
}


# ---------------------------------------------------------------------------
# Configuration loading helpers
# ---------------------------------------------------------------------------
def _parse_bool(value):
    """Parse a string to a boolean."""
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def load_toml_config(path="config.toml"):
    """Load configuration from a TOML file (if it exists)."""
    if tomllib is None:
        return {}
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f) or {}

    # Extract [[url_rules]] into a separate key before normalising
    url_rules = data.pop("url_rules", [])

    # Extract [localstorage] table into the flat key
    localstorage = data.pop("localstorage", None)
    if isinstance(localstorage, dict) and localstorage:
        data["localstorage"] = localstorage

    # Extract [headers] table into the flat key
    headers = data.pop("headers", None)
    if isinstance(headers, dict) and headers:
        data["headers"] = headers

    # Extract [cookies] table into the flat key
    cookies = data.pop("cookies", None)
    if isinstance(cookies, dict) and cookies:
        data["cookies"] = cookies

    # Normalise bools
    for key in BOOL_KEYS:
        if key in data:
            data[key] = _parse_bool(data[key])

    # Normalise bools inside each rule
    for rule in url_rules:
        for key in BOOL_KEYS:
            if key in rule:
                rule[key] = _parse_bool(rule[key])

    data["_url_rules"] = url_rules
    return data


def load_env_config():
    """Load configuration from environment variables."""
    cfg = {}
    for key, env_var in ENV_MAP.items():
        val = os.environ.get(env_var)
        if val is not None:
            if key in BOOL_KEYS:
                cfg[key] = _parse_bool(val)
            else:
                cfg[key] = val
    return cfg


def build_arg_parser():
    """Build the argparse CLI parser."""
    p = argparse.ArgumentParser(
        description="Extract m3u8 URLs from web pages and download with yt-dlp.",
        epilog="Licensed under the GNU Affero General Public License v3.0 only (AGPL-3.0-only).",
    )

    p.add_argument(
        "url",
        nargs="?",
        default=None,
        help="URL to download directly (for one-off downloads)",
    )
    p.add_argument(
        "-f",
        "--urls-file",
        action="append",
        help="Path to file or directory containing URLs (repeatable; "
        "directories load all .txt files recursively, sorted alphabetically; "
        "default: ./urls.txt or ~/.config/m3u8-extractor/urls.txt)",
    )
    p.add_argument(
        "--scan-depth",
        type=int,
        default=None,
        help="Max directory recursion depth when -f or -c points to a directory "
        "(0 = top-level only (default), 1 = one level of subdirectories, -1 = unlimited)",
    )
    p.add_argument("-o", "--output-path", help="Default output directory or filename template")
    p.add_argument("--title-prefix", help="String to prepend to every output filename")
    p.add_argument(
        "--title-postfix",
        help="String to append to every output filename (before extension)",
    )
    p.add_argument("--referrer", help="Referer header to send with requests")
    p.add_argument(
        "--use-base-url-as-referrer",
        action="store_true",
        default=None,
        help="Automatically use each page URL as the Referer header",
    )
    p.add_argument("--cookies", help="Path to a Netscape-format cookies file")
    p.add_argument("--user-agent", help="Custom User-Agent string for yt-dlp and browser requests")
    p.add_argument(
        "--header",
        action="append",
        metavar="NAME=VALUE",
        help="Custom HTTP header (repeatable, e.g. --header 'X-Token=abc')",
    )
    p.add_argument(
        "--auth",
        metavar="USER:PASS",
        help="HTTP basic auth credentials (e.g. 'user:password')",
    )
    p.add_argument(
        "-q",
        "--quality",
        help="yt-dlp format / quality selector (e.g. 'bestvideo+bestaudio')",
    )
    p.add_argument("--transcode", help="Transcode to this format after download (e.g. mp4, mkv)")

    # yt-dlp binary options
    ydlp = p.add_argument_group("yt-dlp binary")
    ydlp.add_argument(
        "--use-system-ytdlp",
        action="store_true",
        default=None,
        help="Use the system yt-dlp binary instead of the Python library",
    )
    ydlp.add_argument("--yt-dlp-path", help="Path to a specific yt-dlp binary")

    # Parallelism
    par = p.add_argument_group("parallelism")
    par.add_argument(
        "-p",
        "--parallel",
        help="Number of parallel downloads: a number, 'all' (default), "
        "'cores' (physical CPU cores), or 'logical_cores'",
    )
    par.add_argument(
        "--speed-unit",
        help="Speed display unit in progress bar: 'bytes' (default, e.g. MB/s) "
        "or 'bits' (e.g. Mbps)",
    )

    # m3u8 / video selection
    m3u8 = p.add_argument_group("stream selection")
    m3u8.add_argument(
        "--stream-type",
        help="Which stream types to look for: "
        "'both' (default), 'm3u8' (only m3u8), or 'video' (only direct files)",
    )
    m3u8.add_argument(
        "--m3u8-select",
        help="Which stream to download when multiple are found: "
        "'first' (default), 'last', 'all', or 'interactive'",
    )
    m3u8.add_argument(
        "--m3u8-filter",
        help="Regex pattern to filter m3u8 URLs (applied before selection)",
    )
    m3u8.add_argument(
        "--video-filter",
        help="Regex pattern to filter direct video URLs (applied before selection)",
    )

    # Adblock
    adb = p.add_argument_group("adblock")
    adb.add_argument(
        "--adblock",
        action="store_true",
        default=None,
        help="Load an adblocker extension in Chrome (uses bundled uBlock Origin Lite by default)",
    )
    adb.add_argument(
        "--adblock-strictness",
        help="Filtering strictness for the built-in adblocker: "
        "'basic', 'optimal', or 'complete' (default)",
    )
    adb.add_argument("--adblock-extension", help="Path to a custom .crx adblocker extension file")

    # Proxy
    prx = p.add_argument_group("proxy")
    prx.add_argument(
        "--proxy",
        help="Proxy for yt-dlp downloads (e.g. http://host:port, socks5://host:port)",
    )
    prx.add_argument(
        "--browser-proxy",
        help="Proxy for the Selenium browser (defaults to --proxy if not set)",
    )

    # SSL
    p.add_argument(
        "--ignore-ssl-errors",
        action="store_true",
        default=None,
        help="Ignore SSL certificate errors in both the browser and yt-dlp",
    )

    # localStorage
    p.add_argument(
        "--localstorage",
        action="append",
        metavar="KEY=VALUE",
        help="Set a localStorage entry before page load "
        "(repeatable, e.g. --localstorage 'jwplayer.qualityLabel=HQ')",
    )

    # Extractor selection
    ext = p.add_argument_group("extractor")
    ext.add_argument(
        "--extractor",
        help="Extraction strategy: 'auto' (default, try yt-dlp native first "
        "then fall back to m3u8), 'ytdlp' (yt-dlp only), "
        "or 'm3u8' (Selenium m3u8 only)",
    )
    ext.add_argument(
        "--extractors",
        help="Comma-separated allowlist of yt-dlp extractor names "
        "(e.g. 'youtube,vimeo'). Only used with 'auto' or 'ytdlp' mode",
    )
    ext.add_argument(
        "--use-selenium-session-for-download",
        action="store_true",
        default=None,
        help="Reuse Selenium request headers/cookies when downloading extracted stream URLs",
    )
    ext.add_argument(
        "--generic-impersonate",
        action="store_true",
        default=None,
        help="Enable yt-dlp generic extractor impersonation "
        "(--extractor-args 'generic:impersonate')",
    )

    # Download-mode flags
    mode = p.add_argument_group("download mode")
    mode.add_argument(
        "--thumbnail",
        action="store_true",
        default=None,
        help="Download the thumbnail alongside the video",
    )
    mode.add_argument(
        "--thumbnail-only",
        action="store_true",
        default=None,
        help="Download only the thumbnail",
    )
    mode.add_argument(
        "--captions",
        action="store_true",
        default=None,
        help="Download captions alongside the video",
    )
    mode.add_argument(
        "--captions-only",
        action="store_true",
        default=None,
        help="Download only the captions",
    )
    mode.add_argument(
        "--audio-only",
        action="store_true",
        default=None,
        help="Download only the audio stream",
    )
    mode.add_argument(
        "--video-only",
        action="store_true",
        default=None,
        help="Download only the video stream (no audio)",
    )
    mode.add_argument(
        "--video-and-captions-only",
        action="store_true",
        default=None,
        help="Download video and captions only (no audio)",
    )
    mode.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Overwrite existing files (default)",
    )
    mode.add_argument(
        "--no-overwrite",
        action="store_true",
        default=None,
        help="Skip download if the output file already exists",
    )

    p.add_argument(
        "--ytdlp-args",
        help="Extra raw arguments forwarded to yt-dlp (e.g. '--limit-rate 1M --retries 10')",
    )

    p.add_argument(
        "-c",
        "--config",
        action="append",
        help="Path to TOML config file or directory (repeatable, later files override; "
        "directories load all .toml files recursively, sorted alphabetically; "
        "default: ./config.toml or ~/.config/m3u8-extractor/config.toml)",
    )

    # Watch mode
    p.add_argument(
        "-w",
        "--watch",
        action="store_true",
        default=False,
        help="Watch the clipboard for URLs and download automatically",
    )
    p.add_argument(
        "--watch-interval",
        type=float,
        default=1.0,
        help="Clipboard polling interval in seconds (default: 1.0)",
    )

    return p


def load_cli_config(args_ns):
    """Convert the argparse Namespace to a config dict (only set keys)."""
    cfg = {}
    mapping = {
        "urls_file": args_ns.urls_file,
        "output_path": args_ns.output_path,
        "title_prefix": args_ns.title_prefix,
        "title_postfix": args_ns.title_postfix,
        "referrer": args_ns.referrer,
        "use_base_url_as_referrer": args_ns.use_base_url_as_referrer,
        "cookies": args_ns.cookies,
        "user_agent": args_ns.user_agent,
        "headers": args_ns.header,
        "auth": args_ns.auth,
        "quality": args_ns.quality,
        "transcode": args_ns.transcode,
        "yt_dlp_path": args_ns.yt_dlp_path,
        "use_system_ytdlp": args_ns.use_system_ytdlp,
        "parallel": args_ns.parallel,
        "m3u8_select": args_ns.m3u8_select,
        "m3u8_filter": args_ns.m3u8_filter,
        "video_filter": args_ns.video_filter,
        "stream_type": args_ns.stream_type,
        "adblock": args_ns.adblock,
        "adblock_strictness": args_ns.adblock_strictness,
        "adblock_extension": args_ns.adblock_extension,
        "proxy": args_ns.proxy,
        "browser_proxy": args_ns.browser_proxy,
        "ignore_ssl_errors": args_ns.ignore_ssl_errors,
        "localstorage": args_ns.localstorage,
        "extractor": args_ns.extractor,
        "extractors": args_ns.extractors,
        "use_selenium_session_for_download": args_ns.use_selenium_session_for_download,
        "generic_impersonate": args_ns.generic_impersonate,
        "thumbnail": args_ns.thumbnail,
        "thumbnail_only": args_ns.thumbnail_only,
        "captions": args_ns.captions,
        "captions_only": args_ns.captions_only,
        "audio_only": args_ns.audio_only,
        "video_only": args_ns.video_only,
        "video_and_captions_only": args_ns.video_and_captions_only,
        "ytdlp_args": args_ns.ytdlp_args,
        "speed_unit": args_ns.speed_unit,
        "scan_depth": args_ns.scan_depth,
    }
    for key, val in mapping.items():
        if val is not None:
            cfg[key] = val

    # Handle --overwrite / --no-overwrite pair
    if getattr(args_ns, "no_overwrite", None):
        cfg["overwrite"] = False
    elif getattr(args_ns, "overwrite", None):
        cfg["overwrite"] = True

    return cfg


def merge_config(cli, env, toml_cfg):
    """Merge configs with priority: CLI > env vars > TOML > defaults."""
    merged = dict(DEFAULTS)
    merged.update(toml_cfg)
    merged.update(env)
    merged.update(cli)
    return merged


def _build_per_url_parser():
    """Build a parser for per-URL and group inline options in the URLs file."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("url", nargs="?", default=None)
    p.add_argument("-o", "--output", dest="output_path")
    p.add_argument("--title-prefix")
    p.add_argument("--title-postfix")
    p.add_argument("--referrer")
    p.add_argument("--use-base-url-as-referrer", action="store_true", default=None)
    p.add_argument("--cookies")
    p.add_argument("--user-agent")
    p.add_argument("--header", dest="headers", action="append")
    p.add_argument("--auth")
    p.add_argument("-q", "--quality")
    p.add_argument("--transcode")
    p.add_argument("--use-system-ytdlp", action="store_true", default=None)
    p.add_argument("--yt-dlp-path")
    p.add_argument("-p", "--parallel")
    p.add_argument("--m3u8-select")
    p.add_argument("--m3u8-filter")
    p.add_argument("--video-filter")
    p.add_argument("--stream-type")
    p.add_argument("--adblock", action="store_true", default=None)
    p.add_argument("--adblock-strictness")
    p.add_argument("--adblock-extension")
    p.add_argument("--proxy")
    p.add_argument("--browser-proxy")
    p.add_argument("--ignore-ssl-errors", action="store_true", default=None)
    p.add_argument("--localstorage", action="append")
    p.add_argument("--extractor")
    p.add_argument("--extractors")
    p.add_argument("--use-selenium-session-for-download", action="store_true", default=None)
    p.add_argument("--generic-impersonate", action="store_true", default=None)
    p.add_argument("--thumbnail", action="store_true", default=None)
    p.add_argument("--thumbnail-only", action="store_true", default=None)
    p.add_argument("--captions", action="store_true", default=None)
    p.add_argument("--captions-only", action="store_true", default=None)
    p.add_argument("--audio-only", action="store_true", default=None)
    p.add_argument("--video-only", action="store_true", default=None)
    p.add_argument("--video-and-captions-only", action="store_true", default=None)
    p.add_argument("--overwrite", action="store_true", default=None)
    p.add_argument("--no-overwrite", action="store_true", default=None)
    p.add_argument("--ytdlp-args")
    p.add_argument("--speed-unit")
    return p


def _normalise_overrides(overrides):
    """Convert negated flags (e.g. no_overwrite) into their canonical form."""
    if overrides.pop("no_overwrite", None):
        overrides["overwrite"] = False
    return overrides


def _parse_url_line(line, per_url_parser):
    """Parse a single line from the URLs file.

    Supports three formats:
        1. URL
        2. URL title-or-path           (legacy: no -- flags)
        3. URL [--flag ...] [-o path]  (rich: any per-URL option)

    Returns (url, per_url_overrides_dict).
    """
    tokens = shlex.split(line)
    # If the line contains any --flag, use the full per-URL parser
    has_flags = any(t.startswith("-") for t in tokens[1:])
    if has_flags:
        args = per_url_parser.parse_args(tokens)
        url = args.url
        overrides = {}
        for key, val in vars(args).items():
            if key == "url" or val is None:
                continue
            overrides[key] = val
        return url, _normalise_overrides(overrides)

    # Legacy format: URL [optional title/path]
    url = tokens[0]
    if len(tokens) > 1:
        return url, {"output_path": " ".join(tokens[1:])}
    return url, {}


def _parse_group_directive(line, per_url_parser):
    """Parse a group directive line (starts with '---').

    Format:  --- [--flag ...]
    Returns a dict of group overrides, or {} to reset.
    """
    rest = line[3:].strip()
    if not rest:
        return {}
    try:
        tokens = shlex.split(rest)
        args = per_url_parser.parse_args(tokens)
        overrides = {}
        for key, val in vars(args).items():
            if key == "url" or val is None:
                continue
            overrides[key] = val
        return _normalise_overrides(overrides)
    except SystemExit:
        log.warn(f"Could not parse group directive: {line}")
        return {}


# ---------------------------------------------------------------------------
# URL rules matching
# ---------------------------------------------------------------------------
def _match_url_rules(url, url_rules):
    """Find all url_rules whose pattern matches the URL and merge them.

    Rules are applied in order, so later rules override earlier ones.
    Each rule is a dict with a 'pattern' key (regex) and any config keys.
    """
    merged = {}
    for rule in url_rules:
        pattern = rule.get("pattern")
        if not pattern:
            continue
        try:
            if re.search(pattern, url):
                overrides = {k: v for k, v in rule.items() if k != "pattern"}
                if overrides:
                    log.detail(f"URL rule matched: '{pattern}'")
                merged.update(overrides)
        except re.error as e:
            log.warn(f"Invalid url_rules pattern '{pattern}': {e}")
    return merged


# ---------------------------------------------------------------------------
# yt-dlp option builder
# ---------------------------------------------------------------------------
def _sanitise_title(title):
    """Remove or replace characters that are illegal in filenames."""
    # Replace path separators and other problematic chars with a dash
    title = re.sub(r"[/\\]", " - ", title)
    # Remove characters illegal on Windows/Linux/macOS: : * ? " < > |
    title = re.sub(r'[:\*\?"<>|]', "", title)
    # Collapse multiple spaces/dashes
    title = re.sub(r"\s{2,}", " ", title).strip()
    title = re.sub(r"-{2,}", "-", title).strip(" -")
    return title or "video"


def _resolve_outtmpl(config, title, output_path_override):
    """Determine the output template string."""
    prefix = config.get("title_prefix", "")
    postfix = config.get("title_postfix", "")
    effective_title = _sanitise_title(f"{prefix}{title}{postfix}")

    out = output_path_override or config.get("output_path")
    if not out:
        return f"{effective_title}.%(ext)s"

    if out.endswith(os.sep) or os.path.isdir(out):
        return os.path.join(out, f"{effective_title}.%(ext)s")

    _, ext = os.path.splitext(out)
    return out if ext else f"{out}.%(ext)s"


def _display_outtmpl(outtmpl):
    """Format yt-dlp output template for human-readable log messages."""
    return str(outtmpl).replace("%(ext)s", "$EXT")


def _apply_format(config, opts):
    """Set the format selector based on download-mode flags."""
    fmt = config.get("quality")
    if config.get("audio_only"):
        fmt = "bestaudio/best"
    elif config.get("video_only") or config.get("video_and_captions_only"):
        fmt = "bestvideo/best"
    if fmt:
        opts["format"] = fmt


def _apply_captions(config, opts):
    """Enable subtitle options when requested."""
    if config.get("captions") or config.get("video_and_captions_only"):
        opts["writesubtitles"] = True
        opts["allsubtitles"] = True
    if config.get("captions_only"):
        opts["writesubtitles"] = True
        opts["allsubtitles"] = True
        opts["skip_download"] = True


def _apply_thumbnails(config, opts):
    """Enable thumbnail options when requested."""
    if config.get("thumbnail"):
        opts["writethumbnail"] = True
    if config.get("thumbnail_only"):
        opts["writethumbnail"] = True
        opts["skip_download"] = True


def build_ydl_opts(config, title, output_path_override=None):
    """Build the yt-dlp options dict from the merged config and per-URL info."""
    outtmpl = _resolve_outtmpl(config, title, output_path_override)

    opts = {
        "outtmpl": outtmpl,
        "quiet": False,
    }

    _apply_format(config, opts)
    _apply_captions(config, opts)
    _apply_thumbnails(config, opts)

    # Transcoding (post-processor)
    transcode = config.get("transcode")
    if transcode:
        opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": transcode}]

    # Referrer
    referrer = config.get("referrer")
    if referrer:
        opts.setdefault("http_headers", {})["Referer"] = referrer

    # User-Agent
    user_agent = config.get("user_agent")
    if user_agent:
        opts.setdefault("http_headers", {})["User-Agent"] = user_agent

    # Browser-captured request headers (from Selenium), if enabled
    browser_headers = _parse_headers_value(config.get("_browser_headers"))
    if browser_headers:
        opts.setdefault("http_headers", {}).update(browser_headers)

    # Cookies
    cookie_file, cookie_header, _cookie_pairs = _resolve_cookie_inputs(config)
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if cookie_header:
        opts.setdefault("http_headers", {})["Cookie"] = cookie_header

    # Custom headers
    headers = _parse_headers_value(config.get("headers"))
    if headers:
        h = opts.setdefault("http_headers", {})
        h.update(headers)

    # HTTP basic auth
    auth = config.get("auth")
    if auth and ":" in str(auth):
        username, password = str(auth).split(":", 1)
        opts["username"] = username
        opts["password"] = password

    # Proxy
    proxy = config.get("proxy")
    if proxy:
        opts["proxy"] = proxy

    # SSL
    if config.get("ignore_ssl_errors"):
        opts["nocheckcertificate"] = True

    # Overwrite
    if not config.get("overwrite", True):
        opts["nooverwrites"] = True

    # Extra yt-dlp arguments (library mode: parse CLI flags into opts dict)
    ytdlp_args = config.get("ytdlp_args")
    if config.get("generic_impersonate"):
        ytdlp_args = f"{ytdlp_args or ''} --extractor-args generic:impersonate".strip()
    if ytdlp_args:
        extra_tokens = shlex.split(ytdlp_args) if isinstance(ytdlp_args, str) else list(ytdlp_args)
        try:
            _, _, _, extra_opts = yt_dlp.parse_options(extra_tokens)
            # Don't let extra args clobber our explicit output template
            extra_opts.pop("outtmpl", None)
            opts.update(extra_opts)
        except Exception as e:
            log.warn(f"Could not parse ytdlp_args for library mode: {e}")

    # Capture yt-dlp log output when our tracker is active
    tracker = _tracker
    if tracker is not None:

        class _TrackerLogger:
            """yt-dlp logger that shows messages live and buffers for replay."""

            _DL_PROGRESS_RE = re.compile(r"^\[download\]\s+\d+(\.\d+)?%\s+(of|at|in)\s")

            def __init__(self, t):
                self._t = t

            def _emit(self, msg):
                if not self._t:
                    return
                # Filter out [download] percentage-progress lines — our
                # tracker already shows that information live.
                if self._DL_PROGRESS_RE.match(msg):
                    return
                self._t.print_live(msg)

            def debug(self, msg):
                self._emit(msg)

            def warning(self, msg):
                self._emit(f"WARNING: {msg}")

            def error(self, msg):
                self._emit(f"ERROR: {msg}")

        opts["logger"] = _TrackerLogger(tracker)

    # Progress tracking (when running in batch/parallel mode)
    if tracker is not None:
        src_url = config.get("_tracker_url", "")

        def _progress_hook(d, _url=src_url, _t=tracker):
            dl_bytes = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if d.get("status") == "downloading":
                info = {
                    "filename": d.get("filename") or d.get("tmpfilename", ""),
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "fragment_index": d.get("fragment_index"),
                    "fragment_count": d.get("fragment_count"),
                }
                _t.update_bytes(_url, dl_bytes, total, info)
                _t.draw_bar()
            elif d.get("status") == "finished":
                _t.update_bytes(_url, dl_bytes, total)

        opts["progress_hooks"] = [_progress_hook]
        # Suppress yt-dlp's own console progress — our tracker handles display
        opts["noprogress"] = True

    return opts, outtmpl


def _build_system_ytdlp_cmd(config, m3u8_url, title, output_path_override=None):
    """Build a command-line list for invoking the system yt-dlp binary."""
    binary = config.get("yt_dlp_path") or "yt-dlp"
    outtmpl = _resolve_outtmpl(config, title, output_path_override)

    cmd = [binary, "-o", outtmpl]

    # Quality / format
    fmt = config.get("quality")
    if config.get("audio_only"):
        fmt = "bestaudio/best"
    elif config.get("video_only") or config.get("video_and_captions_only"):
        fmt = "bestvideo/best"
    if fmt:
        cmd += ["-f", fmt]

    # Captions
    if config.get("captions") or config.get("video_and_captions_only"):
        cmd += ["--write-subs", "--all-subs"]
    if config.get("captions_only"):
        cmd += ["--write-subs", "--all-subs", "--skip-download"]

    # Thumbnails
    if config.get("thumbnail"):
        cmd.append("--write-thumbnail")
    if config.get("thumbnail_only"):
        cmd += ["--write-thumbnail", "--skip-download"]

    # Transcoding
    transcode = config.get("transcode")
    if transcode:
        cmd += ["--recode-video", transcode]

    # Referrer
    referrer = config.get("referrer")
    if referrer:
        cmd += ["--referer", referrer]

    # User-Agent
    user_agent = config.get("user_agent")
    if user_agent:
        cmd += ["--user-agent", user_agent]

    # Browser-captured request headers (from Selenium), if enabled
    browser_headers = _parse_headers_value(config.get("_browser_headers"))
    for hdr_name, hdr_value in browser_headers.items():
        cmd += ["--add-header", f"{hdr_name}:{hdr_value}"]

    # Cookies
    cookie_file, cookie_header, _cookie_pairs = _resolve_cookie_inputs(config)
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    if cookie_header:
        cmd += ["--add-header", f"Cookie:{cookie_header}"]

    # Custom headers
    headers = _parse_headers_value(config.get("headers"))
    for hdr_name, hdr_value in headers.items():
        cmd += ["--add-header", f"{hdr_name}:{hdr_value}"]

    # HTTP basic auth
    auth = config.get("auth")
    if auth and ":" in str(auth):
        username, password = str(auth).split(":", 1)
        cmd += ["--username", username, "--password", password]

    # Proxy
    proxy = config.get("proxy")
    if proxy:
        cmd += ["--proxy", proxy]

    # SSL
    if config.get("ignore_ssl_errors"):
        cmd.append("--no-check-certificates")

    # Overwrite
    if not config.get("overwrite", True):
        cmd.append("--no-overwrites")

    # Suppress yt-dlp's own progress when our tracker is active
    if _tracker is not None:
        cmd.append("--no-progress")

    # Extra yt-dlp arguments (system mode: splice raw tokens before the URL)
    ytdlp_args = config.get("ytdlp_args")
    if config.get("generic_impersonate"):
        ytdlp_args = f"{ytdlp_args or ''} --extractor-args generic:impersonate".strip()
    if ytdlp_args:
        extra_tokens = shlex.split(ytdlp_args) if isinstance(ytdlp_args, str) else list(ytdlp_args)
        cmd.extend(extra_tokens)

    cmd.append(m3u8_url)
    return cmd, outtmpl


# ---------------------------------------------------------------------------
# Adblock helpers
# ---------------------------------------------------------------------------
DEFAULT_ADBLOCK_URL = (
    "https://clients2.google.com/service/update2/crx?"
    "response=redirect&prodversion=126.0&acceptformat=crx2,crx3"
    "&x=id%3Dddkjiahejlhfcafbddmgiahcphecmpfh%26uc"  # uBlock Origin Lite
)
DEFAULT_ADBLOCK_EXT_ID = "ddkjiahejlhfcafbddmgiahcphecmpfh"
_STRICTNESS_LEVELS = {"basic": 1, "optimal": 2, "complete": 3}


def _get_adblock_extension(config):
    """Return the path to the adblock .crx file, downloading if needed."""
    custom = config.get("adblock_extension")
    if custom:
        if os.path.isfile(custom):
            return custom
        log.warn(f"Adblock extension not found at '{custom}'")
        return None

    # Look in default config dir, then CWD
    crx_name = "ublock-origin-lite.crx"
    for candidate in [
        os.path.join(_default_config_dir(), crx_name),
        crx_name,
    ]:
        if os.path.isfile(candidate):
            return candidate

    # Download it
    cache_dir = _default_config_dir()
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, crx_name)
    log.step("Downloading uBlock Origin Lite extension...")
    try:
        import urllib.request

        urllib.request.urlretrieve(DEFAULT_ADBLOCK_URL, dest)
        log.success(f"Saved to {dest}")
        return dest
    except Exception as e:
        log.warn(f"Failed to download adblocker: {e}")
        return None


def _build_chrome_options(config):
    """Build Chrome options, optionally loading an adblocker extension."""
    chrome_options = Options()

    # Enable performance logging to capture network requests
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    use_adblock = config.get("adblock") or config.get("adblock_extension")
    if use_adblock:
        crx_path = _get_adblock_extension(config)
        if crx_path:
            # New headless mode (Chrome 112+) is required for extensions
            chrome_options.add_argument("--headless=new")
            chrome_options.add_extension(crx_path)
            log.info(f"Loaded adblocker: {crx_path}")
            # Browser proxy (falls back to the download proxy if not set)
            browser_proxy = config.get("browser_proxy") or config.get("proxy")
            if browser_proxy:
                chrome_options.add_argument(f"--proxy-server={browser_proxy}")
            if config.get("ignore_ssl_errors"):
                chrome_options.add_argument("--ignore-certificate-errors")

            # User-Agent
            user_agent = config.get("user_agent")
            if user_agent:
                chrome_options.add_argument(f"--user-agent={user_agent}")

            return chrome_options
        log.warn("Continuing without adblock.")

    chrome_options.add_argument("--headless")

    # Browser proxy (falls back to the download proxy if not set separately)
    browser_proxy = config.get("browser_proxy") or config.get("proxy")
    if browser_proxy:
        chrome_options.add_argument(f"--proxy-server={browser_proxy}")

    if config.get("ignore_ssl_errors"):
        chrome_options.add_argument("--ignore-certificate-errors")

    # User-Agent
    user_agent = config.get("user_agent")
    if user_agent:
        chrome_options.add_argument(f"--user-agent={user_agent}")

    return chrome_options


def _apply_adblock_strictness(driver, config):
    """Set the uBlock Origin Lite filtering mode after the extension loads."""
    if not (config.get("adblock") or config.get("adblock_extension")):
        return
    # Only applies to the built-in uBOL; custom extensions manage their own settings
    if config.get("adblock_extension"):
        return

    level_name = str(config.get("adblock_strictness", "optimal")).strip().lower()
    level_num = _STRICTNESS_LEVELS.get(level_name)
    if level_num is None:
        log.warn(
            f"Unknown adblock strictness '{level_name}' "
            f"(expected: {', '.join(_STRICTNESS_LEVELS)}), using 'complete'"
        )
        level_num = _STRICTNESS_LEVELS["complete"]

    if level_num == _STRICTNESS_LEVELS["optimal"]:
        return  # optimal is uBOL's built-in default, nothing to change

    ext_url = f"chrome-extension://{DEFAULT_ADBLOCK_EXT_ID}/dashboard.html"
    try:
        driver.get(ext_url)
        driver.execute_script(
            "return chrome.storage.local.set({defaultFilteringMode: %d})" % level_num
        )
        time.sleep(0.5)  # let the setting propagate
        log.detail(f"Adblock strictness set to '{level_name}'")
    except Exception as e:
        log.warn(f"Could not set adblock strictness: {e}")


def _parse_localstorage_value(raw):
    """Normalise localStorage config into a dict of key→value strings.

    Accepted inputs:
        - dict  {"key": "value", ...}       (from TOML [localstorage] section)
        - list  ["key=value", ...]           (from CLI --localstorage repeated)
        - str   "key=value,key2=value2"      (from env var)
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                out[k.strip()] = v.strip()
        return out
    if isinstance(raw, str):
        out = {}
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    return {}


def _apply_localstorage(driver, config, target_url):
    """Set localStorage entries on the target domain before the real page load."""
    entries = _parse_localstorage_value(config.get("localstorage"))
    if not entries:
        return

    # Navigate to the target origin so localStorage is scoped correctly
    parsed = urlparse(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        driver.get(origin)
        time.sleep(0.5)
        for key, value in entries.items():
            driver.execute_script(
                "localStorage.setItem(arguments[0], arguments[1]);",
                key,
                value,
            )
            log.detail(f"localStorage: {key} = {value}")
    except Exception as e:
        log.warn(f"Could not set localStorage: {e}")


def _parse_headers_value(raw):
    """Normalise custom headers config into a dict of name→value strings.

    Accepted inputs:
        - dict  {"Name": "Value", ...}       (from TOML [headers] section)
        - list  ["Name=Value", ...]           (from CLI --header repeated)
        - str   "Name=Value,Name2=Value2"     (from env var)
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                out[k.strip()] = v.strip()
        return out
    if isinstance(raw, str):
        out = {}
        for pair in raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    return {}


def _parse_cookie_pairs(raw):
    """Normalise cookie config into a dict of cookie-name→value strings."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k).strip(): str(v) for k, v in raw.items() if str(k).strip()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                k = k.strip()
                if k:
                    out[k] = v.strip()
        return out
    if isinstance(raw, str):
        out = {}
        for pair in re.split(r"[;,]", raw):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                k = k.strip()
                if k:
                    out[k] = v.strip()
        return out
    return {}


def _resolve_cookie_inputs(config):
    """Resolve cookie config into (cookie_file, cookie_header, cookie_pairs)."""
    raw = config.get("cookies")
    browser_pairs = _parse_cookie_pairs(config.get("_browser_cookie_pairs"))
    if not raw:
        if not browser_pairs:
            return None, None, {}
        header = "; ".join(f"{k}={v}" for k, v in browser_pairs.items())
        return None, header, browser_pairs

    if isinstance(raw, dict):
        pairs = _parse_cookie_pairs(raw)
        pairs = {**browser_pairs, **pairs}
        if not pairs:
            return None, None, {}
        header = "; ".join(f"{k}={v}" for k, v in pairs.items())
        return None, header, pairs

    if isinstance(raw, str):
        candidate = raw.strip()
        if not candidate:
            if not browser_pairs:
                return None, None, {}
            header = "; ".join(f"{k}={v}" for k, v in browser_pairs.items())
            return None, header, browser_pairs
        if os.path.isfile(candidate):
            if not browser_pairs:
                return candidate, None, {}
            header = "; ".join(f"{k}={v}" for k, v in browser_pairs.items())
            return candidate, header, browser_pairs
        pairs = _parse_cookie_pairs(candidate)
        pairs = {**browser_pairs, **pairs}
        if pairs:
            header = "; ".join(f"{k}={v}" for k, v in pairs.items())
            return None, header, pairs
        if browser_pairs:
            header = "; ".join(f"{k}={v}" for k, v in browser_pairs.items())
            return candidate, header, browser_pairs
        return candidate, None, {}

    pairs = _parse_cookie_pairs(raw)
    pairs = {**browser_pairs, **pairs}
    if not pairs:
        return None, None, {}
    header = "; ".join(f"{k}={v}" for k, v in pairs.items())
    return None, header, pairs


_BROWSER_HEADER_BLOCKLIST = {
    "cookie",
    "content-length",
    "host",
    "connection",
    "accept-encoding",
}


def _sanitise_browser_headers(raw):
    """Keep only safe browser request headers for replay via yt-dlp."""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for name, value in raw.items():
        key = str(name).strip()
        if not key or key.startswith(":"):
            continue
        key_l = key.lower()
        if key_l in _BROWSER_HEADER_BLOCKLIST:
            continue
        if value is None:
            continue
        cleaned[key] = str(value)
    return cleaned


def _header_lookup_for_url(header_map, target_url):
    """Get captured browser request headers for a URL (with path-only fallback)."""
    if target_url in header_map:
        return header_map[target_url]

    target_base = target_url.split("?", 1)[0].split("#", 1)[0]
    for seen_url, seen_headers in header_map.items():
        if seen_url.split("?", 1)[0].split("#", 1)[0] == target_base:
            return seen_headers
    return {}


def _parse_netscape_cookies(filepath):
    """Parse a Netscape-format cookies file into a list of Selenium cookie dicts."""
    cookies = []
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, path, secure, expiry, name, value = parts[:7]
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure.upper() == "TRUE",
                }
                try:
                    exp = int(expiry)
                    if exp > 0:
                        cookie["expiry"] = exp
                except ValueError:
                    pass
                cookies.append(cookie)
    except Exception as e:
        log.warn(f"Could not parse cookies file: {e}")
    return cookies


def _apply_browser_cookies(driver, config, target_url):
    """Load cookies into Selenium from file path or direct cookie pairs."""
    cookie_file, _cookie_header, cookie_pairs = _resolve_cookie_inputs(config)
    if not cookie_file and not cookie_pairs:
        return

    cookies = []
    if cookie_file:
        cookies = _parse_netscape_cookies(cookie_file)

    for name, value in cookie_pairs.items():
        cookies.append({"name": name, "value": value, "path": "/"})

    if not cookies:
        return

    # Navigate to the target origin first so cookies are scoped correctly
    parsed = urlparse(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        driver.get(origin)
        time.sleep(0.5)
        loaded = 0
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
                loaded += 1
            except Exception:
                pass  # cookies for other domains will silently fail
        log.detail(f"Loaded {loaded}/{len(cookies)} cookie(s) into browser")
    except Exception as e:
        log.warn(f"Could not load browser cookies: {e}")


def _apply_browser_headers_and_auth(driver, config):
    """Set custom HTTP headers and basic auth in the browser via CDP."""
    import base64

    headers = _parse_headers_value(config.get("headers"))

    # HTTP basic auth → Authorization header
    auth = config.get("auth")
    if auth and ":" in str(auth):
        encoded = base64.b64encode(str(auth).encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    if not headers:
        return

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": headers})
        names = ", ".join(headers.keys())
        log.detail(f"Browser headers set: {names}")
    except Exception as e:
        log.warn(f"Could not set browser headers: {e}")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
_VIDEO_EXTENSIONS = (
    ".mp4",
    ".webm",
    ".mkv",
    ".avi",
    ".flv",
    ".ts",
    ".mov",
    ".wmv",
    ".mpd",
)


def _extract_urls_from_network_logs(driver):
    """Extract m3u8 and direct video URLs from Chrome's performance logs."""
    m3u8_urls = []
    video_urls = []
    request_headers_by_url = {}
    try:
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                method = msg.get("method", "")
                if method not in (
                    "Network.requestWillBeSent",
                    "Network.responseReceived",
                ):
                    continue
                req_url = ""
                if method == "Network.requestWillBeSent":
                    request = msg["params"].get("request", {})
                    req_url = request.get("url", "")
                    headers = _sanitise_browser_headers(request.get("headers", {}))
                    if req_url and headers:
                        request_headers_by_url[req_url] = headers
                elif method == "Network.responseReceived":
                    req_url = msg["params"].get("response", {}).get("url", "")
                if not req_url:
                    continue
                # Strip query string for extension check
                path = req_url.split("?")[0].split("#")[0]
                if ".m3u8" in path:
                    m3u8_urls.append(req_url)
                elif any(path.endswith(ext) or (ext + "/") in path for ext in _VIDEO_EXTENSIONS):
                    video_urls.append(req_url)
            except (KeyError, json.JSONDecodeError):
                continue
    except Exception as e:
        log.detail(f"Could not read network logs: {e}")
    return m3u8_urls, video_urls, request_headers_by_url


def extract_m3u8(driver, url):
    """Use Selenium to load a page and extract m3u8 and direct video URLs.

    Searches both the rendered page source and intercepted network requests.
    Returns (m3u8_urls, video_urls, page_title, request_headers_by_url).
    """
    driver.get(url)
    time.sleep(5)

    page_title = driver.title.strip()
    page_source = driver.page_source

    # --- m3u8 URLs ---
    m3u8_pattern = r'(https?://[^\s"\'>]+\.m3u8(?:[?#][^\s"\'>]*)?)'
    m3u8_matches = re.findall(m3u8_pattern, page_source)

    # --- Direct video URLs (mp4, webm, etc.) ---
    exts = "|".join(re.escape(e) for e in _VIDEO_EXTENSIONS)
    video_pattern = r'(https?://[^\s"\'>]+(?:' + exts + r')(?:/[^\s"\'>]*)?(?:\?[^\s"\'>]*)?)'
    video_matches = re.findall(video_pattern, page_source)

    # Also check network logs
    net_m3u8, net_video, request_headers_by_url = _extract_urls_from_network_logs(driver)
    m3u8_matches.extend(net_m3u8)
    video_matches.extend(net_video)

    def _dedup_and_fix(urls):
        seen = set()
        unique = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        fixed = []
        for u in unique:
            if u.startswith("t:"):
                u = urljoin(url, u)
                if u.startswith("t:"):
                    u = url + u[2:]
            fixed.append(u)
        return fixed

    return (
        _dedup_and_fix(m3u8_matches),
        _dedup_and_fix(video_matches),
        page_title,
        request_headers_by_url,
    )


def _filter_urls(urls, pattern, label):
    """Apply a regex filter to a list of URLs."""
    if not pattern:
        return urls
    try:
        compiled = re.compile(pattern)
        filtered = [u for u in urls if compiled.search(u)]
        if filtered:
            dropped = len(urls) - len(filtered)
            if dropped:
                log.info(f"{label} filter '{pattern}' matched {len(filtered)}/{len(urls)} URLs")
            return filtered
        log.warn(f"{label} filter '{pattern}' matched nothing — using all {len(urls)} URLs")
        return urls
    except re.error as e:
        log.warn(f"Invalid {label.lower()} filter regex '{pattern}': {e}")
        return urls


def _parse_selection(raw, count):
    """Parse a user selection string into a set of 1-based indices.

    Returns a set of valid indices, or None if the input is invalid.
    """
    selected = set()
    for token in raw.split(","):
        token = token.strip()
        if "-" in token:
            parts = token.split("-", 1)
            try:
                lo, hi = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return None
            if lo < 1 or hi > count or lo > hi:
                return None
            selected.update(range(lo, hi + 1))
        else:
            try:
                n = int(token)
            except ValueError:
                return None
            if n < 1 or n > count:
                return None
            selected.add(n)
    return selected if selected else None


def _interactive_select(urls, label="stream"):
    """Prompt the user to interactively pick which URLs to download."""
    if len(urls) <= 1:
        return urls

    if not sys.stdin.isatty():
        log.warn("Interactive selection unavailable (stdin is not a TTY) — falling back to first")
        return [urls[0]]

    # Pause the progress bar so it doesn't overwrite the prompt
    global _tracker
    paused_tracker = _tracker
    if paused_tracker is not None:
        paused_tracker.reset_scroll_region()
        _tracker = None

    try:
        return _interactive_select_loop(urls, label)
    finally:
        if paused_tracker is not None:
            _tracker = paused_tracker
            _tracker.setup_scroll_region()
            _tracker.draw_bar()


def _interactive_select_loop(urls, label):
    """Inner loop for interactive selection (tracker already paused)."""
    print()
    log.info(f"Select which {label} URL(s) to download:")
    for i, u in enumerate(urls, 1):
        log.list_item(i, u)
    print()

    while True:
        try:
            raw = input(f"Enter number(s) to download (e.g. 1,3 or 1-3 or 'all') [{1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            log.warn("Selection cancelled — falling back to first")
            return [urls[0]]

        if not raw:
            return [urls[0]]

        if raw.lower() == "all":
            return urls

        indices = _parse_selection(raw, len(urls))
        if indices is None:
            log.warn(
                f"Invalid selection '{raw}'. Use numbers between 1"
                f" and {len(urls)}, ranges like 1-3, or 'all'."
            )
            continue

        chosen = [urls[i - 1] for i in sorted(indices)]
        log.info(f"Selected {len(chosen)} {label} URL(s)")
        return chosen


def _select_urls(urls, config, page_url, label="stream"):
    """Select which URLs to download from a list."""
    if not urls:
        return []

    if len(urls) > 1:
        log.warn(f"{len(urls)} {label} URLs found on {page_url}:")
        for i, u in enumerate(urls, 1):
            log.list_item(i, u)

    mode = str(config.get("m3u8_select", "first")).strip().lower()
    if mode == "all":
        log.info(f"Downloading all {len(urls)} {label} URLs")
        return urls
    if mode == "interactive":
        return _interactive_select(urls, label)
    if mode == "last":
        chosen = urls[-1]
        if len(urls) > 1:
            log.info(f"Selected last {label}: {chosen}")
        return [chosen]
    # default: first
    chosen = urls[0]
    if len(urls) > 1:
        log.info(f"Selected first {label}: {chosen}")
    return [chosen]


def _select_m3u8_urls(m3u8_urls, config, page_url):
    """Filter and select m3u8 URLs according to config."""
    urls = _filter_urls(list(m3u8_urls), config.get("m3u8_filter"), "m3u8")
    return _select_urls(urls, config, page_url, "m3u8")


def _select_video_urls(video_urls, config, page_url):
    """Filter and select direct video URLs according to config."""
    urls = _filter_urls(list(video_urls), config.get("video_filter"), "Video")
    return _select_urls(urls, config, page_url, "video")


def _download_m3u8(m3u8_url, effective_config, page_title, output_path_override):
    """Download a single m3u8 URL using either the library or system yt-dlp."""
    use_system = effective_config.get("use_system_ytdlp") or effective_config.get("yt_dlp_path")
    if use_system:
        cmd, outtmpl = _build_system_ytdlp_cmd(
            effective_config, m3u8_url, page_title, output_path_override
        )
        display_out = _display_outtmpl(outtmpl)
        log.step(f"Downloading {display_out} (system yt-dlp)")
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            log.success(f"Completed: {display_out}")
        else:
            log.error(f"yt-dlp exited with code {result.returncode}")
    else:
        ydl_opts, outtmpl = build_ydl_opts(effective_config, page_title, output_path_override)
        display_out = _display_outtmpl(outtmpl)
        log.step(f"Downloading {display_out}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([m3u8_url])
        log.success(f"Completed: {display_out}")


def _try_ytdlp_direct(url, effective_config, output_path_override=None):
    """Try downloading with yt-dlp's native extractors (no Selenium).

    Returns True on success, False if yt-dlp can't handle the URL.
    """
    extractors_csv = effective_config.get("extractors")
    allowed = [e.strip() for e in extractors_csv.split(",") if e.strip()] if extractors_csv else []

    info = _probe_ytdlp(url, effective_config, allowed)
    if not info:
        return False

    title = info.get("title", "video")
    extractor_name = info.get("extractor", "unknown")
    log.info(f"yt-dlp native extractor matched: {extractor_name}")

    return _run_ytdlp_direct(url, effective_config, title, allowed, output_path_override)


def _probe_ytdlp(url, config, allowed):
    """Probe a URL with yt-dlp to see if a native extractor can handle it."""
    opts = {"quiet": True, "no_warnings": True}
    if allowed:
        opts["allowed_extractors"] = allowed
    if config.get("proxy"):
        opts["proxy"] = config["proxy"]
    if config.get("ignore_ssl_errors"):
        opts["nocheckcertificate"] = True
    cookie_file, cookie_header, _cookie_pairs = _resolve_cookie_inputs(config)
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if cookie_header:
        opts.setdefault("http_headers", {})["Cookie"] = cookie_header

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def _run_ytdlp_direct(url, effective_config, title, allowed, output_path_override):
    """Run yt-dlp natively on a URL (after a successful probe)."""
    use_system = effective_config.get("use_system_ytdlp") or effective_config.get("yt_dlp_path")

    if use_system:
        cmd, outtmpl = _build_system_ytdlp_cmd(effective_config, url, title, output_path_override)
        for ext_name in allowed:
            cmd.insert(-1, "--ies")
            cmd.insert(-1, ext_name)

        display_out = _display_outtmpl(outtmpl)
        log.step(f"Downloading {display_out} (system yt-dlp, native extractor)")
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            log.success(f"Completed: {display_out}")
            return True
        log.error(f"yt-dlp exited with code {result.returncode}")
        return False

    ydl_opts, outtmpl = build_ydl_opts(effective_config, title, output_path_override)
    if allowed:
        ydl_opts["allowed_extractors"] = allowed

    display_out = _display_outtmpl(outtmpl)
    log.step(f"Downloading {display_out} (yt-dlp native extractor)")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        log.success(f"Completed: {display_out}")
        return True
    except Exception as e:
        log.warn(f"yt-dlp native download failed: {e}")
        return False


def fetch_m3u8_and_download(url, config, output_path_override=None, per_url_overrides=None):
    """Extract the m3u8 URL from a page and download with yt-dlp.

    Returns (True, None) on success or (False, error_message) on failure.
    """
    # Merge per-URL overrides on top of the global config
    effective_config = dict(config)
    effective_config["_tracker_url"] = url

    # Remove internal keys that shouldn't propagate as yt-dlp options
    url_rules = effective_config.pop("_url_rules", [])

    # Apply URL rules (pattern-matched config from TOML)
    if url_rules:
        rule_overrides = _match_url_rules(url, url_rules)
        if rule_overrides:
            effective_config.update(rule_overrides)

    # Apply per-URL / group overrides on top
    if per_url_overrides:
        effective_config.update(per_url_overrides)

    # output_path from per-URL overrides takes precedence
    if output_path_override is None:
        output_path_override = effective_config.pop("output_path", None)

    # If use_base_url_as_referrer, set referrer from the page URL
    if effective_config.get("use_base_url_as_referrer") and not effective_config.get("referrer"):
        parsed = urlparse(url)
        effective_config["referrer"] = f"{parsed.scheme}://{parsed.netloc}/"

    mode = str(effective_config.get("extractor", "auto")).strip().lower()

    # --- yt-dlp native extraction ("auto" or "ytdlp") ---
    if mode in ("auto", "ytdlp"):
        log.step(f"Trying yt-dlp native extraction for {url}")
        success = _try_ytdlp_direct(url, effective_config, output_path_override)
        if success:
            return True, None
        if mode == "ytdlp":
            msg = f"yt-dlp could not extract from {url} (extractor mode: ytdlp)"
            log.error(msg)
            return False, msg
        log.info("Falling back to Selenium m3u8 extraction...")

    # --- Selenium m3u8 extraction ("auto" fallback or "m3u8") ---
    chrome_options = _build_chrome_options(effective_config)
    driver = webdriver.Chrome(options=chrome_options)
    _apply_adblock_strictness(driver, effective_config)
    _apply_browser_headers_and_auth(driver, effective_config)
    _apply_browser_cookies(driver, effective_config, url)
    _apply_localstorage(driver, effective_config, url)

    try:
        m3u8_urls, video_urls, page_title, request_headers_by_url = extract_m3u8(driver, url)

        browser_cookie_pairs = {}
        if effective_config.get("use_selenium_session_for_download"):
            try:
                for cookie in driver.get_cookies():
                    name = str(cookie.get("name", "")).strip()
                    value = str(cookie.get("value", ""))
                    if name:
                        browser_cookie_pairs[name] = value
            except Exception as e:
                log.detail(f"Could not read Selenium cookies: {e}")

        # Apply stream_type preference
        stype = str(effective_config.get("stream_type", "both")).strip().lower()
        if stype == "m3u8":
            video_urls = []
        elif stype == "video":
            m3u8_urls = []

        if not m3u8_urls and not video_urls:
            msg = f"No matching stream URL found on {url}"
            log.warn(msg)
            return False, msg

        if m3u8_urls:
            selected = _select_m3u8_urls(m3u8_urls, effective_config, url)
            for m3u8_url in selected:
                log.info(f"Found m3u8: {m3u8_url}")
                download_config = effective_config
                if effective_config.get("use_selenium_session_for_download"):
                    download_config = dict(effective_config)
                    download_config["_browser_headers"] = _header_lookup_for_url(
                        request_headers_by_url, m3u8_url
                    )
                    download_config["_browser_cookie_pairs"] = dict(browser_cookie_pairs)
                _download_m3u8(m3u8_url, download_config, page_title, output_path_override)
        else:
            log.info(f"Found {len(video_urls)} direct video URL(s)")
            selected = _select_video_urls(video_urls, effective_config, url)
            for vid_url in selected:
                log.info(f"Found video: {vid_url}")
                download_config = effective_config
                if effective_config.get("use_selenium_session_for_download"):
                    download_config = dict(effective_config)
                    download_config["_browser_headers"] = _header_lookup_for_url(
                        request_headers_by_url, vid_url
                    )
                    download_config["_browser_cookie_pairs"] = dict(browser_cookie_pairs)
                _download_m3u8(vid_url, download_config, page_title, output_path_override)

        return True, None

    except Exception as e:
        msg = str(e)
        log.error(f"An error occurred: {msg}")
        return False, msg
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _resolve_worker_count(value, num_entries):
    """Resolve the parallel worker count from config value.

    Accepted values:
        - "all"           → one worker per entry (unlimited)
        - "cores"         → number of physical CPU cores
        - "logical_cores" → number of logical CPU cores (os.cpu_count())
        - an integer       → that exact number
    """
    if value is None:
        value = "all"
    val = str(value).strip().lower()
    if val == "all":
        return num_entries
    if val == "cores":
        try:
            count = len(os.sched_getaffinity(0))
        except AttributeError:
            count = os.cpu_count() or 1
        return count
    if val in ("logical_cores", "logical"):
        return os.cpu_count() or 1
    try:
        n = int(val)
        return max(1, n)
    except ValueError:
        log.warn(f"Unrecognised parallel value '{value}', defaulting to all")
        return num_entries


def _format_duration(seconds):
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def _print_summary(results, elapsed):
    """Print a summary of download results.

    results: list of (url, success: bool, error: str|None)
    elapsed: total time in seconds
    """
    succeeded = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    lines = [
        "Summary",
        f"{_Style._c(_Style.CYAN, 'ℹ')}  Total time: {_format_duration(elapsed)}",
        f"{_Style._c(_Style.GREEN, '✔')}  {len(succeeded)} succeeded",
    ]

    if failed:
        lines.append(f"{_Style._c(_Style.RED, '✖')}  {len(failed)} failed")
        for url, _, error in failed:
            reason = error or "unknown error"
            lines.append(f"   {_Style._c(_Style.DIM, url)}")
            lines.append(f"   {_Style._c(_Style.DIM, f'└ {reason}')}")
    else:
        lines.append(f"{_Style._c(_Style.GREEN, '✔')}  No failures")

    # Write as a clean block: clear line before every line to avoid stale suffixes.
    sys.stdout.write("\033[0m\n")
    for line in lines:
        sys.stdout.write(f"\r\033[K{line}\n")
    sys.stdout.flush()


def download_from_file(file_paths, config):
    """Read URLs from one or more files and download each one.

    Supports group directives to set options for blocks of URLs:
        --- --audio-only --quality best
        https://example.com/song1
        https://example.com/song2
        ---
        https://example.com/video   # group reset, back to global defaults
    """
    if isinstance(file_paths, str):
        file_paths = [file_paths]

    per_url_parser = _build_per_url_parser()
    try:
        entries = []

        for file_path in file_paths:
            try:
                with open(file_path, "r") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                log.error(f"File not found: '{file_path}'")
                continue

            # Reset group overrides between files
            group_overrides = {}

            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # Group directive: --- [options]
                if line.startswith("---"):
                    group_overrides = _parse_group_directive(line, per_url_parser)
                    if group_overrides:
                        flags = " ".join(f"{k}={v}" for k, v in group_overrides.items())
                        log.info(f"Group options set: {flags}")
                    else:
                        log.info("Group options reset")
                    continue

                try:
                    url, url_overrides = _parse_url_line(line, per_url_parser)
                    # Merge: group options first, then per-URL overrides on top
                    merged = dict(group_overrides)
                    merged.update(url_overrides)
                    entries.append((url, merged))
                except SystemExit:
                    log.warn(f"Could not parse line: {line}")
                    continue

        if not entries:
            log.warn("No URLs found in the file(s).")
            return

        workers = _resolve_worker_count(config.get("parallel"), len(entries))
        log.header(
            f"Downloading {len(entries)} URL{'s' if len(entries) != 1 else ''} "
            f"with {workers} worker{'s' if workers != 1 else ''}"
        )

        results = []
        start_time = time.time()
        global _tracker

        if workers <= 1 or len(entries) == 1:
            _tracker = _ProgressTracker(
                len(entries),
                speed_unit=config.get("speed_unit", "bytes"),
                max_active=1,
            )
            _tracker.setup_scroll_region()
            for i, (url, overrides) in enumerate(entries, 1):
                log.step(f"[{i}/{len(entries)}] {url}")
                try:
                    ok, err = fetch_m3u8_and_download(url, config, per_url_overrides=overrides)
                    results.append((url, ok, err))
                    if ok:
                        _tracker.record_success()
                    else:
                        _tracker.record_failure()
                except Exception as exc:
                    log.error(f"Failed: {url} — {exc}")
                    results.append((url, False, str(exc)))
                    _tracker.record_failure()
                finally:
                    _tracker.finish_bytes(url)
                    _tracker.draw_bar()
            tracker_ref = _tracker
            tracker_ref.reset_scroll_region()
            _tracker = None
            tracker_ref.flush_buffer()
        else:
            _tracker = _ProgressTracker(
                len(entries),
                speed_unit=config.get("speed_unit", "bytes"),
                max_active=workers,
            )
            _tracker.setup_scroll_region()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(fetch_m3u8_and_download, url, config, None, overrides): url
                    for url, overrides in entries
                }
                for future in as_completed(futures):
                    src_url = futures[future]
                    try:
                        ok, err = future.result()
                        results.append((src_url, ok, err))
                        if ok:
                            _tracker.record_success()
                        else:
                            _tracker.record_failure()
                    except Exception as exc:
                        log.error(f"Failed: {src_url} — {exc}")
                        results.append((src_url, False, str(exc)))
                        _tracker.record_failure()
                    finally:
                        _tracker.finish_bytes(src_url)
                        _tracker.draw_bar()
            tracker_ref = _tracker
            tracker_ref.reset_scroll_region()
            _tracker = None
            tracker_ref.flush_buffer()

        elapsed = time.time() - start_time
        _print_summary(results, elapsed)

    except Exception as e:
        log.error(f"An error occurred while reading the file(s): {e}")


# ---------------------------------------------------------------------------
# Clipboard watch mode
# ---------------------------------------------------------------------------
def _read_clipboard():
    """Read the current clipboard text. Returns empty string on failure."""
    # Try platform-specific commands
    for cmd in (
        "xclip -selection clipboard -o",
        "xsel --clipboard --output",
        "pbpaste",
        "powershell.exe -command Get-Clipboard",
    ):
        try:
            result = subprocess.run(
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return ""


def _looks_like_url(text):
    """Quick check if text looks like a URL worth trying."""
    return bool(text) and re.match(r"https?://", text.strip().split("\n")[0])


def watch_clipboard(config, interval=1.0):
    """Poll the clipboard for new URLs and download them automatically."""
    log.header("Watching clipboard for URLs  (Ctrl+C to stop)")
    seen = set()
    last_text = ""

    # Prime with current clipboard so we don't immediately download
    # whatever is already there
    last_text = _read_clipboard()
    if _looks_like_url(last_text):
        seen.add(last_text.strip().split("\n")[0])

    try:
        while True:
            time.sleep(interval)
            text = _read_clipboard()
            if not text or text == last_text:
                continue
            last_text = text

            # Extract the first line as the URL
            url = text.strip().split("\n")[0].strip()
            if not _looks_like_url(url):
                continue
            if url in seen:
                continue

            seen.add(url)
            log.info(f"Clipboard URL detected: {url}")
            try:
                ok, err = fetch_m3u8_and_download(url, config)
                if not ok:
                    log.error(f"Failed: {url} — {err}")
            except Exception as exc:
                log.error(f"Failed: {url} — {exc}")

    except KeyboardInterrupt:
        log.header(f"Stopped. Downloaded {len(seen)} URL(s).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # Resolve config file(s): explicit flag > CWD > user config dir
    config_paths = args.config or [_resolve_default_file(DEFAULT_CONFIG_FILE)]
    config_paths_depth = 0  # default: no recursion into subdirectories
    # If --scan-depth was given on the CLI, use it for config expansion too
    if args.scan_depth is not None:
        config_paths_depth = args.scan_depth if args.scan_depth >= 0 else None
    config_paths = _expand_paths(config_paths, {".toml"}, max_depth=config_paths_depth)
    toml_cfg = {}
    _DICT_MERGE_KEYS = {"headers", "cookies", "localstorage"}
    for _cfg_path in config_paths:
        _one = load_toml_config(_cfg_path)
        # Combine url_rules across configs rather than replacing
        if "_url_rules" in _one and "_url_rules" in toml_cfg:
            toml_cfg["_url_rules"] = toml_cfg["_url_rules"] + _one.pop("_url_rules")
        # Deep-merge dict-type keys so entries accumulate across files
        for _dk in _DICT_MERGE_KEYS:
            if _dk in _one and isinstance(_one[_dk], dict):
                if _dk in toml_cfg and isinstance(toml_cfg[_dk], dict):
                    merged = dict(toml_cfg[_dk])
                    merged.update(_one.pop(_dk))
                    toml_cfg[_dk] = merged
        toml_cfg.update(_one)

    env_cfg = load_env_config()
    cli_cfg = load_cli_config(args)

    config = merge_config(cli_cfg, env_cfg, toml_cfg)

    if args.watch:
        watch_clipboard(config, interval=args.watch_interval)
    elif args.url:
        # One-off download: use the URL directly
        start_time = time.time()
        try:
            ok, err = fetch_m3u8_and_download(args.url, config)
            results = [(args.url, ok, err)]
        except Exception as exc:
            results = [(args.url, False, str(exc))]
        elapsed = time.time() - start_time
        _print_summary(results, elapsed)
    else:
        # Batch download from file(s)
        urls_files = config.get("urls_file", DEFAULT_URLS_FILE)
        if isinstance(urls_files, str):
            urls_files = [urls_files]
        resolved = [_resolve_default_file(f) for f in urls_files]
        scan_depth = config.get("scan_depth", 0)
        scan_depth = int(scan_depth)
        if scan_depth < 0:
            scan_depth = None
        resolved = _expand_paths(resolved, {".txt"}, max_depth=scan_depth)
        download_from_file(resolved, config)


if __name__ == "__main__":
    main()
