# m3u8-extractor

Extract m3u8 stream URLs from web pages and download them
with [yt-dlp](https://github.com/yt-dlp/yt-dlp). Uses Selenium to
render JavaScript-heavy pages, then hands the discovered stream
URL to yt-dlp for reliable downloading.

## Features

- **Automatic m3u8 extraction** — loads pages with headless
  Chrome, finds m3u8 URLs in the rendered source
- **Smart extractor routing** — tries yt-dlp's native extractors
  first, falls back to Selenium m3u8 only when needed
- **Three config sources** — CLI flags, environment variables,
  and TOML config file (priority: CLI > env > TOML > defaults)
- **URL rules** — pattern-matched per-site config in TOML
  (e.g. always use audio-only for music sites)
- **Batch downloads** — read URLs from a file, with per-URL
  and group option overrides
- **Parallel downloads** — download all URLs simultaneously
  by default, or limit concurrency
- **Clipboard watch mode** — monitors clipboard for URLs and
  downloads automatically
- **Multiple m3u8 handling** — warns when multiple streams are
  found, with options to select or filter
- **Adblock** — optionally loads uBlock Origin Lite to bypass ad-heavy pages
- **Proxy support** — separate proxies for browser and downloader
- **System yt-dlp** — use the system binary or a custom yt-dlp
  path instead of the Python library
- **Pretty output** — colored, symbol-coded progress with
  yt-dlp's built-in progress bar

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

Make sure [ChromeDriver](https://chromedriver.chromium.org/) is in
your `PATH` and matches your Chrome version.

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

```text
m3u8-extractor [url] [options]
```

If no URL is given and `--watch` is not set, URLs are read from
a file (`urls.txt` by default).

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
| `--user-agent`               | Custom User-Agent for yt-dlp and browser requests      |
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
| `--overwrite`               | Overwrite existing files (default)        |
| `--no-overwrite`            | Skip download if output file exists       |

### yt-dlp binary

| Flag                 | Description                                                        |
| -------------------- | ------------------------------------------------------------------ |
| `--use-system-ytdlp` | Use the system `yt-dlp` binary instead of the Python library       |
| `--yt-dlp-path`      | Path to a specific yt-dlp binary                                   |
| `--ytdlp-args`       | Extra raw arguments forwarded to yt-dlp (e.g. `'--limit-rate 1M'`) |

### Parallelism

| Flag               | Description                                                                          |
| ------------------ | ------------------------------------------------------------------------------------ |
| `-p`, `--parallel` | Number of parallel downloads: a number, `all` (default), `cores`, or `logical_cores` |
| `--speed-unit`     | Speed display in progress bar: `bytes` (default, e.g. MB/s) or `bits` (e.g. Mbps)    |

### Stream selection

| Flag             | Description                                                                          |
| ---------------- | ------------------------------------------------------------------------------------ |
| `--stream-type`  | Which stream types to look for: `both` (default), `m3u8`, or `video`                 |
| `--m3u8-select`  | Which stream when multiple found: `first` (default), `last`, `all`, or `interactive` |
| `--m3u8-filter`  | Regular expression to filter m3u8 URLs before selection                              |
| `--video-filter` | Regular expression to filter direct video URLs (mp4, webm, etc.) before selection    |

### Adblock

| Flag                   | Description                                                      |
| ---------------------- | ---------------------------------------------------------------- |
| `--adblock`            | Load uBlock Origin Lite in Chrome (auto-downloaded on first use) |
| `--adblock-strictness` | Filtering level: `basic`, `optimal`, or `complete` (default)     |
| `--adblock-extension`  | Path to a custom `.crx` adblocker extension                      |

### Extractor selection

| Flag           | Description                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------------ |
| `--extractor`  | Strategy: `auto` (default, try yt-dlp native then m3u8), `ytdlp` (native only), `m3u8` (Selenium only) |
| `--extractors` | Comma-separated allowlist of yt-dlp extractor names (e.g. `youtube,vimeo`)                             |

### Proxy

| Flag              | Description                                                 |
| ----------------- | ----------------------------------------------------------- |
| `--proxy`         | Proxy for yt-dlp downloads (e.g. `socks5://127.0.0.1:1080`) |
| `--browser-proxy` | Proxy for Chrome (defaults to `--proxy` if not set)         |

### SSL

| Flag                  | Description                                         |
| --------------------- | --------------------------------------------------- |
| `--ignore-ssl-errors` | Ignore SSL certificate errors in browser and yt-dlp |

### localStorage

| Flag                       | Description                                            |
| -------------------------- | ------------------------------------------------------ |
| `--localstorage KEY=VALUE` | Set a localStorage entry before page load (repeatable) |

Example: `--localstorage "jwplayer.qualityLabel=HQ"` to force
HQ quality on JWPlayer sites.

### Headers & authentication

| Flag                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `--header NAME=VALUE` | Custom HTTP header for browser & yt-dlp (repeatable) |
| `--auth USER:PASS`    | HTTP basic auth credentials                          |

The `--cookies` file is loaded into both the Selenium browser
and yt-dlp, so auth-gated pages work during extraction too.

### Watch mode

| Flag               | Description                                         |
| ------------------ | --------------------------------------------------- |
| `-w`, `--watch`    | Watch clipboard for URLs and download automatically |
| `--watch-interval` | Polling interval in seconds (default: `1.0`)        |

## Configuration

Settings are resolved with this priority:

1. **CLI arguments** (highest)
2. **Per-URL flags** (in URL list file)
3. **Group directives** (in URL list file)
4. **URL rules** (pattern-matched from TOML config)
5. **Environment variables**
6. **TOML config file**
7. **Built-in defaults** (lowest)

### Config file

Place a `config.toml` in the current directory or
`~/.config/m3u8-extractor/config.toml`:

```toml
# config.toml

urls_file = "urls.txt"
output_path = "downloads/"
title_prefix = ""
title_postfix = ""
quality = "bestvideo+bestaudio"
transcode = "mp4"
parallel = "all"
speed_unit = "bytes"   # "bytes" (KB/s, MB/s) or "bits" (Kbps, Mbps)

referrer = ""
use_base_url_as_referrer = false
cookies = ""

use_system_ytdlp = false
# yt_dlp_path = "/usr/local/bin/yt-dlp"

extractor = "auto"    # "auto", "ytdlp", or "m3u8"
# extractors = "youtube,vimeo"  # restrict yt-dlp to these extractors

m3u8_select = "first"    # "first", "last", "all", or "interactive"
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

### URL rules

Define per-site config using regular expression patterns in
`[[url_rules]]` sections:

```toml
# Use yt-dlp native extractor for YouTube
[[url_rules]]
pattern = "youtube\\.com|youtu\\.be"
extractor = "ytdlp"

# Audio only for a music site
[[url_rules]]
pattern = "example\\.com/music"
audio_only = true
quality = "bestaudio"
output_path = "music/"

# Extra options for a sketchy site
[[url_rules]]
pattern = "sketchy-site\\.com"
adblock = true
ignore_ssl_errors = true
proxy = "socks5://127.0.0.1:1080"
```

Rules are checked in order — all matching rules are merged,
with later rules overriding earlier ones. Any config option can
be used in a rule.

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
M3U8_EXTRACTOR=auto
M3U8_EXTRACTORS=""
M3U8_PARALLEL=all
M3U8_SPEED_UNIT=bytes
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

The URL list file supports three formats per line, plus group directives:

```text
# Comments start with #

# 1. Just a URL (uses page title as filename)
https://example.com/video1

# 2. URL followed by a title/output path (space-separated)
https://example.com/video2 My Custom Title

# 3. URL with per-URL option flags
https://example.com/video3 --audio-only -o "music/song"
https://example.com/video4 --captions -q "bestvideo+bestaudio"
https://example.com/video5 -o "downloads/" --thumbnail --transcode mkv
```

Per-URL flags override the global config for that specific
download. All CLI flags are supported.

### Group directives

Use `---` to set options for a group of URLs:

```text
# Start an audio-only group
--- --audio-only -q "bestaudio"
https://example.com/song1
https://example.com/song2
https://example.com/song3

# Switch to a different group with captions
--- --captions --transcode mkv
https://example.com/lecture1
https://example.com/lecture2

# Reset to global defaults
---
https://example.com/normal-video

# Per-URL options still override the group
--- --audio-only
https://example.com/song4
https://example.com/video5 --video-only   # overrides audio-only for this URL
```

Group options apply to all URLs that follow, until the next
`---` directive. Use `---` alone to reset back to global defaults.

## Examples

```bash
# Basic one-off download
m3u8-extractor "https://example.com/video"

# Download with custom output and quality
m3u8-extractor -o my-video -q "bestvideo+bestaudio" \
  "https://example.com/video"

# Batch download with 4 parallel workers, using adblock
m3u8-extractor -p 4 --adblock

# Audio only, through a proxy
m3u8-extractor --audio-only \
  --proxy socks5://127.0.0.1:1080 \
  "https://example.com/video"

# Watch clipboard, download captions too
m3u8-extractor --watch --captions

# Use system yt-dlp at a custom path
m3u8-extractor --yt-dlp-path /opt/bin/yt-dlp "https://example.com/video"

# Download all m3u8 streams found on a page
m3u8-extractor --m3u8-select all "https://example.com/multi-stream"

# Filter m3u8 URLs by pattern
m3u8-extractor --m3u8-filter "1080p" "https://example.com/video"

# Use yt-dlp native extractor only (skip Selenium)
m3u8-extractor --extractor ytdlp "https://youtube.com/watch?v=abc123"

# Restrict to specific extractors
m3u8-extractor --extractors "youtube,vimeo" "https://youtube.com/watch?v=abc123"
```

## License

This project is licensed under the
[GNU Affero General Public License v3.0][agpl]
only (AGPL-3.0-only).

See [LICENSE](LICENSE) for the full text.

[agpl]: https://www.gnu.org/licenses/agpl-3.0.html
