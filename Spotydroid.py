import subprocess, sys, importlib, threading

# ─── DEPENDENCY BOOTSTRAP ────────────────────────────────────────────────────
# All packages required by this script.  Format: (pip_name, import_name).
_REQUIRED = [
    ("yt-dlp",      "yt_dlp"),
    ("requests",    "requests"),
    ("mutagen",     "mutagen"),
    ("ytmusicapi",  "ytmusicapi"),
    ("spotipy",     "spotipy"),
    ("pyotp",       "pyotp"),
]

def _pip(*args, **kw):
    subprocess.run(
        [sys.executable, "-m", "pip", *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, **kw
    )

def _ensure_deps():
    """
    Install any missing packages synchronously (blocking), then kick off a
    background yt-dlp upgrade so startup stays fast on subsequent runs.
    """
    missing = []
    for pip_name, mod_name in _REQUIRED:
        try:
            importlib.import_module(mod_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print(f"  Installing missing packages: {', '.join(missing)} …")
        _pip("install", *missing)

    # Always upgrade yt-dlp in the background — it changes frequently and a
    # stale version can silently break downloads.
    def _upgrade_ytdlp():
        _pip("install", "-U", "yt-dlp")

    t = threading.Thread(target=_upgrade_ytdlp, daemon=True, name="ytdlp-upgrade")
    t.start()
    return t   # caller can .join() before first download if desired

_ytdlp_upgrade_thread = _ensure_deps()

import os, csv, zipfile, tempfile, shutil, json, re, time
import requests
from yt_dlp import YoutubeDL
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TPOS, TRCK, TCON, APIC, TXXX, TYER, TXXX, USLT, SYLT
from ytmusicapi import YTMusic
import spotipy
from spotipy.oauth2 import SpotifyOAuth


# ─── ANSI ────────────────────────────────────────────────────────────────────

class C:
    G  = '\033[92m'   # green
    R  = '\033[91m'   # red
    Y  = '\033[93m'   # yellow
    B  = '\033[94m'   # blue
    CY = '\033[96m'   # cyan
    W  = '\033[97m'   # white
    RS = '\033[0m'
    BD = '\033[1m'
    DM = '\033[2m'

def ok(msg):   print(f"  {C.G}{msg}{C.RS}")
def err(msg):  print(f"  {C.R}{msg}{C.RS}")
def warn(msg): print(f"  {C.Y}{msg}{C.RS}")
def info(msg): print(f"  {C.DM}{msg}{C.RS}")
def hdr(msg):  print(f"\n{C.BD}{msg}{C.RS}")
def sep():     print(f"  {'─'*46}")


# ─── PATHS & CREDENTIALS ─────────────────────────────────────────────────────

DATA_DIR            = "/storage/emulated/0/Songs/Data"
OUTPUT_DIR          = "/storage/emulated/0/Songs"
PLAYLIST_DIR        = "/storage/emulated/0/Songs/Playlists"
SPOTIFY_ZIP_PATH    = "/storage/emulated/0/Songs/Data/spotify_playlists.zip"
SPOTIFY_TOKEN_CACHE = "/storage/emulated/0/Songs/Data/.spotify_token_cache"

SPOTIFY_CREDENTIALS_FILE = os.path.join(DATA_DIR, "spotify_credentials.json")
SPOTIFY_REDIRECT_URI     = "http://127.0.0.1:9090"
SPOTIFY_SCOPES           = "playlist-read-private playlist-read-collaborative user-library-read"

# Set at runtime by load_spotify_credentials()
SPOTIFY_CLIENT_ID     = None
SPOTIFY_CLIENT_SECRET = None

SPOTIFY_CSV_FIELDS = [
    "Track URI", "Track Name", "Artist URI(s)", "Artist Name(s)",
    "Album URI", "Album Name", "Album Artist URI(s)", "Album Artist Name(s)",
    "Album Release Date", "Album Image URL", "Disc Number", "Track Number",
    "Track Duration (ms)", "Track Preview URL", "Explicit", "Popularity",
    "ISRC", "Added By", "Added At"
]

_ISRC_RE = re.compile(r'\(([A-Z]{2}[A-Z0-9]{3}\d{7})\)$')


# ─── CREDENTIALS SETUP ───────────────────────────────────────────────────────

def load_spotify_credentials():
    """
    Load Client ID and Secret from the credentials file.
    If missing or incomplete, prompt the user and save for future runs.
    Sets the module-level SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET globals.
    """
    global SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

    os.makedirs(DATA_DIR, exist_ok=True)

    creds = {}
    if os.path.exists(SPOTIFY_CREDENTIALS_FILE):
        try:
            with open(SPOTIFY_CREDENTIALS_FILE, 'r') as f:
                creds = json.load(f)
        except Exception:
            creds = {}

    if creds.get('client_id') and creds.get('client_secret'):
        SPOTIFY_CLIENT_ID     = creds['client_id'].strip()
        SPOTIFY_CLIENT_SECRET = creds['client_secret'].strip()
        return

    # First run or incomplete/missing file — walk the user through setup
    hdr("Spotify API Credentials")
    print("  No credentials found. You need a Spotify API app.")
    print()
    print("  1. Go to https://developer.spotify.com/dashboard")
    print("  2. Log in and click \"Create app\"")
    print("  3. Fill in any name and description")
    print("  4. Set Redirect URI to:  http://127.0.0.1:9090")
    print("  5. Check \"Web API\" and save")
    print("  6. Open app settings and copy the Client ID and Client Secret")
    print()

    try:
        client_id = input("  Client ID     › ").strip()
        if not client_id:
            err("Client ID cannot be empty."); sys.exit(1)
        client_secret = input("  Client Secret › ").strip()
        if not client_secret:
            err("Client Secret cannot be empty."); sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print(); sys.exit(0)

    creds = {'client_id': client_id, 'client_secret': client_secret}
    try:
        with open(SPOTIFY_CREDENTIALS_FILE, 'w') as f:
            json.dump(creds, f, indent=2)
        ok(f"Credentials saved to {SPOTIFY_CREDENTIALS_FILE}")
    except Exception as e:
        err(f"Could not save credentials: {e}")
        warn("They will be used this session only.")

    SPOTIFY_CLIENT_ID     = client_id
    SPOTIFY_CLIENT_SECRET = client_secret
    print()


# ─── STARTUP: RESET OR CONTINUE ──────────────────────────────────────────────

def prompt_reset():
    cache_ok = os.path.exists(SPOTIFY_TOKEN_CACHE)
    zip_ok   = os.path.exists(SPOTIFY_ZIP_PATH)

    hdr("Spotify Music Sync")
    print(f"  token  {C.G+'cached'+C.RS if cache_ok else C.Y+'none'+C.RS}   "
          f"zip  {C.G+'present'+C.RS if zip_ok else C.Y+'none'+C.RS}")
    print(f"\n  [R] reset   [Enter] continue\n")

    try:
        choice = input("  › ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(); sys.exit(0)

    if choice in ('r', 'reset'):
        for path in [SPOTIFY_TOKEN_CACHE, SPOTIFY_ZIP_PATH, SPOTIFY_CREDENTIALS_FILE]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                    ok(f"deleted {path}")
                except Exception as e:
                    err(f"could not delete {path}: {e}")
        print(f"\n  Reset done — log in again on next run.\n")
        sys.exit(0)


# ─── SPOTIFY AUTH HELPER ─────────────────────────────────────────────────────

def make_spotify_auth():
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPES,
        open_browser=False,
        cache_path=SPOTIFY_TOKEN_CACHE
    )

def get_cached_sp_token():
    """Return a valid access token from cache, refreshing if needed. Returns None on failure."""
    try:
        auth = make_spotify_auth()
        tok  = auth.get_cached_token()
        if not tok:
            return None
        if auth.is_token_expired(tok):
            tok = auth.refresh_access_token(tok['refresh_token'])
        return tok['access_token']
    except Exception:
        return None


class _TokenExpired(Exception):
    """Raised when the Spotify token has expired and cannot be refreshed."""


def _sp_get(url, sp_token_box, refresh_fn=None, **kwargs):
    """
    GET a Spotify API URL, auto-refreshing on 401.

    sp_token_box : [str]  – 1-element list; updated in-place on refresh so
                            every caller sharing the same box sees the new token.
    refresh_fn   : callable() → str  (raises _TokenExpired on failure)
                   If None, falls back to get_cached_sp_token().
    Returns the Response on success.
    Raises _TokenExpired if a fresh token cannot be obtained after a 401.
    """
    headers = {"Authorization": f"Bearer {sp_token_box[0]}"}
    r = requests.get(url, headers=headers, **kwargs)

    if r.status_code == 401:
        if refresh_fn:
            new_tok = refresh_fn()       # may raise _TokenExpired
        else:
            new_tok = get_cached_sp_token()
        if not new_tok:
            raise _TokenExpired("Spotify token expired and could not be refreshed")
        sp_token_box[0] = new_tok
        headers = {"Authorization": f"Bearer {new_tok}"}
        r = requests.get(url, headers=headers, **kwargs)

    return r


# ─── SPOTIFY EXPORT → ZIP ────────────────────────────────────────────────────

def fetch_spotify_zip():
    import threading, urllib.parse
    from http.server import HTTPServer, BaseHTTPRequestHandler

    hdr("Spotify Export")

    auth  = make_spotify_auth()
    token = None

    # Try cached / refresh first
    cached = auth.get_cached_token()
    if cached and not auth.is_token_expired(cached):
        token = cached['access_token']
        ok("using cached token")
    elif cached:
        try:
            token = auth.refresh_access_token(cached['refresh_token'])['access_token']
            ok("token refreshed")
        except Exception:
            pass

    # Full browser login
    if not token:
        auth_url = auth.get_authorize_url()
        for cmd in [['termux-open-url', auth_url],
                    ['am', 'start', '-a', 'android.intent.action.VIEW', '-d', auth_url]]:
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except Exception:
                continue

        auth_code = [None]

        class _CB(BaseHTTPRequestHandler):
            def do_GET(self):
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                auth_code[0] = (params.get('code') or [None])[0]
                self.send_response(200 if auth_code[0] else 400)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                msg = b"<h2>Done - you can close this tab.</h2>" if auth_code[0] else b"<h2>Error.</h2>"
                self.wfile.write(msg)
            def log_message(self, *a): pass

        port   = int(SPOTIFY_REDIRECT_URI.rsplit(':', 1)[-1].split('/')[0])
        server = HTTPServer(('127.0.0.1', port), _CB)
        server.timeout = 120
        print("Logging-in")
        server.handle_request()
        server.server_close()

        if not auth_code[0]:
            err("no auth code — login timed out"); return False

        try:
            token = auth.get_access_token(auth_code[0])['access_token']
            ok("Login successful")
        except Exception as e:
            err(f"token exchange failed: {e}"); return False

    # Connect
    sp   = spotipy.Spotify(auth=token)
    user = sp.current_user()
    uid  = user['id']
    ok(f"Connected as {user.get('display_name', uid)}")

    _token = [token]

    def auth_headers():
        return {"Authorization": f"Bearer {_token[0]}"}

    def track_to_row(item):
        track = item.get('track') or item.get('item')
        if not track or not isinstance(track, dict) or track.get('type') != 'track':
            return None
        artists      = track.get('artists') or []
        album        = track.get('album') or {}
        album_artists = album.get('artists') or []
        isrc         = (track.get('external_ids') or {}).get('isrc', '')
        return {
            "Track URI":            track.get('uri', ''),
            "Track Name":           track.get('name', ''),
            "Artist URI(s)":        ','.join(a.get('uri', '') for a in artists),
            "Artist Name(s)":       ','.join(a.get('name', '') for a in artists),
            "Album URI":            album.get('uri', ''),
            "Album Name":           album.get('name', ''),
            "Album Artist URI(s)":  ','.join(a.get('uri', '') for a in album_artists),
            "Album Artist Name(s)": ','.join(a.get('name', '') for a in album_artists),
            "Album Release Date":   album.get('release_date', ''),
            "Album Image URL":      (album.get('images') or [{}])[0].get('url', ''),
            "Disc Number":          str(track.get('disc_number', '')),
            "Track Number":         str(track.get('track_number', '')),
            "Track Duration (ms)":  str(track.get('duration_ms', '')),
            "Track Preview URL":    track.get('preview_url', ''),
            "Explicit":             str(track.get('explicit', False)).lower(),
            "Popularity":           str(track.get('popularity', '')),
            "ISRC":                 isrc,
            "Added By":             '',
            "Added At":             item.get('added_at', ''),
        }

    def fetch_all_pages(sp_call, *args, **kwargs):
        results = sp_call(*args, **kwargs)
        items   = list(results.get('items', []))
        while results.get('next'):
            results = sp.next(results)
            items.extend(results.get('items', []))
        return items

    def fetch_playlist_items_rest(pl_id):
        """Fetch all playlist items via REST (guarantees ISRC via external_ids)."""
        items  = []
        url    = f"https://api.spotify.com/v1/playlists/{pl_id}/items"
        params = {"limit": 100, "offset": 0, "market": "from_token"}

        while url:
            resp = requests.get(url, headers=auth_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                new_tok = get_cached_sp_token()
                if new_tok:
                    _token[0] = new_tok
                    resp = requests.get(url, headers=auth_headers(), params=params, timeout=30)
            if resp.status_code == 403:
                warn("403 — skipping (access denied)"); break
            if resp.status_code != 200:
                warn(f"REST {resp.status_code}"); break
            data   = resp.json()
            items.extend(data.get('items') or [])
            url    = data.get('next')
            params = {}
        return items

    def write_csv(tmpdir, name, items):
        safe = re.sub(r'[\\/*?:"<>|]', '_', name)
        path = os.path.join(tmpdir, f"{safe}.csv")
        rows = [r for r in (track_to_row(i) for i in items) if r]
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=SPOTIFY_CSV_FIELDS,
                               quoting=csv.QUOTE_ALL, extrasaction='ignore')
            w.writeheader(); w.writerows(rows)
        return path, len(rows)

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_paths = []

        # Liked songs
        print(f"\n  Fetching liked songs…")
        try:
            raw = fetch_all_pages(sp.current_user_saved_tracks, limit=50)
            wrapped = [{'track': i['track'], 'added_at': i['added_at']} for i in raw if i.get('track')]
            path, count = write_csv(tmpdir, "liked", wrapped)
            csv_paths.append(path)
            ok(f"liked.csv  {count} Tracks")
        except Exception as e:
            err(f"liked songs failed: {e}")

        # Playlists
        print(f"\n  Fetching playlists…")
        playlists = fetch_all_pages(sp.current_user_playlists, limit=50)
        info(f"{len(playlists)} playlists found")

        for i, pl in enumerate(playlists, 1):
            name      = pl.get('name', 'unknown')
            pl_id     = pl.get('id')
            pl_owner  = (pl.get('owner') or {}).get('id', '')
            total     = (pl.get('tracks') or {}).get('total', '?')

            print(f"  [{i}/{len(playlists)}] {name} ({total})", end='  ', flush=True)

            if pl_owner != uid:
                print(f"{C.Y}skipped (not yours){C.RS}"); continue

            try:
                raw_items = fetch_playlist_items_rest(pl_id)
                items = [
                    it for it in raw_items
                    if it and isinstance((it.get('track') or it.get('item')), dict)
                    and (it.get('track') or it.get('item')).get('type') == 'track'
                    and (it.get('track') or it.get('item')).get('id')
                ]
                path, count = write_csv(tmpdir, name, items)
                csv_paths.append(path)
                print(f"{count} Tracks")
                time.sleep(0.1)
            except Exception as e:
                msg = str(e)
                if '403' in msg:
                    print(f"{C.Y}skipped (403){C.RS}")
                else:
                    print(f"{C.R}failed: {e}{C.RS}")

        # Zip
        os.makedirs(os.path.dirname(SPOTIFY_ZIP_PATH), exist_ok=True)
        with zipfile.ZipFile(SPOTIFY_ZIP_PATH, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in csv_paths:
                zf.write(p, os.path.basename(p))

        kb = os.path.getsize(SPOTIFY_ZIP_PATH) / 1024
        ok(f"{len(csv_paths)} CSVs → zip  ({kb:.0f} KB)")
        return True


# ─── TRACK INFO: RELEASE DATE + REAL PLAY COUNT ──────────────────────────────
#
# Spotify does not expose stream counts in the public Web API.
# The Partner API (api-partner.spotify.com/pathfinder) returns real play counts,
# but the anonymous web-player token endpoint requires a rotating TOTP secret.
#
# Spotify rotates the TOTP cipher every few days, so it cannot be hardcoded.
# Solution: fetch the live secretDict.json from xyloflake/spot-secrets-go,
# which is auto-updated hourly by scraping Spotify's web player JS bundles.
# Pick the highest version number, derive the TOTP secret, get the anon token,
# then query the Partner API queryAlbumTracks for the real per-track playcount.

import base64

_SECRETS_URL = ("https://raw.githubusercontent.com/xyloflake/spot-secrets-go"
                "/refs/heads/main/secrets/secretDict.json")

_PARTNER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")


def _cipher_to_totp_secret(cipher: list) -> str:
    """Convert a Spotify cipher array to a base32 TOTP secret string."""
    raw = "".join(str(c ^ (i % 33 + 9)) for i, c in enumerate(cipher))
    return base64.b32encode(raw.encode("ascii")).decode("ascii").strip("=")


def _fetch_live_secrets() -> tuple[str, int] | tuple[None, None]:
    """
    Fetch the latest TOTP secret from the community-maintained secrets repo.
    Returns (totp_secret_b32, version) or (None, None) on failure.
    """
    try:
        r = requests.get(_SECRETS_URL, timeout=10)
        if r.status_code != 200:
            err(f"    play count: secrets fetch HTTP {r.status_code} — {r.text[:80]}")
            return None, None
        data = r.json()
        if not data:
            err("    play count: secrets JSON is empty")
            return None, None
        # Pick highest version number
        latest_ver = max(int(k) for k in data.keys())
        cipher = data[str(latest_ver)]
        secret = _cipher_to_totp_secret(cipher)
        return secret, latest_ver
    except Exception as e:
        err(f"    play count: secrets fetch failed — {e}")
        return None, None


def _get_anon_token() -> str | None:
    """
    Obtain a Spotify web-player anonymous token using the live TOTP workaround.
    Returns the access token string, or None on failure.
    """
    try:
        import pyotp
        from datetime import datetime
    except ImportError:
        err("    play count: pyotp not installed — run: pip install pyotp")
        return None

    # Step A: fetch live TOTP secret (rotates every few days)
    secret, version = _fetch_live_secrets()
    if not secret:
        return None  # error already printed

    # Step B: generate TOTP from local time
    try:
        totp_code = pyotp.TOTP(secret).at(datetime.now())
    except Exception as e:
        err(f"    play count: TOTP generation failed (ver {version}) — {e}")
        return None

    # Step C: request anon token
    try:
        tok_resp = requests.get(
            "https://open.spotify.com/api/token",
            headers={"User-Agent": _PARTNER_UA, "Referer": "https://open.spotify.com/"},
            params={"reason": "transport", "productType": "web-player",
                    "totp": totp_code, "totpVer": version},
            timeout=10
        )
        if tok_resp.status_code != 200:
            err(f"    play count: anon token HTTP {tok_resp.status_code} "
                f"(totp ver {version}) — {tok_resp.text[:120]}")
            return None
        token = tok_resp.json().get("accessToken")
        if not token:
            err(f"    play count: anon token has no 'accessToken' — {tok_resp.text[:120]}")
            return None
        return token
    except Exception as e:
        err(f"    play count: anon token request failed — {e}")
        return None


def fetch_track_info(track, sp_token, yt_video_id=None, token_refresh_fn=None):
    """
    Return {'view_count': int|None, 'view_source': str}.

    Steps:
      1. Official API: ISRC search → resolve track ID, album ID.
      2. TOTP workaround: get anon web-player token.
      3. Partner API queryAlbumTracks → find this track's real play count.
      4. If Spotify gives nothing and yt_video_id provided, fall back to YouTube.
    """
    result = {'view_count': None, 'view_source': 'Spotify'}

    isrc = (track.get('isrc') or '').strip().upper()
    if not isrc:
        err("    play count: track has no ISRC — skipping")
        if yt_video_id:
            result = _fetch_yt_view_count(result, yt_video_id)
        return result

    if not sp_token:
        err("    play count: no Spotify token — skipping")
        if yt_video_id:
            result = _fetch_yt_view_count(result, yt_video_id)
        return result

    # Mutable box — shared with token_refresh_fn so _sp_get's in-place update
    # is visible in the caller's sp_token_box too.
    sp_token_box = [sp_token]

    # Step 1 — resolve ISRC → track ID + album ID + precise release date
    track_id = album_id = album_uri = None
    try:
        r = _sp_get(
            "https://api.spotify.com/v1/search",
            sp_token_box,
            refresh_fn=token_refresh_fn,
            params={"q": f"isrc:{isrc}", "type": "track", "limit": 1},
            timeout=10
        )
        if r.status_code != 200:
            err(f"    play count: ISRC search HTTP {r.status_code} — {r.text[:120]}")
            return result
        items = r.json().get('tracks', {}).get('items', [])
        if not items:
            err(f"    play count: no Spotify track found for ISRC {isrc}")
            return result
        sp_track = items[0]
        album    = sp_track.get('album') or {}
        track_id  = sp_track.get('id')
        album_id  = album.get('id')
        album_uri = album.get('uri')
        if not track_id:
            err(f"    play count: ISRC search returned track with no ID")
            return result
        if not album_id:
            err(f"    play count: track has no album ID in search response")
            return result
    except _TokenExpired:
        raise   # propagate so the caller can retry / cancel
    except Exception as e:
        err(f"    play count: ISRC search exception — {e}")
        return result

    # Step 2 — get anon web-player token via TOTP
    anon_token = _get_anon_token()
    if not anon_token:
        return result  # error already printed inside _get_anon_token

    # Step 3 — Partner API: query album tracks for real play count
    try:
        pr = requests.get(
            "https://api-partner.spotify.com/pathfinder/v1/query",
            headers={"Authorization": f"Bearer {anon_token}",
                     "User-Agent": _PARTNER_UA},
            params={
                "operationName": "queryAlbumTracks",
                "variables": json.dumps({
                    "uri": album_uri,
                    "offset": 0,
                    "limit": 300
                }),
                "extensions": json.dumps({
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "3ea563e1d68f486d8df30f69de9dcedae74c77e684b889ba7408c589d30f7f2e"
                    }
                })
            },
            timeout=15
        )
        if pr.status_code != 200:
            err(f"    play count: Partner API HTTP {pr.status_code} — {pr.text[:120]}")
            return result

        pdata = pr.json()
        album_data = pdata.get('data', {}).get('album')
        if not album_data:
            err(f"    play count: Partner API response missing 'data.album' — keys: {list(pdata.get('data', {}).keys())}")
            return result

        track_items = album_data.get('tracks', {}).get('items', [])
        if not track_items:
            err(f"    play count: Partner API returned 0 tracks for album {album_id}")
            return result

        # Match by track ID embedded in the track URI
        for item in track_items:
            t = item.get('track') or {}
            uri = t.get('uri', '')  # e.g. "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
            if uri.endswith(track_id):
                pc = t.get('playcount')
                if pc is None:
                    err(f"    play count: matched track has null playcount (may be restricted)")
                else:
                    try:
                        result['view_count'] = int(pc)
                    except (ValueError, TypeError) as e:
                        err(f"    play count: could not parse playcount '{pc}' — {e}")
                return result

        err(f"    play count: track ID {track_id} not found among {len(track_items)} album tracks returned")
    except Exception as e:
        err(f"    play count: Partner API exception — {e}")

    # Step 4 — YouTube fallback if Spotify gave nothing
    if result['view_count'] is None and yt_video_id:
        result = _fetch_yt_view_count(result, yt_video_id)

    return result


def _fetch_yt_view_count(result: dict, video_id: str) -> dict:
    """Fetch view count from YouTube using yt-dlp. Mutates and returns result."""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        with YoutubeDL({'quiet': True, 'no_warnings': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
        vc = info.get('view_count')
        if vc is None:
            err(f"    play count: YouTube returned no view_count for {video_id}")
        else:
            result['view_count']  = int(vc)
            result['view_source'] = 'YouTube'
    except Exception as e:
        err(f"    play count: YouTube fallback failed for {video_id} — {e}")
    return result


# ─── LYRICS ──────────────────────────────────────────────────────────────────

def fetch_lyrics_lrclib(artist, title, album=None, duration=None):
    try:
        params = {'artist_name': artist, 'track_name': title}
        if album:    params['album_name'] = album
        if duration: params['duration']   = duration
        r = requests.get("https://lrclib.net/api/get", params=params, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {'synced': d.get('syncedLyrics'), 'plain': d.get('plainLyrics'), 'source': 'LRCLIB'}
    except Exception:
        pass
    return None


def fetch_lyrics_musixmatch(artist, title):
    try:
        r = requests.get(
            "https://apic-desktop.musixmatch.com/ws/1.1/macro.subtitles.get",
            params={'format': 'json', 'q_artist': artist, 'q_track': title,
                    'user_language': 'en', 'namespace': 'lyrics_synched',
                    'part': 'lyrics_crowd,user,lyrics_verified_by', 'tags': 'nowplaying'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=10
        )
        if r.status_code == 200:
            d  = r.json()
            if d.get('message', {}).get('header', {}).get('status_code') == 200:
                mc     = d['message']['body'].get('macro_calls', {})
                synced = plain = None
                if 'track.subtitles.get' in mc:
                    subs = mc['track.subtitles.get']['message']['body'].get('subtitle_list', [])
                    if subs:
                        synced = _musixmatch_to_lrc(subs[0]['subtitle'].get('subtitle_body', ''))
                if 'track.lyrics.get' in mc:
                    plain = mc['track.lyrics.get']['message']['body'].get('lyrics', {}).get('lyrics_body')
                if synced or plain:
                    return {'synced': synced, 'plain': plain, 'source': 'Musixmatch'}
    except Exception:
        pass
    return None


def _musixmatch_to_lrc(raw):
    lines = []
    for line in raw.strip().split('\n'):
        if not line.strip(): continue
        try:
            data = json.loads(line)
            if isinstance(data, list):
                for item in data:
                    t   = item.get('time', {}).get('total', 0)
                    m   = int(t // 60)
                    s   = t % 60
                    txt = item.get('text', '')
                    lines.append(f"[{m:02d}:{s:05.2f}]{txt}")
        except Exception:
            continue
    return '\n'.join(lines) if lines else None


def fetch_lyrics(artist, title, album=None, duration_ms=None):
    dur = duration_ms / 1000 if duration_ms else None
    for fn in [lambda: fetch_lyrics_lrclib(artist, title, album, dur),
               lambda: fetch_lyrics_musixmatch(artist, title)]:
        lyr = fn()
        if lyr and (lyr.get('synced') or lyr.get('plain')):
            return lyr
    return None


def prepend_info_to_lyrics(lyrics_data, track_info):
    """Prepend release date (from CSV) + stream count (with source tag) as the first line of lyrics."""
    rd     = track_info.get('release_date') or 'N/A'
    vc    = track_info.get('view_count')
    vsrc  = track_info.get('view_source', 'Spotify')
    vc_str = f"{vc:,} [{vsrc}]" if vc is not None else 'N/A'
    line  = f"{rd}  |  {vc_str}"

    new = dict(lyrics_data)
    if new.get('plain'):
        new['plain'] = f"{line}\n\n{new['plain']}"
    if new.get('synced'):
        new['synced'] = f"[00:00.00]{line}\n\n" + new['synced']
    return new


def embed_lyrics(filepath, lyrics_data):
    try:
        audio = MP3(filepath)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall('USLT')
        audio.tags.delall('SYLT')

        if lyrics_data.get('plain'):
            audio.tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics_data['plain']))

        if lyrics_data.get('synced'):
            sylt = []
            for line in lyrics_data['synced'].strip().split('\n'):
                m = re.match(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)', line)
                if m:
                    ts  = int((int(m.group(1)) * 60 + float(m.group(2))) * 1000)
                    txt = m.group(3).strip()
                    if txt:
                        sylt.append((txt, ts))
            if sylt:
                audio.tags.add(SYLT(encoding=3, lang='eng', format=2, type=1, desc='', text=sylt))

        audio.save(v1=2)
        return True
    except Exception:
        return False


# ─── CSV PARSING ─────────────────────────────────────────────────────────────

def parse_csv_row(row, row_num, failed):
    if len(row) < 6:
        failed.append({'row': row_num, 'reason': f'only {len(row)} columns'}); return None

    def col(i): return row[i].strip() if len(row) > i and row[i] else None

    title  = col(1)
    artist = col(3)
    album  = col(5)
    isrc   = (col(16) or '').upper()

    for field, val in [('title', title), ('artist', artist), ('album', album), ('ISRC', isrc or None)]:
        if not val:
            failed.append({'row': row_num, 'reason': f'missing {field}'}); return None

    release_date = row[8][:4] if len(row) > 8 and row[8] else None
    cover_url    = col(9)
    duration_ms  = int(row[12]) if len(row) > 12 and (row[12] or '').strip().isdigit() else 0

    return {'artist': artist, 'title': title, 'album': album,
            'release_date': release_date, 'cover_url': cover_url,
            'duration_ms': duration_ms, 'isrc': isrc}


def parse_csv_tracks(path):
    if not os.path.exists(path):
        err(f"CSV not found: {path}"); return []

    tracks, failed, seen_keys = [], [], {}

    with open(path, 'r', encoding='utf-8') as f:
        reader   = csv.reader(f)
        first    = next(reader, None)
        if not first:
            err("CSV is empty"); return []

        # If first row looks like data (not header), process it
        if not first[0].startswith('spotify:track:'):
            row_num = 1
        else:
            t = parse_csv_row(first, 1, failed)
            if t:
                k = track_key(t)
                seen_keys.setdefault(k, []).append(1)
                tracks.append(t)
            row_num = 2

        for n, row in enumerate(reader, row_num):
            t = parse_csv_row(row, n, failed)
            if t:
                k = track_key(t)
                seen_keys.setdefault(k, []).append(n)
                tracks.append(t)

    if failed:
        warn(f"{len(failed)} rows skipped")
        for f in failed[:5]:
            info(f"  row {f['row']}: {f['reason']}")

    dups = {k: v for k, v in seen_keys.items() if len(v) > 1}
    if dups:
        warn(f"{len(dups)} duplicate ISRCs in CSV")

    ok(f"{len(tracks)} tracks parsed ({len(seen_keys)} unique)")
    return tracks


def track_key(track):
    isrc = (track.get('isrc') or '').strip().upper()
    if isrc:
        return f"isrc:{isrc}"
    raise ValueError(f"Track has no ISRC: {track.get('artist')} - {track.get('title')}")


# ─── LOCAL FILE INDEX ─────────────────────────────────────────────────────────

def get_local_files():
    """Index OUTPUT_DIR MP3s by ISRC key. Files without ISRC in name are skipped."""
    local = {}
    if not os.path.exists(OUTPUT_DIR):
        return local
    for fname in os.listdir(OUTPUT_DIR):
        if not fname.lower().endswith('.mp3'):
            continue
        stem = fname[:-4]
        m = _ISRC_RE.search(stem)
        if not m:
            continue
        isrc        = m.group(1)
        display     = stem[:m.start()].strip()
        artist, title = ('', display)
        if ' - ' in display:
            artist, title = display.split(' - ', 1)
        local[f"isrc:{isrc}"] = {
            'filepath': os.path.join(OUTPUT_DIR, fname),
            'filename': fname, 'artist': artist.strip(), 'title': title.strip(), 'isrc': isrc
        }
    return local


# ─── YOUTUBE MUSIC SEARCH ────────────────────────────────────────────────────

def _normalize(text):
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', text.lower().strip()))

def _is_video(title):
    t = title.lower()
    return 'music video' in t or 'official video' in t

def search_ytmusic(ytmusic, track):
    query   = f"{track['title'].split(' - ')[0]} {track['artist']}"
    sp_t    = _normalize(track['title'].split(' - ')[0])
    sp_a    = _normalize(track['artist'])

    try:
        results = ytmusic.search(query, filter='songs', limit=20) or []
    except Exception:
        return None

    def score(r):
        vid = r.get('videoId')
        if not vid: return 0
        t   = r.get('title', '')
        if _is_video(t): return 0
        nt  = _normalize(t)
        a   = (r.get('artists') or [{}])[0].get('name', '')
        na  = _normalize(a)
        title_match  = nt == sp_t or sp_t in nt or nt in sp_t
        artist_match = na == sp_a
        return (2 if title_match and artist_match else 1 if title_match else 0)

    best = max(results, key=score, default=None)
    if not best or score(best) == 0:
        return None

    ytm_artist = (best.get('artists') or [{}])[0].get('name', '')
    ytm_album  = (best.get('album') or {}).get('name', '')
    return {
        'video_id': best['videoId'],
        'title':    best.get('title', ''),
        'artist':   ytm_artist,
        'album':    ytm_album,
        'duration': best.get('duration_seconds', 0),
        'source':   'YouTube Music',
    }


def search_youtube_fallback(track):
    query = f"{track['title'].split(' - ')[0]} {track['artist']}"
    try:
        with YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        if not info or 'entries' not in info:
            return None
        best = max((e for e in info['entries'] if e), key=lambda e: e.get('view_count', 0), default=None)
        if best:
            return {'video_id': best.get('id', ''), 'title': best.get('title', ''),
                    'artist': best.get('uploader', ''), 'duration': best.get('duration', 0),
                    'views': best.get('view_count', 0), 'source': 'YouTube (fallback)'}
    except Exception:
        pass
    return None


# ─── DOWNLOAD ────────────────────────────────────────────────────────────────

def build_filename(track):
    base = f"{track['artist']} - {track['title']} ({(track.get('isrc') or '').upper()})"
    return re.sub(r'[<>:"/\\|?*]', '_', base)


def download_track(match, track):
    fname = build_filename(track)
    url   = (f"https://music.youtube.com/watch?v={match['video_id']}"
             if match['source'].startswith('YouTube Music')
             else f"https://youtube.com/watch?v={match['video_id']}")

    base_opts = {
        'format':      'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl':     os.path.join(OUTPUT_DIR, f"{fname}.%(ext)s"),
        'quiet':       True, 'no_warnings': True, 'noprogress': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3', 'preferredquality': '320'}],
        'postprocessor_args': {'ffmpeg': ['-loglevel', 'quiet']},
        'prefer_ffmpeg': True, 'retries': 3, 'fragment_retries': 3,
    }

    def _try_download(opts):
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        mp3 = os.path.join(OUTPUT_DIR, f"{fname}.mp3")
        if os.path.exists(mp3):
            return mp3
        for ext in ['m4a', 'webm', 'opus']:
            src = os.path.join(OUTPUT_DIR, f"{fname}.{ext}")
            if os.path.exists(src):
                subprocess.run(['ffmpeg', '-y', '-i', src, '-acodec', 'libmp3lame', '-ab', '320k', mp3],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.remove(src)
                if os.path.exists(mp3): return mp3
        return None

    try:
        return _try_download(base_opts)
    except Exception as e:
        msg = str(e).lower()
        if 'sign in' in msg or 'age' in msg and 'restrict' in msg:
            bypass = {**base_opts, 'age_limit': None,
                      'extractor_args': {'youtube': {'skip': ['dash', 'hls'],
                                                     'player_skip': ['configs']}},
                      'http_headers': {'User-Agent': 'Mozilla/5.0'}}
            try:
                return _try_download(bypass)
            except Exception:
                pass
    return None


# ─── METADATA ────────────────────────────────────────────────────────────────

def add_metadata(filepath, track):
    if not os.path.exists(filepath):
        return False
    try:
        audio = MP3(filepath)
        if audio.tags is None:
            audio.add_tags()
        else:
            audio.tags.clear()

        audio.tags.add(TIT2(encoding=3, text=track['title']))
        audio.tags.add(TPE1(encoding=3, text=track['artist']))
        audio.tags.add(TPE2(encoding=3, text=track['artist']))
        audio.tags.add(TALB(encoding=3, text=track['album']))
        audio.tags.add(TRCK(encoding=3, text='1'))
        audio.tags.add(TPOS(encoding=3, text='1'))
        audio.tags.add(TCON(encoding=3, text='Music'))

        for desc in ['ALBUMARTIST', 'AlbumArtist', 'ARTIST']:
            try: audio.tags.add(TXXX(encoding=3, desc=desc, text=track['artist']))
            except Exception: pass

        if track.get('release_date'):
            year = str(track['release_date'])
            try:    audio.tags.add(TDRC(encoding=3, text=year))
            except: audio.tags.add(TYER(encoding=3, text=year))

        if track.get('cover_url'):
            try:
                resp = requests.get(track['cover_url'], timeout=30)
                resp.raise_for_status()
                mime = 'image/png' if 'png' in resp.headers.get('content-type', '') else 'image/jpeg'
                audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc='Front Cover', data=resp.content))
            except Exception:
                pass

        audio.save(v1=2)
        return True
    except Exception:
        return False


# ─── MAIN SYNC ───────────────────────────────────────────────────────────────

def check_tool(name):
    try:
        subprocess.run([name, '-version'], capture_output=True, check=False, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def sync_playlist():
    hdr("Music Sync")

    if not check_tool('ffmpeg'):
        err("FFmpeg not found — install with: pkg install ffmpeg"); return
    if not os.path.exists(SPOTIFY_ZIP_PATH):
        err(f"Zip not found: {SPOTIFY_ZIP_PATH}"); return

    ytmusic = YTMusic()
    sp_token = get_cached_sp_token()

    # Extract liked.csv
    tmp_dir = tempfile.mkdtemp()
    with zipfile.ZipFile(SPOTIFY_ZIP_PATH, 'r') as zf:
        matches = [n for n in zf.namelist() if os.path.basename(n).lower() == 'liked.csv']
        if not matches:
            err("liked.csv not found in zip"); shutil.rmtree(tmp_dir, ignore_errors=True); return
        zf.extract(matches[0], tmp_dir)
        liked_csv = os.path.join(tmp_dir, matches[0])

    csv_tracks = parse_csv_tracks(liked_csv)
    if not csv_tracks:
        err("No tracks in CSV"); shutil.rmtree(tmp_dir, ignore_errors=True); return

    local_files = get_local_files()
    csv_keys    = {track_key(t) for t in csv_tracks}
    local_keys  = set(local_files.keys())
    missing     = csv_keys - local_keys
    extra       = local_keys - csv_keys
    present     = csv_keys & local_keys

    print(f"\n  {C.G}{len(present)} synced{C.RS}  {C.Y}{len(missing)} missing{C.RS}  {C.R}{len(extra)} extra{C.RS}")

    # Remove extra files
    if extra:
        print(f"\n  {len(extra)} files not in liked songs:")
        for k in extra:
            info(f"  {local_files[k]['filename']}")
        ans = input(f"\n  Delete {len(extra)} files? (y/N): ").strip().lower()
        if ans in ('y', 'yes'):
            deleted = 0
            for k in extra:
                try:
                    os.remove(local_files[k]['filepath'])
                    deleted += 1
                except Exception:
                    pass
            ok(f"deleted {deleted}/{len(extra)}")
        else:
            info("deletion skipped")
        local_files = get_local_files()

    # Download missing
    if not missing:
        ok("nothing to download")
    else:
        updated_local = get_local_files()
        still_missing = csv_keys - set(updated_local.keys())
        to_download   = [t for t in csv_tracks if track_key(t) in still_missing]

        print(f"\n  Downloading {len(to_download)} tracks…\n")
        ok_count, failed = 0, []

        # One shared token box for the whole session.  fetch_track_info and
        # _sp_get write back into it, so every track after a refresh uses
        # the new token automatically.
        sp_token_box = [sp_token]

        def _refresh_token():
            """Refresh Spotify token and update the shared box. Raises _TokenExpired on failure."""
            new_tok = get_cached_sp_token()
            if new_tok:
                ok("Spotify token refreshed")
                sp_token_box[0] = new_tok
                return new_tok
            raise _TokenExpired("Spotify token expired and could not be refreshed — cancelling session")

        # Make sure yt-dlp upgrade has finished before we start downloading.
        _ytdlp_upgrade_thread.join()

        session_cancelled = False

        for i, track in enumerate(to_download, 1):
            if session_cancelled:
                break

            label = f"{track['artist']} - {track['title']}"
            print(f"  [{i}/{len(to_download)}] {label}")

            # 1 — find source
            match = search_ytmusic(ytmusic, track) or search_youtube_fallback(track)

            if not match:
                err("no source found"); failed.append((label, "no source")); continue

            is_ytm = match['source'].startswith('YouTube Music')
            col    = C.G if is_ytm else C.Y
            print(f"    {col}{match['source']}{C.RS}")
            if match.get('views'):
                info(f"    {match['views']:,} views")

            # 2 — download
            mp3 = download_track(match, track)
            if not mp3:
                err("Download failed"); failed.append((label, "Download failed")); continue
            ok("Downloaded")

            # 3 — metadata
            if not add_metadata(mp3, track):
                warn("metadata failed")

            # 4 — lyrics + info
            lyr = fetch_lyrics(track['artist'], track['title'], track.get('album'), track.get('duration_ms'))
            if lyr and (lyr.get('synced') or lyr.get('plain')):
                kind = 'synced' if lyr.get('synced') else 'plain'
                src  = lyr['source']

                # Attempt fetch_track_info; on token expiry retry once after
                # refreshing so the current song is not left without play count.
                t_info = None
                for _attempt in range(2):
                    try:
                        t_info = fetch_track_info(
                            track,
                            sp_token_box[0],
                            yt_video_id=match.get('video_id'),
                            token_refresh_fn=_refresh_token,
                        )
                        break   # success
                    except _TokenExpired:
                        if _attempt == 0:
                            # Token expired mid-fetch; try one more time with a
                            # freshly obtained token before giving up on the song.
                            try:
                                _refresh_token()
                                warn("    token expired — retrying play count…")
                                continue
                            except _TokenExpired:
                                pass
                        err("    Spotify token expired — play count unavailable for this track")
                        warn("    Cancelling remaining play-count lookups — re-run to resume.")
                        session_cancelled = True
                        break

                if t_info is None:
                    t_info = {'view_count': None, 'view_source': 'Spotify'}

                t_info['release_date'] = track.get('release_date') or 'N/A'
                rd      = t_info['release_date']
                vc      = t_info.get('view_count')
                vsrc    = t_info.get('view_source', 'Spotify')
                vc_str  = f"{vc:,} [{vsrc}]" if vc is not None else 'N/A'
                print(f"    {C.CY}{src}{C.RS} {kind}  ·  {rd}  ·  {vc_str}")

                lyr = prepend_info_to_lyrics(lyr, t_info)
                if not embed_lyrics(mp3, lyr):
                    warn("lyrics embed failed")
            else:
                info("no lyrics found")

            ok_count += 1
            time.sleep(1)

        sep()
        print(f"  {C.G}{ok_count}{C.RS}/{len(to_download)} downloaded", end='')
        if failed:
            print(f"  {C.R}{len(failed)} failed{C.RS}")
            for label, reason in failed:
                info(f"  {label}  ({reason})")
        else:
            print()
        if session_cancelled:
            skipped = len(to_download) - ok_count - len(failed)
            if skipped > 0:
                warn(f"  {skipped} tracks skipped — Spotify token expired mid-session")

    print(f"\n  {C.G} Sync done — {C.RS} {len(get_local_files())} tracks total")
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── PLAYLIST → M3U ──────────────────────────────────────────────────────────

def parse_playlist_csv(path):
    if not os.path.exists(path):
        return None
    tracks = []
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or 'ISRC' not in reader.fieldnames:
            return None
        for row in reader:
            isrc = (row.get('ISRC') or '').strip().upper()
            if not isrc: continue
            dur  = int(row['Track Duration (ms)']) if (row.get('Track Duration (ms)') or '').strip().isdigit() else 0
            tracks.append({
                'isrc':       isrc,
                'raw_artist': (row.get('Artist Name(s)') or '').strip(),
                'raw_title':  (row.get('Track Name') or '').strip(),
                'duration_sec': dur / 1000 if dur else None,
                'date_added': (row.get('Added At') or '').strip(),
            })
    tracks.sort(key=lambda t: t['date_added'] or '', reverse=True)
    return tracks


def generate_m3u(matched, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        for item in matched:
            dur   = int(item.get('duration_sec') or 0)
            label = f"{item['artist']} - {item['title']}"
            f.write(f"#EXTINF:{dur},{label}\n{item['filepath']}\n")


def run_playlist_generator():
    hdr("M3U Playlist Generator")

    if not os.path.exists(SPOTIFY_ZIP_PATH):
        err(f"zip not found: {SPOTIFY_ZIP_PATH}"); return

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(SPOTIFY_ZIP_PATH, 'r') as zf:
            zf.extractall(tmpdir)

        csv_files = sorted(
            p for root, _, files in os.walk(tmpdir)
            for f in files if f.lower().endswith('.csv') and f.lower() != 'liked.csv'
            for p in [os.path.join(root, f)]
        )

        if not csv_files:
            err("no playlist CSVs in zip"); return

        local = get_local_files()
        info(f"{len(csv_files)} playlists  ·  {len(local)} local tracks")
        print()

        created = failed = 0
        for csv_path in csv_files:
            name_raw = os.path.splitext(os.path.basename(csv_path))[0]
            name     = ' '.join(w.capitalize() for w in name_raw.replace('_', ' ').split())
            out_m3u  = os.path.join(PLAYLIST_DIR, f"{name}.m3u")

            pl_tracks = parse_playlist_csv(csv_path)
            if not pl_tracks:
                err(f"{name}  (parse error)"); failed += 1; continue

            matched, missing = [], []
            for t in pl_tracks:
                hit = local.get(f"isrc:{t['isrc']}")
                if hit:
                    matched.append({'filepath': hit['filepath'], 'duration_sec': t['duration_sec'],
                                    'artist': hit['artist'], 'title': hit['title']})
                else:
                    missing.append(t)

            if not matched:
                if os.path.exists(out_m3u): os.remove(out_m3u)
                err(f"{name}  (no local tracks)"); failed += 1; continue

            if os.path.exists(out_m3u): os.remove(out_m3u)
            generate_m3u(matched, out_m3u)

            miss_str = f"  {C.Y}{len(missing)} missing{C.RS}" if missing else ""
            ok(f"{name}  {len(matched)}/{len(pl_tracks)}{miss_str}")
            created += 1

    sep()
    print(f"  {C.G}{created} created{C.RS}" + (f"  {C.R}{failed} failed{C.RS}" if failed else ""))


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    load_spotify_credentials()
    prompt_reset()
    fetch_spotify_zip()
    sync_playlist()
    run_playlist_generator()

    if os.path.exists(SPOTIFY_ZIP_PATH):
        os.remove(SPOTIFY_ZIP_PATH)

    # Rename /Songs → /#Songs so Samsung Music picks it up
    new_dir = "/storage/emulated/0/#Songs"
    if os.path.exists(OUTPUT_DIR):
        try:
            os.rename(OUTPUT_DIR, new_dir)
        except Exception as e:
            err(f"rename failed: {e}")


if __name__ == "__main__":
    main()



