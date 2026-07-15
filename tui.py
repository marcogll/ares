import os
import sys
import uuid
import subprocess
import atexit
from pathlib import Path

BASE = Path.home() / "Music" / "ares"
QUEUE_DIR = str(BASE / ".queue")
DOWNLOAD_DIR = str(BASE / "downloaded")
PROCESSED_DIR = str(BASE / "processed")
DAEMON_SCRIPT = str(Path(__file__).parent / "music_daemon.py")
LOG_FILE = str(Path.home() / "Library" / "Logs" / "music-daemon" / "daemon.log")

daemon_proc = None

QUEUE_EXTS = (".url", ".processing", ".failed")


def ensure_dirs():
    for d in [QUEUE_DIR, DOWNLOAD_DIR, PROCESSED_DIR]:
        os.makedirs(d, exist_ok=True)


def start_daemon():
    global daemon_proc
    try:
        daemon_proc = subprocess.Popen(
            [sys.executable, DAEMON_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Daemon started (PID: {daemon_proc.pid})")
    except OSError as e:
        print(f"Failed to start daemon: {e}")
        sys.exit(1)


def stop_daemon():
    global daemon_proc
    if daemon_proc and daemon_proc.poll() is None:
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=3)
            print("Daemon stopped")
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
            print("Daemon killed (force)")
    daemon_proc = None


def is_url_queued(url):
    url = url.strip()
    for fname in os.listdir(QUEUE_DIR):
        if not fname.endswith(QUEUE_EXTS):
            continue
        try:
            with open(os.path.join(QUEUE_DIR, fname)) as f:
                if f.read().strip() == url:
                    return True, fname
        except OSError:
            continue
    return False, None


def send_to_daemon(url):
    url = url.strip()
    dup, fname = is_url_queued(url)
    if dup:
        print(f"Duplicate — already queued as {fname}")
        return None

    job_id = str(uuid.uuid4())[:8]
    filepath = os.path.join(QUEUE_DIR, f"{job_id}.url")
    try:
        with open(filepath, "w") as f:
            f.write(url + "\n")
        return job_id
    except OSError as e:
        print(f"Error writing job: {e}")
        return None


def show_status():
    alive = daemon_proc and daemon_proc.poll() is None
    print(f"\nDaemon: {'RUNNING' if alive else 'STOPPED'}{f' (PID: {daemon_proc.pid})' if alive else ''}")

    pending = processing = failed = 0
    for fname in os.listdir(QUEUE_DIR):
        if fname.endswith(".url"):
            pending += 1
        elif fname.endswith(".processing"):
            processing += 1
        elif fname.endswith(".failed"):
            failed += 1
    print(f"Queue:     {pending} pending, {processing} processing, {failed} failed")

    dl_count = len([f for f in os.listdir(DOWNLOAD_DIR) if not f.startswith(".")])
    print(f"Downloaded: {dl_count} files")

    processed_albums = []
    try:
        for entry in sorted(os.listdir(PROCESSED_DIR)):
            album_dir = os.path.join(PROCESSED_DIR, entry)
            if os.path.isdir(album_dir):
                song_count = len([f for f in os.listdir(album_dir) if f.endswith(".mp3")])
                processed_albums.append(f"  {entry} ({song_count} tracks)")
    except OSError:
        pass

    processed_total = sum(
        len([f for f in os.listdir(os.path.join(PROCESSED_DIR, d)) if f.endswith(".mp3")])
        for d in os.listdir(PROCESSED_DIR)
        if os.path.isdir(os.path.join(PROCESSED_DIR, d))
    )
    print(f"Processed:  {processed_total} tracks in {len(processed_albums)} albums")
    if processed_albums:
        for album_line in processed_albums[-5:]:
            print(album_line)

    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        recent = [l.strip() for l in lines[-5:] if l.strip()]
        if recent:
            print("\nRecent log:")
            for l in recent:
                print(f"  {l}")
    except OSError:
        pass


def handle_command(raw):
    cmd = raw.strip().lower()

    if cmd in ("exit", "quit"):
        return None

    if cmd == "status":
        show_status()
        return True

    if cmd == "retry":
        retry_failed()
        return True

    if not cmd:
        return True

    if not (cmd.startswith("http://") or cmd.startswith("https://")):
        print("Commands: exit, status, retry, or paste a URL")
        return True

    return cmd


def retry_failed():
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
                print(f"Re-queued: {job_id}")
            except OSError as e:
                print(f"Error retrying {fname}: {e}")
    if count == 0:
        print("No failed jobs found")


def main():
    global daemon_proc
    ensure_dirs()
    start_daemon()
    atexit.register(stop_daemon)

    print()
    print("--- Music Downloader ---")
    print(f"Commands: <URL>, status, retry, exit")
    print()

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        result = handle_command(raw)
        if result is None:
            break
        if result is True:
            continue

        job_id = send_to_daemon(result)
        if job_id:
            print(f"Sent to daemon (job: {job_id})")
        else:
            print("Failed to send job")

    stop_daemon()


if __name__ == "__main__":
    main()
