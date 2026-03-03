import os
import sys
import time
import re
import shlex
import argparse
import subprocess
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
    "quality": None,
    "transcode": None,
    "yt_dlp_path": None,         # custom path to yt-dlp binary
    "use_system_ytdlp": False,   # use system yt-dlp instead of Python library
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
    "quality":                  "M3U8_QUALITY",
    "transcode":                "M3U8_TRANSCODE",
    "yt_dlp_path":              "M3U8_YT_DLP_PATH",
    "use_system_ytdlp":         "M3U8_USE_SYSTEM_YTDLP",
    "thumbnail":                "M3U8_THUMBNAIL",
    "thumbnail_only":           "M3U8_THUMBNAIL_ONLY",
    "captions":                 "M3U8_CAPTIONS",
    "captions_only":            "M3U8_CAPTIONS_ONLY",
    "audio_only":               "M3U8_AUDIO_ONLY",
    "video_only":               "M3U8_VIDEO_ONLY",
    "video_and_captions_only":  "M3U8_VIDEO_AND_CAPTIONS_ONLY",
}

BOOL_KEYS = {
    "use_base_url_as_referrer", "use_system_ytdlp",
    "thumbnail", "thumbnail_only",
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
    # Normalise bools
    for key in BOOL_KEYS:
        if key in data:
            data[key] = _parse_bool(data[key])
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
        "quality": args_ns.quality,
        "transcode": args_ns.transcode,
        "yt_dlp_path": args_ns.yt_dlp_path,
        "use_system_ytdlp": args_ns.use_system_ytdlp,
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
    """Build a parser for per-URL inline options in the URLs file."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("url")
    p.add_argument("-o", "--output", dest="output_path")
    p.add_argument("--title-prefix")
    p.add_argument("--title-postfix")
    p.add_argument("--referrer")
    p.add_argument("--use-base-url-as-referrer", action="store_true", default=None)
    p.add_argument("--cookies")
    p.add_argument("-q", "--quality")
    p.add_argument("--transcode")
    p.add_argument("--use-system-ytdlp", action="store_true", default=None)
    p.add_argument("--yt-dlp-path")
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

    cmd.append(m3u8_url)
    return cmd, outtmpl


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def extract_m3u8(driver, url):
    """Use Selenium to load a page and extract the m3u8 URL."""
    driver.get(url)
    time.sleep(5)

    page_title = driver.title.strip()
    page_source = driver.page_source

    m3u8_url = None
    m3u8_pattern = r'(https?://[^\s"]+\.m3u8)'
    match = re.search(m3u8_pattern, page_source)
    if match:
        m3u8_url = match.group(0)

    # Fix incomplete URLs
    if not m3u8_url or m3u8_url.startswith("t:"):
        print("Fixing the m3u8 URL...")
        m3u8_url = urljoin(url, m3u8_url)
        if m3u8_url.startswith("t:"):
            m3u8_url = url + m3u8_url[2:]

    return m3u8_url, page_title


def fetch_m3u8_and_download(url, config, output_path_override=None, per_url_overrides=None):
    """Extract the m3u8 URL from a page and download with yt-dlp."""
    # Merge per-URL overrides on top of the global config
    effective_config = dict(config)
    if per_url_overrides:
        effective_config.update(per_url_overrides)

    # output_path from per-URL overrides takes precedence
    if output_path_override is None:
        output_path_override = effective_config.pop("output_path", None)

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    driver = webdriver.Chrome(options=chrome_options)

    try:
        m3u8_url, page_title = extract_m3u8(driver, url)

        if not m3u8_url:
            print("m3u8 URL not found in the page source.")
            return

        driver.quit()

        # If use_base_url_as_referrer, set referrer from the page URL
        if effective_config.get("use_base_url_as_referrer") and not effective_config.get("referrer"):
            parsed = urlparse(url)
            effective_config["referrer"] = f"{parsed.scheme}://{parsed.netloc}/"

        print(f"Found m3u8 URL: {m3u8_url}")

        use_system = effective_config.get("use_system_ytdlp") or effective_config.get("yt_dlp_path")

        if use_system:
            cmd, outtmpl = _build_system_ytdlp_cmd(
                effective_config, m3u8_url, page_title, output_path_override
            )
            print(f"Downloading to {outtmpl} (system yt-dlp)...")
            result = subprocess.run(cmd, check=False)
            if result.returncode == 0:
                print(f"Download completed: {outtmpl}")
            else:
                print(f"yt-dlp exited with code {result.returncode}")
        else:
            ydl_opts, outtmpl = build_ydl_opts(
                effective_config, page_title, output_path_override
            )
            print(f"Downloading to {outtmpl}...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([m3u8_url])
            print(f"Download completed: {outtmpl}")

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def download_from_file(file_path, config):
    """Read URLs from a file and download each one."""
    per_url_parser = _build_per_url_parser()
    try:
        with open(file_path, "r") as f:
            lines = f.readlines()

        entries = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                url, overrides = _parse_url_line(line, per_url_parser)
                entries.append((url, overrides))
            except SystemExit:
                print(f"Warning: could not parse line: {line}")
                continue

        if not entries:
            print("No URLs found in the file.")
            return

        for url, overrides in entries:
            fetch_m3u8_and_download(url, config, per_url_overrides=overrides)

    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
    except Exception as e:
        print(f"An error occurred while reading the file: {e}")


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

    if args.url:
        # One-off download: use the URL directly
        fetch_m3u8_and_download(args.url, config)
    else:
        # Batch download from file
        urls_file = _resolve_default_file(config["urls_file"])
        download_from_file(urls_file, config)


if __name__ == "__main__":
    main()
