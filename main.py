import os
import sys
import time
import re
import json
import shlex
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import yt_dlp
from urllib.parse import urljoin, urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
class _Style:
    """ANSI escape helpers — degrades gracefully when not a TTY."""

    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"

    _enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    @classmethod
    def _c(cls, code, text):
        return f"{code}{text}{cls.RESET}" if cls._enabled else text

    @classmethod
    def info(cls, msg):
        print(f"{cls._c(cls.CYAN, 'ℹ')}  {msg}")

    @classmethod
    def success(cls, msg):
        print(f"{cls._c(cls.GREEN, '✔')}  {msg}")

    @classmethod
    def warn(cls, msg):
        print(f"{cls._c(cls.YELLOW, '⚠')}  {msg}")

    @classmethod
    def error(cls, msg):
        print(f"{cls._c(cls.RED, '✖')}  {msg}")

    @classmethod
    def step(cls, msg):
        print(f"{cls._c(cls.BLUE, '▸')}  {msg}")

    @classmethod
    def detail(cls, msg):
        print(f"   {cls._c(cls.DIM, msg)}")

    @classmethod
    def header(cls, msg):
        if cls._enabled:
            print(f"\n{cls.BOLD}{msg}{cls.RESET}")
        else:
            print(f"\n{msg}")

    @classmethod
    def list_item(cls, index, text):
        idx = cls._c(cls.DIM, f"[{index}]")
        print(f"   {idx} {text}")


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
    "headers": None,              # custom HTTP headers (list or dict)
    "auth": None,                 # HTTP basic auth "user:pass"
    "quality": None,
    "transcode": None,
    "yt_dlp_path": None,         # custom path to yt-dlp binary
    "use_system_ytdlp": False,   # use system yt-dlp instead of Python library
    "parallel": "all",           # "all", number, "cores", "logical_cores"
    "m3u8_select": "first",       # "first", "last", or "all"
    "m3u8_filter": None,          # regex pattern to filter m3u8 URLs
    "video_filter": None,         # regex pattern to filter direct video URLs
    "stream_type": "both",        # "both", "m3u8", or "video"
    "adblock": False,             # load an adblocker extension in Chrome
    "adblock_extension": None,    # path to a custom .crx adblocker extension
    "adblock_strictness": "complete",  # "basic", "optimal", or "complete"
    "proxy": None,                # proxy for yt-dlp downloads (e.g. socks5://127.0.0.1:1080)
    "browser_proxy": None,        # proxy for the Selenium browser
    "ignore_ssl_errors": False,   # ignore SSL certificate errors
    "localstorage": None,           # localStorage key=value pairs to set before page load
    "extractor": "auto",            # "auto", "ytdlp", or "m3u8"
    "extractors": None,             # comma-separated allowlist of yt-dlp extractors
    # Download-mode flags (all False = default yt-dlp behaviour)
    "thumbnail": False,          # download thumbnail alongside video
    "thumbnail_only": False,
    "captions": False,           # download captions alongside video
    "captions_only": False,
    "audio_only": False,
    "video_only": False,
    "video_and_captions_only": False,
}

# Map config keys -> environment variable names
ENV_MAP = {
    "urls_file":                "M3U8_URLS_FILE",
    "output_path":              "M3U8_OUTPUT_PATH",
    "title_prefix":             "M3U8_TITLE_PREFIX",
    "title_postfix":            "M3U8_TITLE_POSTFIX",
    "referrer":                 "M3U8_REFERRER",
    "use_base_url_as_referrer": "M3U8_USE_BASE_URL_AS_REFERRER",
    "cookies":                  "M3U8_COOKIES",
    "headers":                  "M3U8_HEADERS",
    "auth":                     "M3U8_AUTH",
    "quality":                  "M3U8_QUALITY",
    "transcode":                "M3U8_TRANSCODE",
    "yt_dlp_path":              "M3U8_YT_DLP_PATH",
    "use_system_ytdlp":         "M3U8_USE_SYSTEM_YTDLP",
    "parallel":                  "M3U8_PARALLEL",
    "m3u8_select":               "M3U8_SELECT",
    "m3u8_filter":               "M3U8_FILTER",
    "video_filter":              "M3U8_VIDEO_FILTER",
    "stream_type":               "M3U8_STREAM_TYPE",
    "adblock":                   "M3U8_ADBLOCK",
    "adblock_extension":         "M3U8_ADBLOCK_EXTENSION",
    "adblock_strictness":        "M3U8_ADBLOCK_STRICTNESS",
    "proxy":                     "M3U8_PROXY",
    "browser_proxy":             "M3U8_BROWSER_PROXY",
    "ignore_ssl_errors":         "M3U8_IGNORE_SSL_ERRORS",
    "localstorage":              "M3U8_LOCALSTORAGE",
    "extractor":                 "M3U8_EXTRACTOR",
    "extractors":                "M3U8_EXTRACTORS",
    "thumbnail":                "M3U8_THUMBNAIL",
    "thumbnail_only":           "M3U8_THUMBNAIL_ONLY",
    "captions":                 "M3U8_CAPTIONS",
    "captions_only":            "M3U8_CAPTIONS_ONLY",
    "audio_only":               "M3U8_AUDIO_ONLY",
    "video_only":               "M3U8_VIDEO_ONLY",
    "video_and_captions_only":  "M3U8_VIDEO_AND_CAPTIONS_ONLY",
}

BOOL_KEYS = {
    "use_base_url_as_referrer", "use_system_ytdlp", "adblock",
    "ignore_ssl_errors", "thumbnail", "thumbnail_only",
    "captions", "captions_only", "audio_only", "video_only",
    "video_and_captions_only",
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

    p.add_argument("url", nargs="?", default=None,
                   help="URL to download directly (for one-off downloads)")
    p.add_argument("-f", "--urls-file",
                   help="Path to file containing URLs "
                        "(default: ./urls.txt or ~/.config/m3u8-extractor/urls.txt)")
    p.add_argument("-o", "--output-path",
                   help="Default output directory or filename template")
    p.add_argument("--title-prefix",
                   help="String to prepend to every output filename")
    p.add_argument("--title-postfix",
                   help="String to append to every output filename (before extension)")
    p.add_argument("--referrer",
                   help="Referer header to send with requests")
    p.add_argument("--use-base-url-as-referrer", action="store_true", default=None,
                   help="Automatically use each page URL as the Referer header")
    p.add_argument("--cookies",
                   help="Path to a Netscape-format cookies file")
    p.add_argument("--header", action="append", metavar="NAME=VALUE",
                   help="Custom HTTP header (repeatable, e.g. --header 'X-Token=abc')")
    p.add_argument("--auth", metavar="USER:PASS",
                   help="HTTP basic auth credentials (e.g. 'user:password')")
    p.add_argument("-q", "--quality",
                   help="yt-dlp format / quality selector (e.g. 'bestvideo+bestaudio')")
    p.add_argument("--transcode",
                   help="Transcode to this format after download (e.g. mp4, mkv)")

    # yt-dlp binary options
    ydlp = p.add_argument_group("yt-dlp binary")
    ydlp.add_argument("--use-system-ytdlp", action="store_true", default=None,
                      help="Use the system yt-dlp binary instead of the Python library")
    ydlp.add_argument("--yt-dlp-path",
                      help="Path to a specific yt-dlp binary")

    # Parallelism
    par = p.add_argument_group("parallelism")
    par.add_argument("-p", "--parallel",
                     help="Number of parallel downloads: a number, 'all' (default), "
                          "'cores' (physical CPU cores), or 'logical_cores'")

    # m3u8 / video selection
    m3u8 = p.add_argument_group("stream selection")
    m3u8.add_argument("--stream-type",
                      help="Which stream types to look for: "
                           "'both' (default), 'm3u8' (only m3u8), or 'video' (only direct files)")
    m3u8.add_argument("--m3u8-select",
                      help="Which stream to download when multiple are found: "
                           "'first' (default), 'last', or 'all'")
    m3u8.add_argument("--m3u8-filter",
                      help="Regex pattern to filter m3u8 URLs (applied before selection)")
    m3u8.add_argument("--video-filter",
                      help="Regex pattern to filter direct video URLs (applied before selection)")

    # Adblock
    adb = p.add_argument_group("adblock")
    adb.add_argument("--adblock", action="store_true", default=None,
                     help="Load an adblocker extension in Chrome "
                          "(uses bundled uBlock Origin Lite by default)")
    adb.add_argument("--adblock-strictness",
                     help="Filtering strictness for the built-in adblocker: "
                          "'basic', 'optimal', or 'complete' (default)")
    adb.add_argument("--adblock-extension",
                     help="Path to a custom .crx adblocker extension file")

    # Proxy
    prx = p.add_argument_group("proxy")
    prx.add_argument("--proxy",
                     help="Proxy for yt-dlp downloads "
                          "(e.g. http://host:port, socks5://host:port)")
    prx.add_argument("--browser-proxy",
                     help="Proxy for the Selenium browser "
                          "(defaults to --proxy if not set)")

    # SSL
    p.add_argument("--ignore-ssl-errors", action="store_true", default=None,
                   help="Ignore SSL certificate errors in both the browser and yt-dlp")

    # localStorage
    p.add_argument("--localstorage", action="append", metavar="KEY=VALUE",
                   help="Set a localStorage entry before page load "
                        "(repeatable, e.g. --localstorage 'jwplayer.qualityLabel=HQ')")

    # Extractor selection
    ext = p.add_argument_group("extractor")
    ext.add_argument("--extractor",
                     help="Extraction strategy: 'auto' (default, try yt-dlp native first "
                          "then fall back to m3u8), 'ytdlp' (yt-dlp only), "
                          "or 'm3u8' (Selenium m3u8 only)")
    ext.add_argument("--extractors",
                     help="Comma-separated allowlist of yt-dlp extractor names "
                          "(e.g. 'youtube,vimeo'). Only used with 'auto' or 'ytdlp' mode")

    # Download-mode flags
    mode = p.add_argument_group("download mode")
    mode.add_argument("--thumbnail", action="store_true", default=None,
                      help="Download the thumbnail alongside the video")
    mode.add_argument("--thumbnail-only", action="store_true", default=None,
                      help="Download only the thumbnail")
    mode.add_argument("--captions", action="store_true", default=None,
                      help="Download captions alongside the video")
    mode.add_argument("--captions-only", action="store_true", default=None,
                      help="Download only the captions")
    mode.add_argument("--audio-only", action="store_true", default=None,
                      help="Download only the audio stream")
    mode.add_argument("--video-only", action="store_true", default=None,
                      help="Download only the video stream (no audio)")
    mode.add_argument("--video-and-captions-only", action="store_true", default=None,
                      help="Download video and captions only (no audio)")

    p.add_argument("-c", "--config",
                   help="Path to TOML config file "
                        "(default: ./config.toml or ~/.config/m3u8-extractor/config.toml)")

    # Watch mode
    p.add_argument("-w", "--watch", action="store_true", default=False,
                   help="Watch the clipboard for URLs and download automatically")
    p.add_argument("--watch-interval", type=float, default=1.0,
                   help="Clipboard polling interval in seconds (default: 1.0)")

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
        "thumbnail": args_ns.thumbnail,
        "thumbnail_only": args_ns.thumbnail_only,
        "captions": args_ns.captions,
        "captions_only": args_ns.captions_only,
        "audio_only": args_ns.audio_only,
        "video_only": args_ns.video_only,
        "video_and_captions_only": args_ns.video_and_captions_only,
    }
    for key, val in mapping.items():
        if val is not None:
            cfg[key] = val
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
    p.add_argument("--thumbnail", action="store_true", default=None)
    p.add_argument("--thumbnail-only", action="store_true", default=None)
    p.add_argument("--captions", action="store_true", default=None)
    p.add_argument("--captions-only", action="store_true", default=None)
    p.add_argument("--audio-only", action="store_true", default=None)
    p.add_argument("--video-only", action="store_true", default=None)
    p.add_argument("--video-and-captions-only", action="store_true", default=None)
    return p


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
        return url, overrides

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
        return overrides
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
def _resolve_outtmpl(config, title, output_path_override):
    """Determine the output template string."""
    prefix = config.get("title_prefix", "")
    postfix = config.get("title_postfix", "")
    effective_title = f"{prefix}{title}{postfix}"

    out = output_path_override or config.get("output_path")
    if not out:
        return f"{effective_title}.%(ext)s"

    if out.endswith(os.sep) or os.path.isdir(out):
        return os.path.join(out, f"{effective_title}.%(ext)s")

    _, ext = os.path.splitext(out)
    return out if ext else f"{out}.%(ext)s"


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
        opts["postprocessors"] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": transcode}
        ]

    # Referrer
    referrer = config.get("referrer")
    if referrer:
        opts.setdefault("http_headers", {})["Referer"] = referrer

    # Cookies
    cookies = config.get("cookies")
    if cookies:
        opts["cookiefile"] = cookies

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

    # Cookies
    cookies = config.get("cookies")
    if cookies:
        cmd += ["--cookies", cookies]

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
            return chrome_options
        log.warn("Continuing without adblock.")

    chrome_options.add_argument("--headless")

    # Browser proxy (falls back to the download proxy if not set separately)
    browser_proxy = config.get("browser_proxy") or config.get("proxy")
    if browser_proxy:
        chrome_options.add_argument(f"--proxy-server={browser_proxy}")

    if config.get("ignore_ssl_errors"):
        chrome_options.add_argument("--ignore-certificate-errors")

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
        log.warn(f"Unknown adblock strictness '{level_name}' "
                 f"(expected: {', '.join(_STRICTNESS_LEVELS)}), using 'complete'")
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
                key, value,
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
    """Load cookies from a Netscape file into the Selenium browser."""
    cookie_file = config.get("cookies")
    if not cookie_file:
        return
    cookies = _parse_netscape_cookies(cookie_file)
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
    ".mp4", ".webm", ".mkv", ".avi", ".flv", ".ts", ".mov", ".wmv", ".mpd",
)


def _extract_urls_from_network_logs(driver):
    """Extract m3u8 and direct video URLs from Chrome's performance logs."""
    m3u8_urls = []
    video_urls = []
    try:
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                method = msg.get("method", "")
                if method not in ("Network.requestWillBeSent",
                                  "Network.responseReceived"):
                    continue
                req_url = ""
                if method == "Network.requestWillBeSent":
                    req_url = msg["params"].get("request", {}).get("url", "")
                elif method == "Network.responseReceived":
                    req_url = msg["params"].get("response", {}).get("url", "")
                if not req_url:
                    continue
                # Strip query string for extension check
                path = req_url.split("?")[0].split("#")[0]
                if ".m3u8" in path:
                    m3u8_urls.append(req_url)
                elif any(path.endswith(ext) or (ext + "/") in path
                         for ext in _VIDEO_EXTENSIONS):
                    video_urls.append(req_url)
            except (KeyError, json.JSONDecodeError):
                continue
    except Exception as e:
        log.detail(f"Could not read network logs: {e}")
    return m3u8_urls, video_urls


def extract_m3u8(driver, url):
    """Use Selenium to load a page and extract m3u8 and direct video URLs.

    Searches both the rendered page source and intercepted network requests.
    Returns (m3u8_urls, video_urls, page_title).
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
    net_m3u8, net_video = _extract_urls_from_network_logs(driver)
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

    return _dedup_and_fix(m3u8_matches), _dedup_and_fix(video_matches), page_title


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
                log.info(f"{label} filter '{pattern}' matched "
                         f"{len(filtered)}/{len(urls)} URLs")
            return filtered
        log.warn(f"{label} filter '{pattern}' matched nothing — using all {len(urls)} URLs")
        return urls
    except re.error as e:
        log.warn(f"Invalid {label.lower()} filter regex '{pattern}': {e}")
        return urls


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
        log.step(f"Downloading {outtmpl} (system yt-dlp)")
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            log.success(f"Completed: {outtmpl}")
        else:
            log.error(f"yt-dlp exited with code {result.returncode}")
    else:
        ydl_opts, outtmpl = build_ydl_opts(
            effective_config, page_title, output_path_override
        )
        log.step(f"Downloading {outtmpl}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([m3u8_url])
        log.success(f"Completed: {outtmpl}")


def _try_ytdlp_direct(url, effective_config, output_path_override=None):
    """Try downloading with yt-dlp's native extractors (no Selenium).

    Returns True on success, False if yt-dlp can't handle the URL.
    """
    extractors_csv = effective_config.get("extractors")
    allowed = (
        [e.strip() for e in extractors_csv.split(",") if e.strip()]
        if extractors_csv else []
    )

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
    if config.get("cookies"):
        opts["cookiefile"] = config["cookies"]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def _run_ytdlp_direct(url, effective_config, title, allowed, output_path_override):
    """Run yt-dlp natively on a URL (after a successful probe)."""
    use_system = effective_config.get("use_system_ytdlp") or effective_config.get("yt_dlp_path")

    if use_system:
        cmd, outtmpl = _build_system_ytdlp_cmd(
            effective_config, url, title, output_path_override
        )
        for ext_name in allowed:
            cmd.insert(-1, "--ies")
            cmd.insert(-1, ext_name)

        log.step(f"Downloading {outtmpl} (system yt-dlp, native extractor)")
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            log.success(f"Completed: {outtmpl}")
            return True
        log.error(f"yt-dlp exited with code {result.returncode}")
        return False

    ydl_opts, outtmpl = build_ydl_opts(
        effective_config, title, output_path_override
    )
    if allowed:
        ydl_opts["allowed_extractors"] = allowed

    log.step(f"Downloading {outtmpl} (yt-dlp native extractor)")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        log.success(f"Completed: {outtmpl}")
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
        m3u8_urls, video_urls, page_title = extract_m3u8(driver, url)

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

        driver.quit()

        if m3u8_urls:
            selected = _select_m3u8_urls(m3u8_urls, effective_config, url)
            for m3u8_url in selected:
                log.info(f"Found m3u8: {m3u8_url}")
                _download_m3u8(m3u8_url, effective_config, page_title, output_path_override)
        else:
            log.info(f"Found {len(video_urls)} direct video URL(s)")
            selected = _select_video_urls(video_urls, effective_config, url)
            for vid_url in selected:
                log.info(f"Found video: {vid_url}")
                _download_m3u8(vid_url, effective_config, page_title, output_path_override)

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

    log.header("Summary")
    log.info(f"Total time: {_format_duration(elapsed)}")
    log.success(f"{len(succeeded)} succeeded")

    if failed:
        log.error(f"{len(failed)} failed")
        for url, _, error in failed:
            reason = error or "unknown error"
            log.detail(f"{url}")
            log.detail(f"  └ {reason}")
    else:
        log.success("No failures")


def download_from_file(file_path, config):
    """Read URLs from a file and download each one.

    Supports group directives to set options for blocks of URLs:
        --- --audio-only --quality best
        https://example.com/song1
        https://example.com/song2
        ---
        https://example.com/video   # group reset, back to global defaults
    """
    per_url_parser = _build_per_url_parser()
    try:
        with open(file_path, "r") as f:
            lines = f.readlines()

        entries = []
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
            log.warn("No URLs found in the file.")
            return

        workers = _resolve_worker_count(config.get("parallel"), len(entries))
        log.header(f"Downloading {len(entries)} URL{'s' if len(entries) != 1 else ''} "
                   f"with {workers} worker{'s' if workers != 1 else ''}")

        results = []
        start_time = time.time()

        if workers <= 1 or len(entries) == 1:
            for i, (url, overrides) in enumerate(entries, 1):
                log.step(f"[{i}/{len(entries)}] {url}")
                try:
                    ok, err = fetch_m3u8_and_download(url, config, per_url_overrides=overrides)
                    results.append((url, ok, err))
                except Exception as exc:
                    log.error(f"Failed: {url} — {exc}")
                    results.append((url, False, str(exc)))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        fetch_m3u8_and_download, url, config, None, overrides
                    ): url
                    for url, overrides in entries
                }
                for future in as_completed(futures):
                    src_url = futures[future]
                    try:
                        ok, err = future.result()
                        results.append((src_url, ok, err))
                    except Exception as exc:
                        log.error(f"Failed: {src_url} — {exc}")
                        results.append((src_url, False, str(exc)))

        elapsed = time.time() - start_time
        _print_summary(results, elapsed)

    except FileNotFoundError:
        log.error(f"File not found: '{file_path}'")
    except Exception as e:
        log.error(f"An error occurred while reading the file: {e}")


# ---------------------------------------------------------------------------
# Clipboard watch mode
# ---------------------------------------------------------------------------
def _read_clipboard():
    """Read the current clipboard text. Returns empty string on failure."""
    # Try platform-specific commands
    for cmd in ("xclip -selection clipboard -o",
                "xsel --clipboard --output",
                "pbpaste",
                "powershell.exe -command Get-Clipboard"):
        try:
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return ""


def _looks_like_url(text):
    """Quick check if text looks like a URL worth trying."""
    return bool(text) and re.match(r'https?://', text.strip().split('\n')[0])


def watch_clipboard(config, interval=1.0):
    """Poll the clipboard for new URLs and download them automatically."""
    log.header("Watching clipboard for URLs  (Ctrl+C to stop)")
    seen = set()
    last_text = ""

    # Prime with current clipboard so we don't immediately download
    # whatever is already there
    last_text = _read_clipboard()
    if _looks_like_url(last_text):
        seen.add(last_text.strip().split('\n')[0])

    try:
        while True:
            time.sleep(interval)
            text = _read_clipboard()
            if not text or text == last_text:
                continue
            last_text = text

            # Extract the first line as the URL
            url = text.strip().split('\n')[0].strip()
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

    # Resolve config file: explicit flag > CWD > user config dir
    toml_path = args.config or _resolve_default_file(DEFAULT_CONFIG_FILE)
    toml_cfg = load_toml_config(toml_path)
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
        # Batch download from file
        urls_file = _resolve_default_file(config["urls_file"])
        download_from_file(urls_file, config)


if __name__ == "__main__":
    main()
