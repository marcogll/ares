import os
import re
import json
import shutil
import logging
from datetime import datetime
from pathlib import Path
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TPE1, TIT2, TALB, TRCK, error as MutagenError

log = logging.getLogger("music_daemon.metadata")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
ARTWORK_SIZE_SMALL = "100x100bb"
ARTWORK_SIZE_LARGE = "1000x1000bb"
REQUEST_TIMEOUT = 15

METADATA_LOG = str(Path.home() / "Library" / "Logs" / "music-daemon" / "metadata.jsonl")


def _normalize_itunes(t):
    return {
        "artistName": t.get("artistName", ""),
        "trackName": t.get("trackName", ""),
        "collectionName": t.get("collectionName", ""),
        "trackNumber": t.get("trackNumber", 0) or 0,
        "artworkUrl": t.get("artworkUrl100", ""),
        "source": "iTunes",
    }


def _normalize_deezer(t):
    return {
        "artistName": t.get("artist", {}).get("name", ""),
        "trackName": t.get("title", ""),
        "collectionName": t.get("album", {}).get("title", ""),
        "trackNumber": t.get("track_position", 0) or 0,
        "artworkUrl": t.get("album", {}).get("cover_medium", ""),
        "source": "Deezer",
    }


def _write_metadata_log(entry):
    try:
        os.makedirs(os.path.dirname(METADATA_LOG), exist_ok=True)
        with open(METADATA_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


RE_TITLE_ARTIST = re.compile(r'^(.+?)\s*[–\-—]\s*(.+)$')
RE_TITLE_SUFFIX = re.compile(
    r'\s*\((?:'
    r'[Oo]fficial\s*(?:[Mm]usic\s*)?[Vv]ideo'
    r'|[Oo]fficial\s*[Ll]yrics?\s*[Vv]ideo'
    r'|[Oo]fficial\s*[Aa]udio'
    r'|[Ll]yrics?\s*[Vv]ideo'
    r'|[Ll]yrics'
    r'|[Aa]udio'
    r'|[Ee]xplicit'
    r')\s*\)\s*$'
)


def parse_youtube_title(title):
    title = title.strip()
    if not title:
        return None
    title_clean = re.sub(RE_TITLE_SUFFIX, '', title).strip()
    m = RE_TITLE_ARTIST.match(title_clean)
    if m:
        artist = m.group(1).strip()
        track = m.group(2).strip()
        return {"artistName": artist, "trackName": track, "source": "YouTube title"}
    return None


def extract_title_from_filename(filepath):
    basename = os.path.splitext(os.path.basename(filepath))[0]
    cleaned = re.sub(r"^[a-f0-9]{8}_", "", basename)
    return cleaned


def sanitize_path(name):
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Unknown"


def strip_features(name):
    return re.sub(
        r"\s*[([{]?\s*(?:feat\.|ft\.|featuring)\s*[^)\]]*[\])]?\s*",
        " ", name, flags=re.IGNORECASE
    ).strip()


def extract_main_artist(artist):
    cleaned = re.sub(
        r"\s*[([{]?\s*(?:feat\.|ft\.|featuring)\s*[^)\]]*[\])]?\s*",
        "", artist, flags=re.IGNORECASE
    ).strip()
    return cleaned or artist


def sanitize_search_term(filepath):
    basename = os.path.splitext(os.path.basename(filepath))[0]
    cleaned = re.sub(r"^[a-f0-9]{8}_", "", basename)
    cleaned = re.sub(r"[^\w\s\-'.&(),!]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = strip_features(cleaned)
    return cleaned[:200]


def search_itunes_track(search_term):
    params = {"term": search_term, "entity": "song", "limit": 1}
    try:
        resp = requests.get(ITUNES_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("resultCount", 0) > 0:
            return _normalize_itunes(data["results"][0])
        log.info(f"No iTunes results for: {search_term}")
        return None
    except requests.RequestException as e:
        log.warning(f"iTunes API error: {e}")
        return None


def search_deezer_track(search_term):
    params = {"q": search_term, "limit": 1}
    try:
        resp = requests.get(DEEZER_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("total", 0) > 0:
            return _normalize_deezer(data["data"][0])
        log.info(f"No Deezer results for: {search_term}")
        return None
    except requests.RequestException as e:
        log.warning(f"Deezer API error: {e}")
        return None


def search_track(search_term):
    track = search_itunes_track(search_term)
    if track:
        return track
    log.info("Falling back to Deezer API")
    return search_deezer_track(search_term)


def fetch_album_artwork(artwork_url):
    if not artwork_url:
        return None
    if "100x100bb" in artwork_url:
        artwork_url = artwork_url.replace(ARTWORK_SIZE_SMALL, ARTWORK_SIZE_LARGE)
    try:
        resp = requests.get(artwork_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "jpeg" in content_type or "jpg" in content_type:
            mime = "image/jpeg"
        elif "png" in content_type:
            mime = "image/png"
        else:
            mime = "image/jpeg"
        return (mime, resp.content)
    except requests.RequestException as e:
        log.warning(f"Failed to fetch artwork: {e}")
        return None


def apply_metadata(filepath, track, artwork_data):
    try:
        audio = MP3(filepath, ID3=ID3)
    except MutagenError as e:
        log.error(f"Cannot read MP3 tags: {e}")
        return False

    try:
        audio.tags.add(TPE1(encoding=3, text=track["artistName"]))
        audio.tags.add(TIT2(encoding=3, text=track["trackName"]))
        audio.tags.add(TALB(encoding=3, text=track["collectionName"]))
        if track.get("trackNumber"):
            audio.tags.add(TRCK(encoding=3, text=str(track["trackNumber"])))

        if artwork_data:
            mime, img_bytes = artwork_data
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Front cover", data=img_bytes))

        audio.save()
        log.info(f"Applied {track['source']} metadata: {track['trackName']} - {track['artistName']}")
        return True
    except MutagenError as e:
        log.error(f"Failed to write tags: {e}")
        return False


def _normalize_folder_name(name):
    lowered = name.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered


def _find_existing_album_dir(processed_base_dir, artist, album):
    target = f"{artist} - {album}"
    target_norm = _normalize_folder_name(target)
    if not os.path.isdir(processed_base_dir):
        return None, target
    for entry in os.listdir(processed_base_dir):
        if _normalize_folder_name(entry) == target_norm:
            return os.path.join(processed_base_dir, entry), entry
    return None, target


def tag_and_organize(filepath, processed_base_dir):
    if not os.path.exists(filepath):
        log.error(f"File not found: {filepath}")
        return False

    if not filepath.lower().endswith(".mp3"):
        log.warning(f"Skipping non-MP3 file: {filepath}")
        return False

    video_title = extract_title_from_filename(filepath)
    parsed = parse_youtube_title(video_title)

    # Try to get album metadata from APIs
    search_term = sanitize_search_term(filepath)
    api_track = search_track(search_term)

    if parsed:
        track = {
            "artistName": parsed["artistName"],
            "trackName": parsed["trackName"],
            "collectionName": api_track["collectionName"] if api_track else "Unknown Album",
            "trackNumber": api_track.get("trackNumber", 0) if api_track else 0,
            "artworkUrl": api_track.get("artworkUrl", "") if api_track else "",
            "source": f"YouTube title + {api_track['source']}" if api_track else "YouTube title (no album)",
        }
        log.info(f"Parsed from title: {track['artistName']} - {track['trackName']}")
    elif api_track:
        track = api_track
    else:
        log.warning(f"No metadata found for: {filepath}")
        return False

    artwork = fetch_album_artwork(track.get("artworkUrl", ""))
    apply_metadata(filepath, track, artwork)

    # Re-read tags from file as source of truth
    try:
        audio = MP3(filepath, ID3=ID3)
        raw_artist = str(audio.tags.get("TPE1", "Unknown Artist"))
        main_artist = extract_main_artist(sanitize_path(raw_artist))
        album = sanitize_path(str(audio.tags.get("TALB", "Unknown Album")))
        track_name = sanitize_path(str(audio.tags.get("TIT2", "Unknown Track")))
        trck = audio.tags.get("TRCK")
        track_num = 0
        if trck:
            try:
                track_num = int(str(trck).split("/")[0])
            except ValueError:
                pass
    except Exception as e:
        log.error(f"Failed to re-read tags: {e}")
        raw_artist = track["artistName"]
        main_artist = extract_main_artist(sanitize_path(raw_artist))
        album = sanitize_path(track["collectionName"]) or "Unknown Album"
        track_num = track.get("trackNumber", 0)
        track_name = sanitize_path(track["trackName"]) or "Unknown Track"

    existing_dir, target_name = _find_existing_album_dir(processed_base_dir, main_artist, album)
    if existing_dir:
        dest_dir = existing_dir
    else:
        dest_dir = os.path.join(processed_base_dir, target_name)
        os.makedirs(dest_dir, exist_ok=True)

    ext = os.path.splitext(filepath)[1]
    dest_file = os.path.join(dest_dir, f"{track_num:02d} - {track_name}{ext}")

    base, ext = os.path.splitext(dest_file)
    counter = 1
    while os.path.exists(dest_file):
        dest_file = f"{base}_{counter}{ext}"
        counter += 1

    shutil.move(filepath, dest_file)
    log.info(f"Organized: {dest_file}")

    _write_metadata_log({
        "ts": datetime.now().isoformat(),
        "source": track["source"],
        "artist": raw_artist,
        "album": album,
        "track": track_name,
        "track_number": track_num,
        "dest": dest_file,
    })

    return True


def read_metadata_log(limit=50):
    if not os.path.exists(METADATA_LOG):
        return []
    try:
        with open(METADATA_LOG) as f:
            lines = f.readlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"Failed to read metadata log: {e}")
        return []
