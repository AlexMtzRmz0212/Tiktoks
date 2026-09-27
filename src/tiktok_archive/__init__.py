"""TikTok saved-video archive: favorites.csv -> Postgres (source of truth) -> Obsidian vault (read-only view)."""

from . import tls

# Before anything imports yt-dlp/curl_cffi or opens a connection.
tls.configure()
