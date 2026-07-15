import os
import re
import json
import time
import uuid
import threading
import logging
import signal
import subprocess
from pathlib import Path
from logging.handlers import RotatingFileHandler

from metadata_scraper import tag_and_organize

BASE = Path.home() / "Music" / "ares"
CONFIG_PATH = BASE / "config.json"

DEFAULT_CONFIG = {
    "QUEUE_DIR": str(BASE / ".queue"),
    "DOWNLOAD_DIR": str(BASE / "downloaded"),
    "PROCESSED_DIR": str(BASE / "processed"),
    "LOG_DIR": str(Path.home() / "Library" / "Logs" / "music-daemon"),
    "POLL_INTERVAL": 1.0,
    "ARIA2_ARGS": "-x 16 -s 16 -k 1M",
    "AUDIO_FORMAT": "mp3",
    "AUDIO_QUALITY": "0",
    "MAX_CONCURRENT": 2,
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            for k in DEFAULT_CONFIG:
                if k not in cfg:
                    cfg[k] = DEFAULT_CONFIG[k]
            return cfg
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

CONFIG = load_config()

LOG_FILE = os.path.join(CONFIG["LOG_DIR"], "daemon.log")
ERROR_LOG = os.path.join(CONFIG["LOG_DIR"], "error.log")

QUEUE_EXT = ".url"
PROCESSING_EXT = ".processing"
FAILED_EXT = ".failed"
PROGRESS_EXT = ".progress"

RE_PCT = re.compile(r"(\d+\.?\d*)%\s*of")
RE_ITEM = re.compile(r"Downloading (video|item) (\d+) of (\d+)")
RE_SPEED = re.compile(r"at\s+([\d.]+[KM]?i?B/s)")
RE_ETA = re.compile(r"ETA\s+([\d:]+)")
RE_YT_VIDEO_ID = re.compile(r"(?:youtube\.com/watch\?.*v=|youtu\.be/|music\.youtube\.com/watch\?.*v=)([a-zA-Z0-9_-]{11})")

running = True
_queue_lock = threading.Lock()


def setup_logging():
    os.makedirs(CONFIG["LOG_DIR"], exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger("music_daemon")
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    err_handler = RotatingFileHandler(ERROR_LOG, maxBytes=2 * 1024 * 1024, backupCount=2)
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(formatter)
    logger.addHandler(err_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    return logger


def ensure_dirs(logger):
    for d in [CONFIG["QUEUE_DIR"], CONFIG["DOWNLOAD_DIR"], CONFIG["PROCESSED_DIR"], CONFIG["LOG_DIR"]]:
        os.makedirs(d, exist_ok=True)
        logger.info(f"Directory ready: {d}")


def recover_orphaned_processing(logger):
    recovered = 0
    for fname in os.listdir(CONFIG["QUEUE_DIR"]):
        if fname.endswith(PROCESSING_EXT):
            src = os.path.join(CONFIG["QUEUE_DIR"], fname)
            try:
                with open(src) as f:
                    url = f.read().strip()
            except OSError:
                logger.error(f"Cannot read orphaned {fname}, skipping")
                continue
            clean_path = os.path.join(CONFIG["QUEUE_DIR"], f"{str(uuid.uuid4())[:8]}{QUEUE_EXT}")
            try:
                with open(clean_path, "w") as f:
                    f.write(url + "\n")
                os.remove(src)
                logger.warning(f"Recovered orphaned job: {fname} -> {os.path.basename(clean_path)}")
                recovered += 1
            except OSError as e:
                logger.error(f"Failed to recover {fname}: {e}")
    if recovered:
        logger.info(f"Recovered {recovered} orphaned job(s)")


def validate_url(url):
    url = url.strip()
    if not url:
        return False, "Empty URL"
    return True, url


def find_duplicate_url(url, skip_path=None):
    url = url.strip()
    skip = os.path.abspath(skip_path) if skip_path else None
    m = RE_YT_VIDEO_ID.search(url)
    vid = m.group(1) if m else None
    for fname in os.listdir(CONFIG["QUEUE_DIR"]):
        fpath = os.path.join(CONFIG["QUEUE_DIR"], fname)
        if os.path.abspath(fpath) == skip:
            continue
        if not fname.endswith((QUEUE_EXT, PROCESSING_EXT)):
            continue
        try:
            with open(fpath) as f:
                existing = f.read().strip()
            if existing == url:
                return fname
            if vid:
                m2 = RE_YT_VIDEO_ID.search(existing)
                if m2 and m2.group(1) == vid:
                    return fname
        except OSError:
            continue
    return None


def _track_progress(proc, progress_path, logger):
    progress = {"percent": 0, "speed": "", "eta": "", "item": 0, "total": 0, "status": "starting"}
    out_lines = []

    def _write():
        try:
            with open(progress_path, "w") as f:
                json.dump(progress, f)
        except OSError:
            pass

    def _reader():
        nonlocal progress
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                m = RE_ITEM.search(line)
                if m:
                    progress["item"] = int(m.group(2))
                    progress["total"] = int(m.group(3))

                m = RE_PCT.search(line)
                if m:
                    progress["percent"] = round(float(m.group(1)), 1)

                m = RE_SPEED.search(line)
                if m:
                    progress["speed"] = m.group(1)

                m = RE_ETA.search(line)
                if m:
                    progress["eta"] = m.group(1)

                if "Converting" in line and not "already" in line:
                    progress["status"] = "converting"
                elif progress["percent"] > 0 and progress["percent"] < 100:
                    progress["status"] = "downloading"
                elif progress["percent"] == 100:
                    progress["status"] = "processing"

                _write()
        except Exception as e:
            logger.debug(f"Progress reader error: {e}")

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t, out_lines


def process_job(filepath, logger):
    basename = os.path.basename(filepath)
    job_id = os.path.splitext(basename)[0]

    try:
        with open(filepath, "r") as f:
            raw = f.read().strip()
    except (OSError, IOError) as e:
        logger.error(f"[{job_id}] Cannot read job file: {e}")
        return

    valid, url_or_error = validate_url(raw)
    if not valid:
        logger.error(f"[{job_id}] Invalid URL: {url_or_error}")
        failed_path = filepath + FAILED_EXT
        os.rename(filepath, failed_path)
        return

    url = url_or_error

    existing = find_duplicate_url(url, filepath)
    if existing:
        logger.info(f"[{job_id}] Skipping duplicate of {existing}")
        os.remove(filepath)
        return

    logger.info(f"[{job_id}] Processing: {url}")

    processing_path = filepath + PROCESSING_EXT
    try:
        os.rename(filepath, processing_path)
    except OSError:
        logger.error(f"[{job_id}] Failed to claim job file (race)")
        return

    try:
        prefix = str(uuid.uuid4())[:8]
        cmd = [
            "yt-dlp",
            "--extract-audio",
            "--audio-format", CONFIG["AUDIO_FORMAT"],
            "--audio-quality", CONFIG["AUDIO_QUALITY"],
            "--downloader", "aria2c",
            "--downloader-args", f"aria2c:{CONFIG['ARIA2_ARGS']}",
            "--output", os.path.join(CONFIG["DOWNLOAD_DIR"], f"{prefix}_%(title)s.%(ext)s"),
            "--print", "after_move:filepath",
            "--no-overwrites",
            "--ignore-errors",
            "--newline",
            url,
        ]

        progress_path = processing_path + PROGRESS_EXT
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        progress = {"percent": 0, "speed": "", "eta": "", "item": 0, "total": 0, "status": "starting"}
        out_lines = []

        def _write_progress():
            try:
                with open(progress_path, "w") as f:
                    json.dump(progress, f)
            except OSError:
                pass

        def _read_stderr():
            try:
                for line in proc.stderr:
                    line = line.strip()
                    if not line:
                        continue
                    m = RE_ITEM.search(line)
                    if m:
                        progress["item"] = int(m.group(2))
                        progress["total"] = int(m.group(3))
                    m = RE_PCT.search(line)
                    if m:
                        progress["percent"] = round(float(m.group(1)), 1)
                    m = RE_SPEED.search(line)
                    if m:
                        progress["speed"] = m.group(1)
                    m = RE_ETA.search(line)
                    if m:
                        progress["eta"] = m.group(1)
                    if "Converting" in line and "already" not in line:
                        progress["status"] = "converting"
                    elif progress["percent"] >= 100:
                        progress["status"] = "processing"
                    elif progress["percent"] > 0:
                        progress["status"] = "downloading"
                    _write_progress()
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        for line in iter(proc.stdout.readline, ""):
            line = line.strip()
            if line:
                out_lines.append(line)

        proc.wait(timeout=3600)
        progress["status"] = "done"
        _write_progress()

        if os.path.exists(progress_path):
            try:
                os.remove(progress_path)
            except OSError:
                pass

        if proc.returncode == 0:
            processed_count = 0
            for fpath in out_lines:
                if os.path.exists(fpath):
                    logger.info(f"[{job_id}] Downloaded: {fpath}")
                    if tag_and_organize(fpath, CONFIG["PROCESSED_DIR"]):
                        processed_count += 1
                    else:
                        logger.warning(f"[{job_id}] Tagging skipped for {os.path.basename(fpath)}")
                else:
                    logger.warning(f"[{job_id}] File not found: {fpath}")
            if out_lines:
                logger.info(f"[{job_id}] Organized {processed_count}/{len(out_lines)} tracks")
            else:
                logger.warning(f"[{job_id}] yt-dlp succeeded but no file paths in output")
            os.remove(processing_path)
        else:
            stderr_output = ""
            try:
                remaining = proc.stderr.read()
                if remaining:
                    stderr_output = remaining.strip()
            except Exception:
                pass
            err_msg = stderr_output[:500] if stderr_output else "unknown error"
            logger.error(f"[{job_id}] yt-dlp failed (code {proc.returncode}): {err_msg}")
            os.rename(processing_path, processing_path + FAILED_EXT)

    except subprocess.TimeoutExpired:
        logger.error(f"[{job_id}] Download timed out")
        if proc:
            proc.kill()
        os.rename(processing_path, processing_path + FAILED_EXT)
    except FileNotFoundError as e:
        logger.critical(f"Missing dependency: {e}")
        os.rename(processing_path, processing_path + FAILED_EXT)
    except Exception as e:
        logger.error(f"[{job_id}] Unexpected error: {e}")
        os.rename(processing_path, processing_path + FAILED_EXT)


def worker_loop(logger, worker_index):
    global running
    while running:
        cfg = load_config()
        max_conc = cfg.get("MAX_CONCURRENT", 2)

        if worker_index >= max_conc:
            time.sleep(2)
            continue

        job_path = None
        with _queue_lock:
            try:
                active = sum(1 for f in os.listdir(cfg["QUEUE_DIR"]) if f.endswith(PROCESSING_EXT))
                if active >= max_conc:
                    job_path = None
                else:
                    for fname in sorted(os.listdir(cfg["QUEUE_DIR"])):
                        if fname.endswith(QUEUE_EXT):
                            job_path = os.path.join(cfg["QUEUE_DIR"], fname)
                            break
            except OSError:
                pass

        if job_path:
            process_job(job_path, logger)
        else:
            time.sleep(cfg.get("POLL_INTERVAL", 1.0))


def signal_handler(signum, frame):
    global running
    logger = logging.getLogger("music_daemon")
    logger.info(f"Received signal {signum}, shutting down...")
    running = False


def main():
    global running
    logger = setup_logging()
    ensure_dirs(logger)
    recover_orphaned_processing(logger)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("=== Music Daemon started ===")
    logger.info(f"Queue:      {CONFIG['QUEUE_DIR']}")
    logger.info(f"Downloads:  {CONFIG['DOWNLOAD_DIR']}")
    logger.info(f"Processed:  {CONFIG['PROCESSED_DIR']}")

    max_workers = CONFIG.get("MAX_CONCURRENT", 2) * 2
    logger.info(f"Starting {max_workers} download workers (adjustable via MAX_CONCURRENT)")
    workers = []
    for i in range(max_workers):
        t = threading.Thread(target=worker_loop, args=(logger, i), daemon=True, name=f"worker-{i}")
        t.start()
        workers.append(t)

    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        running = False

    logger.info("=== Music Daemon stopped ===")


if __name__ == "__main__":
    main()
