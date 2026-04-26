<img width="1080" height="2340" alt="27c92dad-5964-437a-9f60-bb0e0a9abcdb-1_all_54897" src="https://github.com/user-attachments/assets/a7fb1699-7ecd-4b8c-be80-4d264f57d2b5" />

WARNING: This script will only download the original Liked playlist, other songs in other playlists that are not in the Liked playlist will not be downloaded.

After the download, other playlists will still be created, but songs that are exclusively on these playlists will be missing.

NOTE: This script may break at any time as it depends on external APIs and services that change without notice. If something stops working, check the original source for an updated version.

── SETUP (Termux on Android)

Allow storage access: termux-setup-storage
Install dependencies: pkg install python python-pip ffmpeg

Go to Downloads: cd /storage/emulated/0/Download

Run the script: python spotify_downloader.py

Python packages (yt-dlp, mutagen, spotipy, etc.) install automatically.

 ── SPOTIFY API CREDENTIALS

On first run the script will ask for your Client ID and Secret and save them to the data folder. To create your own credentials:

1. Go to: https://developer.spotify.com/dashboard
2. Log in and click "Create app"
3. Fill in any name and description
4. Set Redirect URI to: http://127.0.0.1:9090
5. Check "Web API" and save
6. Open the app settings and copy the Client ID and Client Secret

 ── USING THE SCRIPT

Log in to your Spotify account when prompted.

All liked songs are downloaded to /storage/emulated/0/Songs

Playlists are exported as .m3u to /storage/emulated/0/Songs/Playlists

After it finishes, rename the folder to refresh Samsung Music:
#Songs → Songs (at /storage/emulated/0/)

Import the .m3u playlist file in Samsung Music (hiding the Spotify tab is optional but keeps things tidy).

<img width="1080" height="2340" alt="1000154164" src="https://github.com/user-attachments/assets/9ee7c532-9326-4d69-b8a2-b46d6d654363" />
<img width="1080" height="2340" alt="1000154162" src="https://github.com/user-attachments/assets/ec847ea9-2d37-4870-b067-29465bd0513e" />
