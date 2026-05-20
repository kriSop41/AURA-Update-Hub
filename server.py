from flask import Flask, request, jsonify, send_from_directory
import requests
import os
from urllib.parse import urlparse, parse_qs
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from ytmusicapi import YTMusic
from flask_cors import CORS
import sqlite3
import json
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from flask_socketio import SocketIO, emit, join_room, leave_room
import traceback
import yt_dlp
from werkzeug.utils import secure_filename

app = Flask(__name__, static_url_path='', static_folder='.')
CORS(app) 
socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=60, ping_interval=25)
yt = YTMusic(auth=None)
yt.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
_spotify_token_cache = {"access_token": None, "expires_at": 0}

DEV_HUB_TOKEN_ENV_KEYS = ("AURA_DEV_HUB_TOKEN", "AURA_DEVELOPER_TOKEN")
UPDATE_STORAGE_DIR = Path(app.root_path) / "updates"
APK_UPLOAD_DIR = UPDATE_STORAGE_DIR / "apk"
UPDATE_MANIFEST_PATH = UPDATE_STORAGE_DIR / "latest_update.json"
ALLOWED_UPDATE_FILE_EXTENSIONS = {".apk"}
MAX_WHATS_NEW_ITEMS = 20


def _extract_apk_version_metadata(apk_path: Path):
    """
    Read versionCode/versionName directly from the uploaded APK manifest.
    Returns {"version_code": int, "version_name": str} or None.
    """
    try:
        from pyaxmlparser import APK as ParsedApk
    except Exception as exc:
        app.logger.warning("APK metadata parser unavailable: %s", exc)
        return None

    try:
        parsed = ParsedApk(str(apk_path))
        raw_code = str(parsed.version_code or "").strip()
        raw_name = str(parsed.version_name or "").strip()
        if not raw_code:
            return None
        version_code = int(raw_code)
        version_name = raw_name or "0.0.0"
        return {
            "version_code": version_code,
            "version_name": version_name
        }
    except Exception as exc:
        app.logger.warning("Could not extract APK metadata for %s: %s", apk_path.name, exc)
        return None


def _default_update_manifest():
    return {
        "latest_version_code": 0,
        "latest_version_name": "0.0.0",
        "apk_file": "",
        "apk_url": "",
        "catalog": [],
        "force_update": False,
        "published_at": None,
        "history": []
    }


def _resolve_dev_hub_token():
    for env_name in DEV_HUB_TOKEN_ENV_KEYS:
        token = (os.getenv(env_name) or "").strip()
        if token:
            return token
    # Keep a known fallback for local testing; override with env vars in production.
    return "aura-dev-change-me"


def _sanitize_catalog(raw_catalog):
    if isinstance(raw_catalog, list):
        raw_items = raw_catalog
    elif isinstance(raw_catalog, str):
        raw_items = raw_catalog.splitlines()
    else:
        raw_items = []

    cleaned = []
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        text = text.lstrip("-").lstrip("*").strip()
        if text:
            cleaned.append(text[:220])
    return cleaned[:MAX_WHATS_NEW_ITEMS]


def _save_update_manifest(manifest):
    UPDATE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with UPDATE_MANIFEST_PATH.open("w", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2)


def _load_update_manifest():
    if not UPDATE_MANIFEST_PATH.exists():
        default_manifest = _default_update_manifest()
        _save_update_manifest(default_manifest)
        return default_manifest

    try:
        with UPDATE_MANIFEST_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("Update manifest must be a JSON object.")
    except Exception:
        data = _default_update_manifest()
        _save_update_manifest(data)
        return data

    normalized = _default_update_manifest()
    normalized.update(data)
    normalized["latest_version_code"] = int(normalized.get("latest_version_code") or 0)
    normalized["latest_version_name"] = str(normalized.get("latest_version_name") or "0.0.0")
    normalized["apk_file"] = str(normalized.get("apk_file") or "")
    normalized["apk_url"] = str(normalized.get("apk_url") or "")
    normalized["catalog"] = _sanitize_catalog(normalized.get("catalog"))
    normalized["force_update"] = bool(normalized.get("force_update"))
    normalized["history"] = normalized.get("history") if isinstance(normalized.get("history"), list) else []
    return normalized


def _build_update_payload(manifest):
    payload = {
        "latest_version_code": int(manifest.get("latest_version_code") or 0),
        "latest_version_name": str(manifest.get("latest_version_name") or "0.0.0"),
        "apk_url": str(manifest.get("apk_url") or ""),
        "catalog": _sanitize_catalog(manifest.get("catalog")),
        "force_update": bool(manifest.get("force_update")),
        "published_at": manifest.get("published_at")
    }
    payload["available"] = payload["latest_version_code"] > 0 and bool(payload["apk_url"])
    return payload


def _is_dev_hub_authorized():
    expected_token = _resolve_dev_hub_token()
    request_json = request.get_json(silent=True) if request.is_json else {}
    provided_token = (
        request.headers.get("X-Aura-Developer-Token")
        or request.form.get("token")
        or (request_json or {}).get("token")
        or request.args.get("token")
        or ""
    )
    return provided_token.strip() == expected_token


def _init_update_storage():
    UPDATE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    APK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not UPDATE_MANIFEST_PATH.exists():
        _save_update_manifest(_default_update_manifest())

# Database Setup
def init_db():
    with sqlite3.connect('aura_users.db') as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (google_id TEXT PRIMARY KEY, email TEXT, data TEXT)''')
        conn.commit()

init_db()
_init_update_storage()

# Security: Block access to source code and config files
@app.route('/health')
def health():
    return "OK", 200

@app.before_request
def block_sensitive_files():
    if request.path.endswith('.py') or request.path in ['/requirements.txt', '/Procfile', '/.env']:
        return "Access Denied", 403

@app.route('/')
def index():
    return send_from_directory('.', 'developer_hub.html')


@app.route('/developer-hub')
def developer_hub():
    return send_from_directory('.', 'developer_hub.html')


@app.route('/downloads/<path:filename>')
def download_apk(filename):
    return send_from_directory(str(APK_UPLOAD_DIR), filename, as_attachment=True)


@app.route('/api/app-update/latest')
def latest_app_update():
    manifest = _load_update_manifest()
    payload = _build_update_payload(manifest)

    apk_url = payload.get("apk_url", "")
    if apk_url and apk_url.startswith("/"):
        payload["apk_url"] = f"{request.url_root.rstrip('/')}{apk_url}"

    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route('/api/app-update/publish', methods=['POST'])
def publish_app_update():
    if not _is_dev_hub_authorized():
        return jsonify({'error': 'Unauthorized developer token'}), 401

    version_code_raw = request.form.get("versionCode") or request.form.get("latest_version_code")
    version_name_input = (
        request.form.get("versionName")
        or request.form.get("latest_version_name")
        or ""
    ).strip()
    catalog_input = (
        request.form.get("catalog")
        or request.form.get("whatsNew")
        or ""
    )
    force_update_raw = (request.form.get("forceUpdate") or "false").strip().lower()
    force_update = force_update_raw in {"1", "true", "yes", "on"}
    apk_file = request.files.get("apk")

    manifest = _load_update_manifest()
    previous_entry = _build_update_payload(manifest)
    if previous_entry.get("available"):
        history = manifest.get("history", [])
        history.insert(0, previous_entry)
        manifest["history"] = history[:20]

    apk_metadata = None

    if apk_file and apk_file.filename:
        safe_name = secure_filename(apk_file.filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in ALLOWED_UPDATE_FILE_EXTENSIONS:
            return jsonify({'error': 'Only .apk files are supported'}), 400

        stem = Path(safe_name).stem or "aura-update"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        final_name = f"{stem}-{stamp}{extension}"
        target_path = APK_UPLOAD_DIR / final_name
        apk_file.save(target_path)

        manifest["apk_file"] = final_name
        manifest["apk_url"] = f"/downloads/{final_name}"
        apk_metadata = _extract_apk_version_metadata(target_path)
    elif not manifest.get("apk_url"):
        return jsonify({'error': 'Upload an APK for the first published update'}), 400

    metadata_source = "manual"
    if apk_metadata is not None:
        version_code = apk_metadata["version_code"]
        version_name = apk_metadata["version_name"]
        metadata_source = "apk_manifest"
    else:
        if not version_code_raw:
            return jsonify({'error': 'versionCode is required when APK metadata cannot be read'}), 400
        if not version_name_input:
            return jsonify({'error': 'versionName is required when APK metadata cannot be read'}), 400
        try:
            version_code = int(version_code_raw)
        except ValueError:
            return jsonify({'error': 'versionCode must be a valid integer'}), 400
        version_name = version_name_input

    if version_code < int(manifest.get("latest_version_code") or 0):
        return jsonify({
            'error': (
                f'Cannot publish versionCode {version_code} because latest published '
                f'versionCode is {manifest.get("latest_version_code", 0)}.'
            )
        }), 400

    manifest["latest_version_code"] = version_code
    manifest["latest_version_name"] = version_name
    manifest["catalog"] = _sanitize_catalog(catalog_input)
    manifest["force_update"] = force_update
    manifest["published_at"] = datetime.now(timezone.utc).isoformat()

    _save_update_manifest(manifest)

    response_payload = _build_update_payload(manifest)
    if response_payload["apk_url"].startswith("/"):
        response_payload["apk_url"] = f"{request.url_root.rstrip('/')}{response_payload['apk_url']}"

    response = jsonify({
        'status': 'ok',
        'message': f'Update {version_name} ({version_code}) published successfully.',
        'update': response_payload,
        'metadata_source': metadata_source
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route('/search')
def search():
    print(f"Search Request: {request.args.get('q')}") # Debug log
    try:
        query = request.args.get('q')
        if not query:
            return jsonify([])

        # Optional raw mode for legacy call sites (e.g. fallback video search).
        search_filter = request.args.get('type')
        if search_filter:
            return jsonify(yt.search(query, filter=search_filter))

        songs_raw = yt.search(query, filter='songs', limit=20)
        albums_raw = yt.search(query, filter='albums', limit=12)
        artists_raw = yt.search(query, filter='artists', limit=3)

        songs = [
            formatted_track for track in songs_raw
            if (formatted_track := _format_track(track)) is not None
        ]

        def _format_album(album):
            if not album:
                return None
            browse_id = album.get('browseId') or album.get('id')
            if not browse_id:
                return None

            thumbs = album.get('thumbnails') or album.get('thumbnail')
            thumb_url = ''
            if isinstance(thumbs, list) and thumbs:
                last_thumb = thumbs[-1]
                thumb_url = last_thumb.get('url', '') if isinstance(last_thumb, dict) else str(last_thumb)
            elif isinstance(thumbs, dict):
                thumb_url = thumbs.get('url', '')

            artists = album.get('artists') or []
            artist_name = artists[0].get('name', 'Unknown') if artists and isinstance(artists[0], dict) else 'Unknown'
            media_type = (album.get('type') or album.get('resultType') or '').lower()
            year_value = album.get('year')
            is_single = ('single' in media_type) or (isinstance(year_value, str) and year_value.lower() == 'single')

            return {
                'browseId': browse_id,
                'title': album.get('title', 'Untitled Album'),
                'artist': artist_name,
                'thumb': thumb_url,
                'year': year_value,
                'isSingle': is_single
            }

        albums = []
        singles = []
        seen_media_ids = set()

        for item in albums_raw:
            formatted = _format_album(item)
            if not formatted:
                continue
            media_id = formatted['browseId']
            if media_id in seen_media_ids:
                continue
            seen_media_ids.add(media_id)
            if formatted.get('isSingle'):
                singles.append(formatted)
            else:
                albums.append(formatted)

        # If query looks like an artist, include their catalog albums/singles as well.
        if artists_raw:
            first_artist = artists_raw[0]
            artist_id = first_artist.get('browseId') or first_artist.get('id')
            if artist_id:
                try:
                    artist_details = yt.get_artist(artist_id)
                    for section_name in ['albums', 'singles']:
                        section = artist_details.get(section_name) or {}
                        for album in section.get('results', []):
                            formatted = _format_album(album)
                            if not formatted:
                                continue
                            media_id = formatted['browseId']
                            if media_id in seen_media_ids:
                                continue
                            seen_media_ids.add(media_id)
                            if section_name == 'singles' or formatted.get('isSingle'):
                                singles.append(formatted)
                            else:
                                albums.append(formatted)
                except Exception as artist_err:
                    print(f"Artist album enrichment failed: {artist_err}")

        return jsonify({
            'songs': songs,
            'albums': albums[:30],
            'singles': singles[:30]
        })
    except Exception as e:
        print(f"Search Error: {e}") # Check Render Logs for this
        return jsonify({'error': str(e)}), 500

@app.route('/album')
def album():
    try:
        browse_id = request.args.get('browseId')
        if not browse_id:
            return jsonify({'error': 'browseId is required'}), 400

        album_data = yt.get_album(browse_id)
        tracks = [
            formatted_track for track in album_data.get('tracks', [])
            if (formatted_track := _format_track(track)) is not None
        ]
        return jsonify({
            'title': album_data.get('title', 'Album'),
            'artist': ', '.join([a.get('name', '') for a in album_data.get('artists', []) if isinstance(a, dict)]).strip(', '),
            'tracks': tracks
        })
    except Exception as e:
        print(f"Album Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/artist_songs')
def artist_songs():
    try:
        browse_id = (request.args.get('browseId') or '').strip()
        artist_name = (request.args.get('name') or request.args.get('artist') or '').strip()
        limit_raw = request.args.get('limit') or '20'
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 40))

        tracks = []
        if browse_id:
            try:
                artist_data = yt.get_artist(browse_id)
                if not artist_name:
                    artist_name = (artist_data.get('name') or '').strip()
                songs_block = artist_data.get('songs') or {}
                songs_results = songs_block.get('results', []) if isinstance(songs_block, dict) else []
                tracks.extend(
                    formatted_track for song in songs_results
                    if (formatted_track := _format_track(song)) is not None
                )
            except Exception as browse_err:
                print(f"Artist songs browse lookup failed: {browse_err}")

        if not tracks and artist_name:
            strict_query = f"\"{artist_name}\""
            primary_results = yt.search(strict_query, filter='songs', limit=max(limit * 3, 20))
            fallback_results = yt.search(artist_name, filter='songs', limit=max(limit * 3, 20))
            tracks.extend(
                formatted_track for song in (primary_results + fallback_results)
                if (formatted_track := _format_track(song)) is not None
            )

        normalized_target = _normalize_text(_primary_artist_name(artist_name))
        if normalized_target:
            strict_matches = [track for track in tracks if _track_matches_artist_name(track, normalized_target)]
            if strict_matches:
                tracks = strict_matches

        seen = set()
        deduped = []
        for track in tracks:
            key = track.get('id')
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(track)
            if len(deduped) >= limit:
                break

        return jsonify({
            'artist': artist_name,
            'tracks': deduped
        })
    except Exception as e:
        print(f"Artist songs error: {e}")
        return jsonify({'error': str(e), 'tracks': []}), 500


@app.route('/video_lookup')
def video_lookup():
    try:
        title = (request.args.get('title') or '').strip()
        artist = (request.args.get('artist') or '').strip()
        current_id = (request.args.get('id') or '').strip()

        if not title:
            return jsonify({'available': False})

        query = f"{title} {artist} official video".strip()
        results = yt.search(query, filter='videos', limit=8)

        chosen = None
        for item in results:
            vid = item.get('videoId')
            if not vid:
                continue
            if current_id and vid == current_id:
                continue
            chosen = item
            break

        if not chosen:
            return jsonify({'available': False})

        thumbs = chosen.get('thumbnails') or []
        thumb_url = ''
        if isinstance(thumbs, list) and thumbs:
            last_thumb = thumbs[-1]
            thumb_url = last_thumb.get('url', '') if isinstance(last_thumb, dict) else str(last_thumb)

        return jsonify({
            'available': True,
            'videoId': chosen.get('videoId'),
            'title': chosen.get('title'),
            'thumb': thumb_url
        })
    except Exception as e:
        print(f"Video Lookup Error: {e}")
        return jsonify({'available': False, 'error': str(e)}), 500

@app.route('/audio_stream')
def audio_stream():
    video_id = (request.args.get('id') or '').strip()
    if not video_id:
        return jsonify({'error': 'id is required'}), 400

    try:
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'noplaylist': True,
            'format': 'bestaudio/best',
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            'http_headers': {
                'User-Agent': yt.headers.get('User-Agent') or 'Mozilla/5.0'
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)

        formats = info.get('formats') or []
        audio_formats = [
            fmt for fmt in formats
            if fmt.get('url') and fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none'
        ]
        chosen = sorted(
            audio_formats or [fmt for fmt in formats if fmt.get('url') and fmt.get('acodec') != 'none'],
            key=lambda fmt: (
                fmt.get('abr') or 0,
                fmt.get('tbr') or 0,
                fmt.get('filesize') or fmt.get('filesize_approx') or 0
            ),
            reverse=True
        )[0]

        return jsonify({
            'id': video_id,
            'url': chosen.get('url'),
            'mimeType': chosen.get('mime_type') or chosen.get('ext') or 'audio',
            'duration': int(info.get('duration') or 0),
            'title': info.get('title') or '',
            'artist': info.get('artist') or info.get('uploader') or '',
            'expiresAt': int(time.time()) + 3000
        })
    except Exception as e:
        print(f"Audio Stream Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/lyrics')
def lyrics():
    title = request.args.get('title')
    artist = request.args.get('artist')
    
    # Try fetching synced lyrics from LRCLIB (often sources from Musixmatch/Spotify)
    if title and artist:
        # Clean title: remove (Official Video), [Lyrics], etc. for better matching
        clean_title = title.split('(')[0].split('[')[0].strip()
        try:
            resp = requests.get("https://lrclib.net/api/get", params={'artist_name': artist, 'track_name': clean_title})
            data = resp.json()
            if data.get('syncedLyrics'):
                return jsonify({'lyrics': data['syncedLyrics'], 'synced': True})
            if data.get('plainLyrics'):
                return jsonify({'lyrics': data['plainLyrics'], 'synced': False})
        except (requests.exceptions.RequestException, json.JSONDecodeError): pass

    video_id = request.args.get('id')
    if not video_id: return jsonify({'lyrics': ''})
    try:
        watch_playlist = yt.get_watch_playlist(videoId=video_id)
        lyrics_id = watch_playlist.get('lyrics')
        if lyrics_id:
            lyrics_data = yt.get_lyrics(lyrics_id)
            return jsonify({'lyrics': lyrics_data['lyrics'], 'synced': False})
    except Exception: pass

    # Fallback: If direct ID failed, search for the official song on YT Music
    if title and artist:
        try:
            search_results = yt.search(f"{title} {artist}", filter="songs")
            if search_results:
                official_id = search_results[0]['videoId']
                if official_id != video_id:
                    watch_playlist = yt.get_watch_playlist(videoId=official_id)
                    lyrics_id = watch_playlist.get('lyrics')
                    if lyrics_id:
                        lyrics_data = yt.get_lyrics(lyrics_id)
                        return jsonify({'lyrics': lyrics_data['lyrics'], 'synced': False})
        except Exception: pass

    return jsonify({'lyrics': 'Lyrics not available.'})

def parse_duration_from_string(duration_str):
    if duration_str is None:
        return 0
    if isinstance(duration_str, (int, float)):
        return int(duration_str)
    parts = duration_str.split(':')
    seconds = 0
    try:
        if len(parts) == 2:
            seconds = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        return 0
    return seconds


def _normalize_text(value):
    if value is None:
        return ''
    cleaned = ''.join(ch if str(ch).isalnum() else ' ' for ch in str(value).lower())
    return ' '.join(cleaned.split())


def _primary_artist_name(name):
    text = str(name or '')
    separators = [',', '&', ' feat ', ' feat. ', ' featuring ', ' ft ', ' ft. ', ' x ']
    lowered = text.lower()
    cut_index = len(text)
    for sep in separators:
        idx = lowered.find(sep)
        if idx != -1:
            cut_index = min(cut_index, idx)
    return text[:cut_index].strip() if cut_index < len(text) else text.strip()


def _track_matches_artist_name(track, normalized_artist):
    if not normalized_artist:
        return True
    artist_name = track.get('artist', '')
    normalized_primary = _normalize_text(_primary_artist_name(artist_name))
    normalized_full = _normalize_text(artist_name)
    if normalized_primary == normalized_artist or normalized_full == normalized_artist:
        return True
    if len(normalized_artist) >= 3:
        wrapped = f" {normalized_full} "
        needle = f" {normalized_artist} "
        return needle in wrapped
    return False

def _format_track(track):
    """Helper to format track data consistently."""
    if not track or not track.get('videoId'):
        return None
    
    artist_name = 'Unknown'
    artist_id = None
    if track.get('artists'):
        artist_name = track['artists'][0].get('name', 'Unknown')
        artist_id = track['artists'][0].get('id')

    thumbs = track.get('thumbnails') or track.get('thumbnail')
    thumb_url = ''
    if isinstance(thumbs, list) and thumbs:
        last_thumb = thumbs[-1]
        thumb_url = last_thumb.get('url', '') if isinstance(last_thumb, dict) else str(last_thumb)
    elif isinstance(thumbs, dict):
        thumb_url = thumbs.get('url', '')

    video_id = track.get('videoId')
    if not thumb_url and video_id:
        thumb_url = f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'

    return {
        'id': track['videoId'],
        'title': track.get('title', 'Untitled'),
        'artist': artist_name,
        'artistId': artist_id,
        'thumb': thumb_url,
        'duration': parse_duration_from_string(track.get('duration'))
    }

def _get_spotify_token():
    client_id = os.getenv('9cbca88a09d94c37a177a768d95ff749')
    client_secret = os.getenv('0f2d6701477c4cf19fa5c4fc0fc162d5')
    if not client_id or not client_secret:
        return None, 'Spotify credentials are not configured on the server.'

    now = int(time.time())
    if _spotify_token_cache["access_token"] and now < _spotify_token_cache["expires_at"]:
        return _spotify_token_cache["access_token"], None

    try:
        res = requests.post(
            'https://accounts.spotify.com/api/token',
            data={'grant_type': 'client_credentials'},
            auth=(client_id, client_secret),
            timeout=10
        )
        if res.status_code != 200:
            return None, f"Spotify auth failed: {res.text}"
        data = res.json()
        token = data.get('access_token')
        expires_in = int(data.get('expires_in', 3600))
        if not token:
            return None, 'Spotify auth failed: missing access token.'
        _spotify_token_cache["access_token"] = token
        _spotify_token_cache["expires_at"] = now + max(60, expires_in - 30)
        return token, None
    except Exception as e:
        return None, f"Spotify auth error: {e}"

def _parse_spotify_playlist_id(url):
    if url.startswith('spotify:playlist:'):
        parts = url.split(':')
        return parts[-1] if parts else None

    parsed = urlparse(url)
    if 'spotify.com' not in parsed.netloc:
        return None
    path_parts = parsed.path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[0] == 'playlist':
        return path_parts[1]
    return None

def _fetch_spotify_playlist_tracks(playlist_id, token, max_tracks=200):
    headers = {'Authorization': f'Bearer {token}'}
    res = requests.get(f'https://api.spotify.com/v1/playlists/{playlist_id}', headers=headers, timeout=10)
    if res.status_code != 200:
        raise Exception(f"Spotify playlist fetch failed: {res.text}")
    data = res.json()
    playlist_name = data.get('name', 'Imported Spotify Playlist')
    tracks = []

    def _extract_items(items):
        for item in items:
            track = item.get('track')
            if not track or track.get('is_local'):
                continue
            title = track.get('name')
            artists = track.get('artists') or []
            artist_name = artists[0].get('name') if artists and isinstance(artists[0], dict) else ''
            if title:
                tracks.append({'title': title, 'artist': artist_name})
                if len(tracks) >= max_tracks:
                    return True
        return False

    items = data.get('tracks', {}).get('items', [])
    if _extract_items(items):
        return playlist_name, tracks

    next_url = data.get('tracks', {}).get('next')
    while next_url and len(tracks) < max_tracks:
        page = requests.get(next_url, headers=headers, timeout=10)
        if page.status_code != 200:
            break
        page_data = page.json()
        items = page_data.get('items', [])
        if _extract_items(items):
            break
        next_url = page_data.get('next')

    return playlist_name, tracks

def _resolve_spotify_tracks_to_youtube(tracks):
    resolved = []
    for item in tracks:
        title = item.get('title', '').strip()
        artist = item.get('artist', '').strip()
        if not title:
            continue
        query = f"{title} {artist}".strip()
        try:
            results = yt.search(query, filter='songs', limit=1)
            if not results:
                results = yt.search(query, filter='videos', limit=1)
            if results:
                formatted = _format_track(results[0])
                if formatted:
                    resolved.append(formatted)
        except Exception:
            continue
    return resolved

@app.route('/import_playlist', methods=['POST'])
def import_playlist():
    try:
        data = request.get_json() or {}
        url = data.get('url')
        if not url:
            return jsonify({'error': 'URL is required'}), 400

        playlist_id = None
        if 'youtube.com' in url or 'youtu.be' in url:
            query_params = parse_qs(urlparse(url).query)
            if 'list' in query_params:
                playlist_id = query_params['list'][0]
        
        if playlist_id:
            playlist = yt.get_playlist(playlist_id, limit=200)
            tracks = [
                formatted_track for track in playlist.get('tracks', [])
                if (formatted_track := _format_track(track)) is not None
            ]
            return jsonify({'title': playlist.get('title', 'Imported Playlist'), 'tracks': tracks})

        spotify_playlist_id = _parse_spotify_playlist_id(url)
        if spotify_playlist_id:
            token, err = _get_spotify_token()
            if err:
                return jsonify({'error': err}), 400
            playlist_name, spotify_tracks = _fetch_spotify_playlist_tracks(spotify_playlist_id, token, max_tracks=200)
            yt_tracks = _resolve_spotify_tracks_to_youtube(spotify_tracks)
            if not yt_tracks:
                return jsonify({'error': 'Could not match any Spotify tracks on YouTube.'}), 400
            return jsonify({'title': playlist_name, 'tracks': yt_tracks})

        return jsonify({'error': 'Invalid or unsupported playlist URL'}), 400
    except Exception as e:
        print(f"Import Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/recommend', methods=['POST'])
def recommend():
    try:
        data = request.get_json() or {}
        history = data.get('history', []) # Expecting a list of videoIds
        
        raw_tracks = []
        if history and len(history) > 0:
            # Use a random song from history as a seed for YouTube's ML recommendation engine
            seed_id = random.choice(history)
            watch_list = yt.get_watch_playlist(videoId=seed_id, limit=20)
            raw_tracks = watch_list.get('tracks', [])
        else:
            # Fallback to trending/top hits if no history (Random/Initial state)
            queries = ['Top Global Hits', 'New Music', 'Trending Songs', 'Viral Hits']
            raw_tracks = yt.search(random.choice(queries), filter='songs', limit=20)

        # Process tracks into a consistent format for the frontend
        processed_tracks = [
            formatted_track for track in raw_tracks
            if (formatted_track := _format_track(track)) is not None
        ]
            
        return jsonify(processed_tracks)
    except Exception as e:
        print(f"Recommend Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_artist_thumbnails', methods=['POST'])
def get_artist_thumbnails():
    try:
        artists_req = request.get_json() or []
        artists_with_thumbs = []
        for artist_data in artists_req:
            try:
                artist_id = artist_data.get('id')
                if not artist_id: continue
                artist_details = yt.get_artist(artist_id)
                artists_with_thumbs.append({
                    'name': artist_data.get('name'),
                    'browseId': artist_id,
                    'thumbnail': artist_details['thumbnails'][-1]['url'] if artist_details.get('thumbnails') else ''
                })
            except Exception as e:
                print(f"Could not fetch artist {artist_id}: {e}")
                continue # Skip if artist can't be fetched, log the error
        return jsonify(artists_with_thumbs)
    except Exception as e:
        print(f"Get Artist Thumbnails Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def google_login():
    try:
        token = request.json.get('credential')
        client_id = request.json.get('clientId')
        
        if not token: return jsonify({'error': 'No token provided'}), 400

        # Verify the token
        id_info = id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
        user_id = id_info['sub']
        email = id_info.get('email')

        with sqlite3.connect('aura_users.db') as conn:
            c = conn.cursor()
            c.execute("SELECT data FROM users WHERE google_id = ?", (user_id,))
            row = c.fetchone()
            
            user_data = {}
            if row and row[0]:
                user_data = json.loads(row[0])
            else:
                # New user
                c.execute("INSERT OR IGNORE INTO users (google_id, email, data) VALUES (?, ?, ?)", (user_id, email, '{}'))
            
        return jsonify({
            'status': 'success', 
            'data': user_data,
            'user_info': {
                'id': user_id,
                'name': id_info.get('name'),
                'picture': id_info.get('picture'),
                'email': email
            }
        })
    except ValueError as e:
        print(f"Auth Error (Invalid Token): {e}")
        return jsonify({'error': 'Invalid or expired token'}), 401
    except Exception as e:
        print(f"Auth Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/sync', methods=['POST'])
def sync_user_data():
    try:
        token = request.json.get('credential')
        client_id = request.json.get('clientId')
        data = request.json.get('data')
        
        if not token or not data: return jsonify({'error': 'Missing data'}), 400

        # Verify token again for security on write
        id_info = id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
        user_id = id_info['sub']

        with sqlite3.connect('aura_users.db') as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET data = ? WHERE google_id = ?", (json.dumps(data), user_id))
            
        return jsonify({'status': 'synced'})
    except ValueError as e:
        print(f"Sync Auth Error (Invalid Token): {e}")
        return jsonify({'error': 'Invalid or expired token'}), 401
    except Exception as e:
        print(f"Sync Error: {e}")
        return jsonify({'error': str(e)}), 500

# Socket.IO Events for Listen Along
# NOTE: This in-memory state will be lost on server restart.
# For production, consider using a persistent store like Redis.
party_rooms = {}
sid_to_room = {}

def emit_users(room):
    if room in party_rooms:
        users_list = []
        host_sid = party_rooms[room]['host']
        host_id = party_rooms[room]['users'].get(host_sid, {}).get('id')
        
        for sid, u in party_rooms[room]['users'].items():
            users_list.append({
                'id': u['id'],
                'name': u['name'],
                'avatar': u['avatar'],
                'isHost': (sid == host_sid)
            })
        socketio.emit('party_users', {'users': users_list, 'hostId': host_id}, room=room)

@socketio.on('join_party')
def on_join(data):
    room = str(data['room']).strip().lower()
    print(f"Join Party: {room} | User: {data.get('username')} | PID: {os.getpid()}")
    username = data.get('username', 'Guest')
    user_id = data.get('userId')
    avatar = data.get('avatar')
    
    join_room(room)
    sid_to_room[request.sid] = room
    
    if room not in party_rooms:
        # First user to join creates the party, becomes host, and defines the initial state
        print(f"Creating new party room: {room} on PID {os.getpid()}")
        party_rooms[room] = {
            'host': request.sid, 
            'users': {},
            'state': { 'song': None, 'isPlaying': False, 'time': 0, 'queue': [] }
        }
    
    party_rooms[room]['users'][request.sid] = {
        'id': user_id,
        'name': username,
        'avatar': avatar
    }
    
    # Immediately send the current, authoritative party state to the user who just joined.
    # The client should use this to sync its player and queue.
    emit('party_state_update', party_rooms[room]['state'], room=request.sid)

    emit('party_notification', {'msg': f'{username} joined the party!'}, room=room)
    emit_users(room)

@socketio.on('leave_party')
def on_leave(data):
    room = str(data['room']).strip().lower()
    username = data.get('username', 'Guest')
    leave_room(room)
    sid_to_room.pop(request.sid, None)
    
    if room in party_rooms and request.sid in party_rooms[room]['users']:
        del party_rooms[room]['users'][request.sid]
        if party_rooms[room]['host'] == request.sid:
            if party_rooms[room]['users']:
                party_rooms[room]['host'] = next(iter(party_rooms[room]['users']))
            else:
                del party_rooms[room]
    
    emit('party_notification', {'msg': f'{username} left the party.'}, room=room)
    emit_users(room)

@socketio.on('kick_user')
def on_kick(data):
    room = data.get('room')
    if room: room = str(room).strip().lower()
    target_id = data.get('targetId')
    
    if room in party_rooms and party_rooms[room]['host'] == request.sid:
        target_sid = None
        target_name = "User"
        for sid, user in party_rooms[room]['users'].items():
            if user['id'] == target_id:
                target_sid = sid
                target_name = user['name']
                break
        
        if target_sid:
            try:
                leave_room(room, sid=target_sid)
                sid_to_room.pop(target_sid, None)
            except Exception: pass # Target might have already disconnected
            
            del party_rooms[room]['users'][target_sid]
            emit('kicked', room=target_sid)
            emit('party_notification', {'msg': f'{target_name} was kicked.'}, room=room)
            emit_users(room)

@socketio.on('disconnect')
def on_disconnect():
    try:
        room_id = sid_to_room.pop(request.sid, None)
        if not room_id or room_id not in party_rooms:
            return

        room_data = party_rooms[room_id]
        user = room_data['users'].pop(request.sid, None)
        if not user:
            return

        socketio.emit('party_notification', {'msg': f"{user.get('name', 'A user')} disconnected."}, room=room_id)

        if not room_data['users']:
            del party_rooms[room_id]
        elif room_data['host'] == request.sid:
            room_data['host'] = next(iter(room_data['users']))
            emit_users(room_id)
        else:
            emit_users(room_id)
    except Exception as e:
        print(f"Disconnect Error: {e}")

@socketio.on('party_action')
def on_party_action(data):
    try:
        if not isinstance(data, dict):
            return

        # Robustly determine the room: prefer server-side state, fallback to payload
        room = sid_to_room.get(request.sid)
        if not room and data.get('room'):
            room = str(data.get('room')).strip().lower()

        # Support both legacy client format ({action, payload}) and newer format ({type, ...}).
        action_type = data.get('action') or data.get('type')
        payload = data.get('payload') if isinstance(data.get('payload'), dict) else data

        if not room or not action_type:
            print(f"Ignored Action (Missing Info): {action_type} | Room: {room} | SID: {request.sid}")
            return

        if room not in party_rooms:
            print(f"Ignored Action (Room Not Found): {room}")
            return

        # Auto-repair: If we know the room but socket isn't joined (e.g. server restart + client reconnect), fix it.
        if request.sid not in sid_to_room:
            print(f"Auto-repairing connection for {request.sid} to {room}")
            sid_to_room[request.sid] = room
            join_room(room)

        room_data = party_rooms[room]
        is_host = request.sid == room_data['host']
        state = room_data['state']
        requester = room_data['users'].get(request.sid, {})
        requester_info = {
            'id': requester.get('id'),
            'name': requester.get('name', 'Guest'),
            'avatar': requester.get('avatar')
        }

        print(f"Party Action: {action_type} | Room: {room} | User: {request.sid} | IsHost: {is_host}")

        def emit_update(action, payload_obj):
            socketio.emit('party_update', {'action': action, 'payload': payload_obj}, room=room)

        # Legacy collaborative actions currently used by the frontend.
        if action_type == 'syncQueue':
            new_queue = payload.get('queue')
            if isinstance(new_queue, list):
                state['queue'] = new_queue
                emit_update('syncQueue', {'queue': state['queue']})

        elif action_type == 'addTrack':
            track = payload.get('track')
            if track:
                state['queue'].append(track)
                emit_update('addTrack', {'track': track, 'user': payload.get('user') or requester_info['name']})

        elif action_type == 'playIndex':
            idx = payload.get('index')
            if isinstance(idx, int) and 0 <= idx < len(state['queue']):
                state['song'] = state['queue'][idx]
                state['time'] = 0
                state['isPlaying'] = True
                emit_update('playIndex', {'index': idx})

        elif action_type == 'togglePlay':
            is_playing = bool(payload.get('isPlaying'))
            state['isPlaying'] = is_playing
            emit_update('togglePlay', {'isPlaying': is_playing})

        elif action_type == 'seek':
            time_pos = payload.get('time')
            if isinstance(time_pos, (int, float)):
                state['time'] = time_pos
                emit_update('seek', {'time': time_pos})

        # Host-gated song request flow.
        elif action_type == 'requestPlay':
            track = payload.get('track')
            if not track:
                return

            if is_host:
                state['queue'].append(track)
                new_index = len(state['queue']) - 1
                emit_update('syncQueue', {'queue': state['queue']})
                emit_update('playIndex', {'index': new_index})
                socketio.emit('party_chat', {
                    'username': 'AURA',
                    'userId': 'system',
                    'avatar': None,
                    'message': f"{requester_info['name']} played {track.get('title', 'a song')}"
                }, room=room)
            else:
                host_sid = room_data['host']
                socketio.emit('party_play_request', {
                    'track': track,
                    'requester': requester_info
                }, room=host_sid)
                emit('party_notification', {'msg': 'Play request sent to host.'}, room=request.sid)

        elif action_type == 'approvePlayRequest':
            if not is_host:
                return

            track = payload.get('track')
            requester_data = payload.get('requester') or {}
            requester_name = requester_data.get('name', 'Someone')
            if not track:
                return

            state['queue'].append(track)
            new_index = len(state['queue']) - 1
            emit_update('syncQueue', {'queue': state['queue']})
            emit_update('playIndex', {'index': new_index})
            socketio.emit('party_chat', {
                'username': 'AURA',
                'userId': 'system',
                'avatar': None,
                'message': f"{requester_name} played {track.get('title', 'a song')}"
            }, room=room)

        elif action_type == 'rejectPlayRequest':
            if is_host:
                requester_data = payload.get('requester') or {}
                requester_id = requester_data.get('id')
                if requester_id:
                    for sid, user in room_data['users'].items():
                        if user.get('id') == requester_id:
                            emit('party_notification', {'msg': 'Host rejected your play request.'}, room=sid)
                            break
    except Exception as e:
        print(f"Error in on_party_action: {e}")
        traceback.print_exc()

@socketio.on('get_party_state')
def on_get_state(data=None):
    room = sid_to_room.get(request.sid)
    if not room and data and isinstance(data, dict):
        room = data.get('room')

    if room:
        room = str(room).strip().lower()

    if room and room in party_rooms:
        if request.sid not in sid_to_room:
            sid_to_room[request.sid] = room
            join_room(room)
        emit('party_state_update', party_rooms[room]['state'], room=request.sid)

@socketio.on('party_chat')
def on_party_chat(data):
    room = sid_to_room.get(request.sid)
    if not room and data.get('room'):
        room = str(data.get('room')).strip().lower()
        
    if room:
        socketio.emit('party_chat', data, room=room)

@socketio.on('typing')
def on_typing(data):
    room = sid_to_room.get(request.sid)
    if not room and data.get('room'):
        room = str(data.get('room')).strip().lower()
        
    if room:
        emit('typing', data, room=room, include_self=False)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host='0.0.0.0', port=port)
