# m3u8-extractor

Extract m3u8 stream URLs from web pages and download them with [yt-dlp](https://github.com/yt-dlp/yt-dlp). Uses Selenium to render JavaScript-heavy pages, then hands the discovered stream URL to yt-dlp for reliable downloading.

## Features

- **Automatic m3u8 extraction** — loads pages with headless Chrome, finds m3u8 URLs in the rendered source
- **Three config sources** — CLI flags, environment variables, and TOML config file (priority: CLI > env > TOML > defaults)
- **Batch downloads** — read URLs from a file, with per-URL option overrides
- **Parallel downloads** — download all URLs simultaneously by default, or limit concurrency
- **Clipboard watch mode** — monitors clipboard for URLs and downloads automatically
- **Multiple m3u8 handling** — warns when multiple streams are found, with options to select or filter
- **Adblock** — optionally loads uBlock Origin Lite to bypass ad-heavy pages
- **Proxy support** — separate proxies for browser and downloader
- **System yt-dlp** — use the system binary or a custom yt-dlp path instead of the Python library
- **Pretty output** — colored, symbol-coded progress with yt-dlp's built-in progress bar

## Installation

**Requirements:** Python 3.9+, Chrome/Chromium, ChromeDriver

```bash
pip install -e .
```

Or install dependencies directly:

```bash
pip install selenium yt-dlp
# On Python < 3.11, also:
pip install tomli
```

Make sure [ChromeDriver](https://chromedriver.chromium.org/) is in your `PATH` and matches your Chrome version.

## Quick start

```bash
# Download a single URL
m3u8-extractor "https://example.com/video-page"

# Download from a URL list
m3u8-extractor

# Watch clipboard and auto-download
m3u8-extractor --watch

# Audio only, with adblock
m3u8-extractor --audio-only --adblock "https://example.com/video-page"
```

## Usage

```
m3u8-extractor [url] [options]
```

If no URL is given and `--watch` is not set, URLs are read from a file (`urls.txt` by default).

### General options

| Flag                         | Description                                            |
| ---------------------------- | ------------------------------------------------------ |
| `url`                        | URL to download directly (positional, optional)        |
| `-f`, `--urls-file`          | Path to URL list file                                  |
| `-o`, `--output-path`        | Output directory or filename template                  |
| `--title-prefix`             | String to prepend to every filename                    |
| `--title-postfix`            | String to append to every filename (before extension)  |
| `--referrer`                 | Referer header for requests                            |
| `--use-base-url-as-referrer` | Auto-set referer from each page's base URL             |
| `--cookies`                  | Path to Netscape-format cookies file                   |
| `-q`, `--quality`            | yt-dlp format selector (e.g. `bestvideo+bestaudio`)    |
| `--transcode`                | Transcode to format after download (e.g. `mp4`, `mkv`) |
| `-c`, `--config`             | Path to TOML config file                               |

### Download modes

| Flag                        | Description                               |
| --------------------------- | ----------------------------------------- |
| `--thumbnail`               | Download thumbnail alongside video        |
| `--thumbnail-only`          | Download only the thumbnail               |
| `--captions`                | Download captions alongside video         |
| `--captions-only`           | Download only captions                    |
| `--audio-only`              | Download only the audio stream            |
| `--video-only`              | Download only the video stream (no audio) |
| `--video-and-captions-only` | Download video and captions (no audio)    |

### yt-dlp binary

| Flag                 | Description                                                  |
| -------------------- | ------------------------------------------------------------ |
| `--use-system-ytdlp` | Use the system `yt-dlp` binary instead of the Python library |
| `--yt-dlp-path`      | Path to a specific yt-dlp binary                             |

### Parallelism

| Flag               | Description                                                                          |
| ------------------ | ------------------------------------------------------------------------------------ |
| `-p`, `--parallel` | Number of parallel downloads: a number, `all` (default), `cores`, or `logical_cores` |

### m3u8 selection

| Flag            | Description                                                         |
| --------------- | ------------------------------------------------------------------- |
| `--m3u8-select` | Which m3u8 when multiple found: `first` (default), `last`, or `all` |
| `--m3u8-filter` | Regex pattern to filter m3u8 URLs before selection                  |

### Adblock

| Flag                  | Description                                                      |
| --------------------- | ---------------------------------------------------------------- |
| `--adblock`           | Load uBlock Origin Lite in Chrome (auto-downloaded on first use) |
| `--adblock-extension` | Path to a custom `.crx` adblocker extension                      |

### Proxy

| Flag              | Description                                                 |
| ----------------- | ----------------------------------------------------------- |
| `--proxy`         | Proxy for yt-dlp downloads (e.g. `socks5://127.0.0.1:1080`) |
| `--browser-proxy` | Proxy for Chrome (defaults to `--proxy` if not set)         |

### SSL

| Flag                  | Description                                         |
| --------------------- | --------------------------------------------------- |
| `--ignore-ssl-errors` | Ignore SSL certificate errors in browser and yt-dlp |

### Watch mode

| Flag               | Description                                         |
| ------------------ | --------------------------------------------------- |
| `-w`, `--watch`    | Watch clipboard for URLs and download automatically |
| `--watch-interval` | Polling interval in seconds (default: `1.0`)        |

## Configuration

Settings are resolved with this priority:

1. **CLI arguments** (highest)
2. **Environment variables**
3. **TOML config file**
4. **Built-in defaults** (lowest)

### Config file

Place a `config.toml` in the current directory or `~/.config/m3u8-extractor/config.toml`:

```toml
# config.toml

urls_file = "urls.txt"
output_path = "downloads/"
title_prefix = ""
title_postfix = ""
quality = "bestvideo+bestaudio"
transcode = "mp4"
parallel = "all"

referrer = ""
use_base_url_as_referrer = false
cookies = ""

use_system_ytdlp = false
# yt_dlp_path = "/usr/local/bin/yt-dlp"

m3u8_select = "first"    # "first", "last", or "all"
# m3u8_filter = "pattern"

adblock = false
# adblock_extension = "/path/to/extension.crx"

# proxy = "socks5://127.0.0.1:1080"
# browser_proxy = "http://127.0.0.1:8080"

ignore_ssl_errors = false

thumbnail = false
thumbnail_only = false
captions = false
captions_only = false
audio_only = false
video_only = false
video_and_captions_only = false
```

### Environment variables

Every option has a corresponding environment variable prefixed with `M3U8_`:

```bash
M3U8_URLS_FILE=urls.txt
M3U8_OUTPUT_PATH=downloads/
M3U8_TITLE_PREFIX=""
M3U8_TITLE_POSTFIX=""
M3U8_REFERRER=""
M3U8_USE_BASE_URL_AS_REFERRER=false
M3U8_COOKIES=""
M3U8_QUALITY="bestvideo+bestaudio"
M3U8_TRANSCODE=mp4
M3U8_YT_DLP_PATH=""
M3U8_USE_SYSTEM_YTDLP=false
M3U8_PARALLEL=all
M3U8_SELECT=first
M3U8_FILTER=""
M3U8_ADBLOCK=false
M3U8_ADBLOCK_EXTENSION=""
M3U8_PROXY=""
M3U8_BROWSER_PROXY=""
M3U8_IGNORE_SSL_ERRORS=false
M3U8_THUMBNAIL=false
M3U8_THUMBNAIL_ONLY=false
M3U8_CAPTIONS=false
M3U8_CAPTIONS_ONLY=false
M3U8_AUDIO_ONLY=false
M3U8_VIDEO_ONLY=false
M3U8_VIDEO_AND_CAPTIONS_ONLY=false
```

Boolean values accept `1`, `true`, `yes`, `on` (case-insensitive).

### Config file resolution

Both `config.toml` and `urls.txt` are searched in order:

1. Current working directory
2. `~/.config/m3u8-extractor/` (respects `$XDG_CONFIG_HOME`)

Use `-c` or `-f` to specify an explicit path.

## URL list format

The URL list file supports three formats per line:

```
# Comments start with #

# 1. Just a URL (uses page title as filename)
https://example.com/video1

# 2. URL followed by a title/output path (space-separated)
https://example.com/video2 My Custom Title

# 3. URL with per-URL option flags
https://example.com/video3 --audio-only -o "music/song"
https://example.com/video4 --captions -q "bestvideo+bestaudio" --referrer "https://example.com"
https://example.com/video5 -o "downloads/" --thumbnail --transcode mkv
```

Per-URL flags override the global config for that specific download. All CLI flags are supported.

## Examples

```bash
# Basic one-off download
m3u8-extractor "https://example.com/video"

# Download with custom output and quality
m3u8-extractor -o "my-video" -q "bestvideo+bestaudio" "https://example.com/video"

# Batch download with 4 parallel workers, using adblock
m3u8-extractor -p 4 --adblock

# Audio only, through a proxy
m3u8-extractor --audio-only --proxy "socks5://127.0.0.1:1080" "https://example.com/video"

# Watch clipboard, download captions too
m3u8-extractor --watch --captions

# Use system yt-dlp at a custom path
m3u8-extractor --yt-dlp-path /opt/bin/yt-dlp "https://example.com/video"

# Download all m3u8 streams found on a page
m3u8-extractor --m3u8-select all "https://example.com/multi-stream"

# Filter m3u8 URLs by pattern
m3u8-extractor --m3u8-filter "1080p" "https://example.com/video"
```

## License

This project is licensed under the [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html) only (AGPL-3.0-only).

See [LICENSE](LICENSE) for the full text.
