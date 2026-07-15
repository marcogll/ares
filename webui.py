import os
import re
import sys
import json
import uuid
import signal
import subprocess
import atexit
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file
from metadata_scraper import read_metadata_log
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC

BASE = Path.home() / "Music" / "ares"
CONFIG_PATH = BASE / "config.json"
QUEUE_DIR = str(BASE / ".queue")
DOWNLOAD_DIR = str(BASE / "downloaded")
PROCESSED_DIR = str(BASE / "processed")
DAEMON_SCRIPT = str(Path(__file__).parent / "music_daemon.py")
LOG_FILE = str(Path.home() / "Library" / "Logs" / "music-daemon" / "daemon.log")

QUEUE_EXTS = (".url", ".processing", ".failed")

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"MAX_CONCURRENT": 2}

def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

daemon_proc = None
app = Flask(__name__)


# -- helpers --

def ensure_dirs():
    for d in [QUEUE_DIR, DOWNLOAD_DIR, PROCESSED_DIR]:
        os.makedirs(d, exist_ok=True)


def daemon_alive():
    return daemon_proc is not None and daemon_proc.poll() is None


def count_by_ext(ext):
    return sum(1 for f in os.listdir(QUEUE_DIR) if f.endswith(ext))


def collect_status():
    pending = count_by_ext(".url")
    processing_count = count_by_ext(".processing")
    failed = count_by_ext(".failed")

    queued_urls = []
    processing_jobs = []
    for fname in sorted(os.listdir(QUEUE_DIR)):
        fpath = os.path.join(QUEUE_DIR, fname)
        if fname.endswith(".url"):
            try:
                with open(fpath) as f:
                    queued_urls.append(f.read().strip()[:80])
            except OSError:
                queued_urls.append("?")
        elif fname.endswith(".processing"):
            try:
                with open(fpath) as f:
                    url = f.read().strip()[:80]
            except OSError:
                url = "?"
            prog = {}
            progress_path = fpath + ".progress"
            if os.path.exists(progress_path):
                try:
                    with open(progress_path) as f:
                        prog = json.load(f)
                except (OSError, json.JSONDecodeError):
                    pass
            processing_jobs.append({"file": fname, "url": url, "progress": prog})

    dl_files = sorted(f for f in os.listdir(DOWNLOAD_DIR) if not f.startswith("."))
    dl_sizes = []
    for f in dl_files:
        fpath = os.path.join(DOWNLOAD_DIR, f)
        try:
            sz = os.path.getsize(fpath)
        except OSError:
            sz = 0
        dl_sizes.append({"name": f, "size": sz})

    albums = []
    processed_total = 0
    try:
        for entry in sorted(os.listdir(PROCESSED_DIR)):
            album_path = os.path.join(PROCESSED_DIR, entry)
            if os.path.isdir(album_path):
                tracks = sorted(f for f in os.listdir(album_path) if f.endswith(".mp3"))
                if tracks:
                    albums.append({
                        "name": entry,
                        "tracks": len(tracks),
                        "songs": tracks,
                        "cover": f"/api/cover?album={entry}",
                    })
                    processed_total += len(tracks)
    except OSError:
        pass

    logs = []
    try:
        with open(LOG_FILE) as f:
            logs = [l.strip() for l in f.readlines()[-12:] if l.strip()]
    except OSError:
        pass

    cfg = load_config()
    return {
        "daemon": {"running": daemon_alive(), "pid": daemon_proc.pid if daemon_proc else None},
        "queue": {"pending": pending, "processing": processing_count, "failed": failed,
                  "items": queued_urls},
        "processing_jobs": processing_jobs,
        "downloaded": {"count": len(dl_sizes), "files": dl_sizes},
        "processed": {"count": processed_total, "albums": albums},
        "config": {"max_concurrent": cfg.get("MAX_CONCURRENT", 2)},
        "logs": logs,
    }


RE_YT_VIDEO_ID = re.compile(
    r"(?:youtube\.com/watch\?.*v=|youtu\.be/|music\.youtube\.com/watch\?.*v=)([a-zA-Z0-9_-]{11})"
)


def is_url_queued(url):
    url = url.strip()
    m = RE_YT_VIDEO_ID.search(url)
    vid = m.group(1) if m else None
    for fname in os.listdir(QUEUE_DIR):
        if not fname.endswith(QUEUE_EXTS):
            continue
        try:
            with open(os.path.join(QUEUE_DIR, fname)) as f:
                existing = f.read().strip()
            if existing == url:
                return True
            if vid:
                m2 = RE_YT_VIDEO_ID.search(existing)
                if m2 and m2.group(1) == vid:
                    return True
        except OSError:
            continue
    return False


# -- daemon lifecycle --

def start_daemon():
    global daemon_proc
    if daemon_alive():
        return True
    ensure_dirs()
    try:
        daemon_proc = subprocess.Popen(
            [sys.executable, DAEMON_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def stop_daemon():
    global daemon_proc
    if daemon_proc and daemon_proc.poll() is None:
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
    daemon_proc = None


# -- routes --

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(collect_status())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    if is_url_queued(url):
        return jsonify({"ok": False, "duplicate": True, "error": "URL already queued"}), 409

    job_id = str(uuid.uuid4())[:8]
    try:
        with open(os.path.join(QUEUE_DIR, f"{job_id}.url"), "w") as f:
            f.write(url + "\n")
        return jsonify({"ok": True, "job_id": job_id})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/retry", methods=["POST"])
def api_retry():
    count = 0
    for fname in os.listdir(QUEUE_DIR):
        if fname.endswith(".failed"):
            src = os.path.join(QUEUE_DIR, fname)
            try:
                with open(src) as f:
                    url = f.read().strip()
                if not url:
                    os.remove(src)
                    continue
                job_id = str(uuid.uuid4())[:8]
                with open(os.path.join(QUEUE_DIR, f"{job_id}.url"), "w") as f:
                    f.write(url + "\n")
                os.remove(src)
                count += 1
            except OSError:
                continue
    return jsonify({"ok": True, "count": count})


@app.route("/api/cover")
def api_cover():
    album_name = request.args.get("album", "")
    if not album_name:
        return "", 404
    album_path = os.path.join(PROCESSED_DIR, album_name)
    if not os.path.isdir(album_path):
        return "", 404
    for f in sorted(os.listdir(album_path)):
        if f.endswith(".mp3"):
            try:
                audio = MP3(os.path.join(album_path, f), ID3=ID3)
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        return send_file(Path(tag.data), mimetype=tag.mime)
            except Exception:
                continue
    return "", 404


@app.route("/api/metadata")
def api_metadata():
    return jsonify({"entries": read_metadata_log(50)})


@app.route("/api/clean/cache", methods=["POST"])
def api_clean_cache():
    count = 0
    for fname in os.listdir(DOWNLOAD_DIR):
        fpath = os.path.join(DOWNLOAD_DIR, fname)
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
                count += 1
        except OSError:
            continue
    return jsonify({"ok": True, "count": count})


@app.route("/api/clean/failed", methods=["POST"])
def api_clean_failed():
    count = 0
    for fname in os.listdir(QUEUE_DIR):
        if fname.endswith(".failed"):
            try:
                os.remove(os.path.join(QUEUE_DIR, fname))
                count += 1
            except OSError:
                continue
    return jsonify({"ok": True, "count": count})


@app.route("/api/daemon/start", methods=["POST"])
def api_daemon_start():
    if daemon_alive():
        return jsonify({"ok": True, "message": "Already running"})
    ok = start_daemon()
    return jsonify({"ok": ok, "pid": daemon_proc.pid if daemon_proc else None})


@app.route("/api/daemon/stop", methods=["POST"])
def api_daemon_stop():
    stop_daemon()
    return jsonify({"ok": True})


@app.route("/api/config")
def api_config():
    cfg = load_config()
    return jsonify({"max_concurrent": cfg.get("MAX_CONCURRENT", 2)})


@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.get_json(silent=True) or {}
    max_conc = data.get("max_concurrent")
    if not isinstance(max_conc, int) or max_conc < 1 or max_conc > 20:
        return jsonify({"ok": False, "error": "max_concurrent must be 1–20"}), 400
    cfg = load_config()
    cfg["MAX_CONCURRENT"] = max_conc
    save_config(cfg)
    return jsonify({"ok": True, "max_concurrent": max_conc})


# -- main --

atexit.register(stop_daemon)

if __name__ == "__main__":
    ensure_dirs()
    start_daemon()
    print(f"Web UI: http://127.0.0.1:5800")
    print(f"Queue:  {QUEUE_DIR}")
    app.run(host="127.0.0.1", port=5800, debug=False)
