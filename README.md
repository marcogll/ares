<p align="center">
  <img src="https://raw.githubusercontent.com/marcogll/mg_data_storage/refs/heads/main/soul23/logo/soul23_logo.svg" width="110" alt="ares">
</p>

<h1 align="center">ares</h1>

<p align="center">
  Music downloader and media library manager with concurrent queue processing and automatic metadata tagging
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3a3a3a?style=flat-square&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/Flask-3a3a3a?style=flat-square&logo=flask&logoColor=white">
  <img src="https://img.shields.io/badge/yt--dlp-3a3a3a?style=flat-square&logo=youtube&logoColor=white">
  <img src="https://img.shields.io/badge/aria2-3a3a3a?style=flat-square&logo=aria&logoColor=white">
  <img src="https://img.shields.io/badge/Tailwind-3a3a3a?style=flat-square&logo=tailwindcss&logoColor=white">
  <img src="https://img.shields.io/badge/Mutagen-3a3a3a?style=flat-square&logo=musicbrainz&logoColor=white">
</p>

---

<p align="center">
  <b>ares</b> downloads audio from YouTube and YouTube Music URLs, applies ID3 metadata via YouTube title parsing or iTunes/Deezer fallback, and organizes files into an <code>Artist — Album</code> directory structure. A web UI provides real-time progress, concurrent download control, and a media library browser.
</p>

## Features

- **Queue-based downloading** — submit URLs via web UI, processed by background daemon
- **Concurrent workers** — configurable parallel downloads (default 2)
- **Metadata extraction** — parses artist and track from YouTube video titles; falls back to iTunes and Deezer APIs for album and artwork
- **ID3 tagging** — writes artist, title, album, track number, and cover art to MP3 files
- **Album organization** — files are moved into `Artist — Album` directories with normalized naming
- **Duplicate detection** — prevents re-downloading by YouTube video ID and URL
- **Web UI** — tabbed interface with downloader panel and media library browser

## Requirements

- Python 3.9+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [aria2](https://aria2.github.io/)
- [ffmpeg](https://ffmpeg.org/) (required by yt-dlp for audio extraction)

## Installation

```bash
git clone https://github.com/marcogll/ares.git
cd ares

pip install -r requirements.txt
```

## Usage

```bash
python3 webui.py
```

The web interface starts at `http://127.0.0.1:5800`. The download daemon starts automatically.

### Web UI

| Tab | Description |
| --- | --- |
| **Downloader** | Paste URLs, monitor progress, view queued and downloaded files, adjust concurrent download limit |
| **Media Library** | Browse organized albums grouped by artist, view track lists and cover art |

### Configuration

Settings are stored in `config.json`:

| Key | Default | Description |
| --- | --- | --- |
| `MAX_CONCURRENT` | `2` | Maximum simultaneous downloads |
| `AUDIO_FORMAT` | `mp3` | Output audio format |
| `AUDIO_QUALITY` | `0` | Audio quality (0 = best) |

The concurrent download limit can also be adjusted from the web UI.

## Project Structure

```
ares/
├── webui.py              # Flask web server and API
├── music_daemon.py       # Background download worker pool
├── metadata_scraper.py   # Metadata parsing, API lookups, ID3 tagging
├── tui.py                # Terminal UI (legacy)
└── templates/
    └── index.html        # Web UI template
```
