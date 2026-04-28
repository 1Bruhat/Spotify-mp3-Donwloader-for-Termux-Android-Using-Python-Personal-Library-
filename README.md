<img width="1080" height="2340" alt="27c92dad-5964-437a-9f60-bb0e0a9abcdb-1_all_54897" src="https://github.com/user-attachments/assets/a7fb1699-7ecd-4b8c-be80-4d264f57d2b5" />

WARNING: This script will only download the original Liked playlist, other songs in other playlists that are not in the Liked playlist will not be downloaded.

After the download, other playlists will still be created, but songs that are exclusively on these playlists will be missing.

NOTE: This script may break at any time as it depends on external APIs and services that change without notice. If something stops working, check the original source for an updated version.

── SETUP (Termux on Android)

Copy and paste this command line into termux to install all required packages.

```bash
echo -e "\033[33m>>> You have 10 seconds to allow storage access.\033[0m" && sleep 3 && termux-setup-storage && sleep 13 && echo -e "\033[33m>>> Answer 'y' to all installation prompts.\033[0m" && pkg update -y && pkg upgrade -y && pkg install python python-pip ffmpeg -y && clear && echo -e "\033[32m>>> Starting spotify_downloader.py in Downloads folder.\n\n>>> For next runs, use: python /storage/emulated/0/Download/spotify_downloader.py\n\033[0m" && echo "" && cd /storage/emulated/0/Download && python spotify_downloader.py
```

Alternatively, you can install them manually.

- Allow storage access: termux-setup-storage

- Install dependencies: pkg install python python-pip ffmpeg

(in case of error, install them separately)

- The installation of "pkg upgrade" is recommended

- Go to Downloads: cd /storage/emulated/0/Download

- Run the script: python spotify_downloader.py

- Python packages (yt-dlp, mutagen, spotipy, etc.) install automatically.

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

 ── HOW IT WORKS
 
This script downloads your entire Spotify Liked Songs library and creates M3U playlists for your saved playlists, complete with metadata.

It uses Spotify Web API, ytdlp, YTMusic API, mutagen, LRCLIB, Musixmatch, Spotify Partner API, TOTP and spotipy

- You provide your Spotify API credentials (Client ID and Secret) on first run.
- The script authenticates with Spotify and exports all your Liked Songs and playlists as CSV files inside a ZIP archive.
- It scans local MP3s by ISRC to identify which tracks are missing or extra.
- For each missing song it searches YouTube Music (Best quality and no "videoclip audio") and downloads the best audio match as MP3 using ytdlp.
- It embeds ID3 metadata: title, artist, album year, cover, spotify view count and synced lyrics.
- It fetches the release date and real play count from Spotify Partner API using a live TOTP token and writes both into the lyrics tag.
- After downloading all songs, it generates M3U playlist files based on your Spotify playlists referencing the local MP3 files.
- Finally, it renames the output folder to force Samsung Music to reindex the tracks.

The script is designed for Termux on Android and maintains a persistent Spotify login cache, so subsequent runs only download new or changed tracks.

<img width="1080" height="2340" alt="1000154164" src="https://github.com/user-attachments/assets/9ee7c532-9326-4d69-b8a2-b46d6d654363" />

<img width="590" height="1280" alt="1000154203" src="https://github.com/user-attachments/assets/f3e9fd64-42d1-447d-b92a-9690cb757463" />

<img width="1080" height="2340" alt="1000154162" src="https://github.com/user-attachments/assets/ec847ea9-2d37-4870-b067-29465bd0513e" />

<img width="1080" height="2340" alt="1000154196" src="https://github.com/user-attachments/assets/c070cc73-d172-4f84-bf6c-e0078e0b9a4b" />
