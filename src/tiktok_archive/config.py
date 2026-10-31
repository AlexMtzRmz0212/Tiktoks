"""Settings, loaded from environment variables (and .env at the repo root)."""

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# Must match the VECTOR(n) column in migrations/001_init.sql.
EMBEDDING_DIM = 1024

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _path(name: str, default: str) -> Path:
    p = Path(_env(name, default)).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass(frozen=True)
class Settings:
    database_url: str
    favorites_csv: Path
    vault_path: Path
    media_dir: Path
    caption_langs: tuple[str, ...]
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    ytdlp_cookies_from_browser: str
    embedding_model: str
    embedding_device: str
    max_attempts: int
    related_max: int
    related_min_similarity: float
    search_min_similarity: float
    topic_similarity: float
    topic_min_size: int
    chunk_chars: int
    chunk_max: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=_env("DATABASE_URL"),
        favorites_csv=_path("FAVORITES_CSV", "data/favorites.csv"),
        vault_path=_path("VAULT_PATH", "vault"),
        media_dir=_path("MEDIA_DIR", "data/media"),
        caption_langs=tuple(
            lang.strip().lower() for lang in _env("CAPTION_LANGS", "eng,en,spa,es").split(",") if lang.strip()
        ),
        whisper_model=_env("WHISPER_MODEL", "large-v3-turbo"),
        whisper_device=_env("WHISPER_DEVICE", "auto"),
        whisper_compute_type=_env("WHISPER_COMPUTE_TYPE"),
        ytdlp_cookies_from_browser=_env("YTDLP_COOKIES_FROM_BROWSER"),
        embedding_model=_env("EMBEDDING_MODEL", "BAAI/bge-m3"),
        embedding_device=_env("EMBEDDING_DEVICE", "cpu"),
        max_attempts=int(_env("MAX_ATTEMPTS", "3")),
        related_max=int(_env("RELATED_MAX", "5")),
        related_min_similarity=float(_env("RELATED_MIN_SIMILARITY", "0.52")),
        search_min_similarity=float(_env("SEARCH_MIN_SIMILARITY", "0.45")),
        topic_similarity=float(_env("TOPIC_SIMILARITY", "0.50")),
        topic_min_size=int(_env("TOPIC_MIN_SIZE", "4")),
        chunk_chars=int(_env("CHUNK_CHARS", "800")),
        chunk_max=int(_env("CHUNK_MAX", "8")),
    )


def utf8_console() -> None:
    """Windows consoles default to cp1252, which can't print emoji from captions or usernames."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
