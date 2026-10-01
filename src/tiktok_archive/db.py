"""Postgres access. Every read and write of the videos table goes through here."""

from collections.abc import Iterable, Sequence
from datetime import datetime

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import get_settings

EXPORT_COLUMNS = """
    video_id, username, canonical_url, date_saved, status, title, caption, hashtags,
    transcript, categories, tags, transcript_source
"""


def connect(*, vector: bool = True) -> psycopg.Connection:
    """Open a connection. Used as `with connect() as conn:`, which commits on exit."""
    url = get_settings().database_url
    if not url:
        raise SystemExit("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    conn = psycopg.connect(url, row_factory=dict_row)
    if vector:
        register_vector(conn)
    return conn


# --- loading ------------------------------------------------------------------

def insert_new_videos(conn: psycopg.Connection, rows: Sequence[dict]) -> list[str]:
    """Insert rows that aren't in the table yet. Returns the video_ids actually inserted."""
    if not rows:
        return []
    cur = conn.execute(
        """
        INSERT INTO videos (video_id, username, canonical_url, share_link, date_saved)
        SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::timestamp[])
        ON CONFLICT (video_id) DO NOTHING
        RETURNING video_id
        """,
        (
            [r["video_id"] for r in rows],
            [r["username"] for r in rows],
            [r["canonical_url"] for r in rows],
            [r["share_link"] for r in rows],
            [r["date_saved"] for r in rows],
        ),
    )
    return [r["video_id"] for r in cur.fetchall()]


# --- work queue -----------------------------------------------------------------

def fetch_queue(conn: psycopg.Connection, max_attempts: int, limit: int | None = None) -> list[str]:
    """Videos still to process, newest saves first."""
    cur = conn.execute(
        """
        SELECT video_id FROM videos
        WHERE processing_state IN ('pending', 'transcribed')
           OR (processing_state = 'failed' AND attempts < %s)
        ORDER BY date_saved DESC NULLS LAST, video_id
        LIMIT %s
        """,
        (max_attempts, limit),
    )
    return [r["video_id"] for r in cur.fetchall()]


def fetch_reprocess_ids(
    conn: psycopg.Connection,
    ids: Sequence[str] | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[str]:
    """Already-processed videos matching the filters, for re-running Stage 3."""
    where = ["processing_state = 'done'"]
    params: list = []
    if ids:
        where.append("video_id = ANY(%s)")
        params.append(list(ids))
    if category:
        where.append("%s = ANY(categories)")
        params.append(category)
    params.append(limit)
    cur = conn.execute(
        f"SELECT video_id FROM videos WHERE {' AND '.join(where)} "
        "ORDER BY date_saved DESC NULLS LAST, video_id LIMIT %s",
        params,
    )
    return [r["video_id"] for r in cur.fetchall()]


def get_video(conn: psycopg.Connection, video_id: str) -> dict:
    row = conn.execute("SELECT * FROM videos WHERE video_id = %s", (video_id,)).fetchone()
    if row is None:
        raise KeyError(f"video {video_id} is not in the database")
    return row


# --- writes from the pipeline --------------------------------------------------

def save_transcription(conn: psycopg.Connection, video_id: str, t) -> None:
    conn.execute(
        """
        UPDATE videos SET
            username = COALESCE(%(username)s, username),
            caption = %(caption)s,
            hashtags = %(hashtags)s,
            duration_seconds = %(duration_seconds)s,
            transcript = %(transcript)s,
            transcript_source = %(source)s,
            language = %(language)s,
            processing_state = 'transcribed',
            last_error = NULL
        WHERE video_id = %(video_id)s
        """,
        {
            "video_id": video_id,
            "username": t.username,
            "caption": t.caption,
            "hashtags": t.hashtags,
            "duration_seconds": t.duration_seconds,
            "transcript": t.transcript,
            "source": t.source,
            "language": t.language,
        },
    )


def save_classification(
    conn: psycopg.Connection,
    video_id: str,
    *,
    title: str | None,
    categories: Sequence[str],
    tags: Sequence[str],
    embedding,
) -> None:
    conn.execute(
        """
        UPDATE videos SET
            title = %(title)s,
            categories = %(categories)s,
            tags = %(tags)s,
            embedding = %(embedding)s,
            processed_at = now(),
            processing_state = 'done',
            last_error = NULL
        WHERE video_id = %(video_id)s
        """,
        {
            "video_id": video_id,
            "title": title,
            "categories": list(categories),
            "tags": list(tags),
            "embedding": embedding,
        },
    )


def mark_failed(conn: psycopg.Connection, video_id: str, error: str, *, unavailable: bool = False) -> None:
    """Record a failure. A video that already has notes keeps state 'done' (a failed reprocess)."""
    conn.execute(
        """
        UPDATE videos SET
            processing_state = CASE
                WHEN processing_state = 'done' THEN 'done'
                WHEN %(unavailable)s THEN 'unavailable'
                ELSE 'failed'
            END,
            attempts = attempts + 1,
            last_error = %(error)s
        WHERE video_id = %(video_id)s
        """,
        {"video_id": video_id, "error": error[:2000], "unavailable": unavailable},
    )


def set_status(conn: psycopg.Connection, video_ids: Iterable[str], status: str) -> list[str]:
    cur = conn.execute(
        "UPDATE videos SET status = %s WHERE video_id = ANY(%s) RETURNING video_id",
        (status, list(video_ids)),
    )
    return [r["video_id"] for r in cur.fetchall()]


# --- reads for export / search ---------------------------------------------------

def fetch_for_export(conn: psycopg.Connection, ids: Sequence[str] | None = None) -> list[dict]:
    if ids is None:
        cur = conn.execute(f"SELECT {EXPORT_COLUMNS} FROM videos WHERE processing_state = 'done'")
    else:
        cur = conn.execute(
            f"SELECT {EXPORT_COLUMNS} FROM videos WHERE processing_state = 'done' AND video_id = ANY(%s)",
            (list(ids),),
        )
    return cur.fetchall()


def semantic_search(
    conn: psycopg.Connection,
    query_embedding,
    *,
    category: str | None = None,
    status: str | None = None,
    tags: Sequence[str] | None = None,
    saved_after: datetime | None = None,
    k: int = 10,
) -> list[dict]:
    where = ["embedding IS NOT NULL"]
    params: list = [query_embedding]
    if category:
        where.append("%s = ANY(categories)")
        params.append(category)
    if status:
        where.append("status = %s")
        params.append(status)
    if tags:
        where.append("tags && %s")
        params.append(list(tags))
    if saved_after:
        where.append("date_saved >= %s")
        params.append(saved_after)
    params += [query_embedding, k]
    cur = conn.execute(
        f"""
        SELECT video_id, title, categories, status, tags, canonical_url, date_saved,
               1 - (embedding <=> %s) AS similarity
        FROM videos
        WHERE {' AND '.join(where)}
        ORDER BY embedding <=> %s
        LIMIT %s
        """,
        params,
    )
    return cur.fetchall()
