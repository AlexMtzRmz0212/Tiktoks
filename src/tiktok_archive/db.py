"""Postgres access. Every read and write of the videos table goes through here."""

from collections.abc import Iterable, Sequence
from datetime import datetime

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import get_settings

EXPORT_COLUMNS = """
    video_id, username, canonical_url, date_saved, status, title, caption, hashtags,
    transcript, categories, tags, transcript_source, topic_id
"""


def connect(*, vector: bool = True, timeout: int | None = None) -> psycopg.Connection:
    """Open a connection. Used as `with connect() as conn:`, which commits on exit."""
    url = get_settings().database_url
    if not url:
        raise SystemExit("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    # Never wait forever for a database that isn't running (Docker stopped, wrong port).
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=timeout or 10)
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
            -- Stage 1 may only know the id (address .../@/video/<id>); fill in the real handle once it's known.
            canonical_url = CASE WHEN %(username)s::text ~ '^[A-Za-z0-9_.-]+$'
                                 THEN 'https://www.tiktok.com/@' || %(username)s::text || '/video/' || video_id
                                 ELSE canonical_url END,
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


def related_videos(
    conn: psycopg.Connection,
    video_ids: Sequence[str],
    *,
    k: int,
    min_similarity: float,
) -> dict[str, list[dict]]:
    """For each video, the finished videos closest in meaning to it (by embedding), best first.

    Each entry has video_id, title, similarity and shared_tags (tags both videos have).
    """
    related: dict[str, list[dict]] = {}
    ids = list(video_ids)
    for start in range(0, len(ids), 200):
        cur = conn.execute(
            """
            SELECT v.video_id AS source, n.video_id, n.title, 1 - (n.embedding <=> v.embedding) AS similarity,
                   ARRAY(SELECT t FROM unnest(v.tags) t WHERE t = ANY(n.tags)) AS shared_tags
            FROM videos v
            CROSS JOIN LATERAL (
                SELECT o.video_id, o.title, o.tags, o.embedding FROM videos o
                WHERE o.video_id <> v.video_id AND o.processing_state = 'done' AND o.embedding IS NOT NULL
                ORDER BY o.embedding <=> v.embedding
                LIMIT %(k)s
            ) n
            WHERE v.video_id = ANY(%(ids)s) AND v.embedding IS NOT NULL
            ORDER BY v.video_id, similarity DESC
            """,
            {"k": k, "ids": ids[start:start + 200]},
        )
        for r in cur.fetchall():
            if r["similarity"] >= min_similarity:
                related.setdefault(r["source"], []).append(
                    {"video_id": r["video_id"], "title": r["title"], "similarity": float(r["similarity"]), "shared_tags": r["shared_tags"]}
                )
    return related


def topic_inputs(conn: psycopg.Connection) -> list[dict]:
    """Finished videos with their fingerprint, for grouping into topics."""
    return conn.execute(
        """
        SELECT video_id, title, username, tags, categories, embedding
        FROM videos
        WHERE processing_state = 'done' AND embedding IS NOT NULL
        ORDER BY video_id
        """
    ).fetchall()


def replace_topics(conn: psycopg.Connection, topics: Sequence[dict]) -> None:
    """Replace every topic. Each topic is {name, members: [video_id, ...]} with members closest-to-centre first."""
    conn.execute("UPDATE videos SET topic_id = NULL, topic_rank = NULL WHERE topic_id IS NOT NULL")
    conn.execute("DELETE FROM topics")
    for t in topics:
        topic_id = conn.execute(
            "INSERT INTO topics (name, size) VALUES (%s, %s) RETURNING topic_id", (t["name"], len(t["members"]))
        ).fetchone()["topic_id"]
        conn.execute(
            """
            UPDATE videos SET topic_id = %s, topic_rank = m.rank
            FROM unnest(%s::text[]) WITH ORDINALITY AS m(video_id, rank)
            WHERE videos.video_id = m.video_id
            """,
            (topic_id, list(t["members"])),
        )


def topics_with_members(conn: psycopg.Connection) -> list[dict]:
    """Every topic with its finished member videos (closest to the centre first), for the Obsidian hubs."""
    topics = {
        r["topic_id"]: {"topic_id": r["topic_id"], "name": r["name"], "members": []}
        for r in conn.execute("SELECT topic_id, name FROM topics ORDER BY size DESC, name").fetchall()
    }
    for r in conn.execute(
        """
        SELECT topic_id, video_id, title, username FROM videos
        WHERE topic_id IS NOT NULL AND processing_state = 'done'
        ORDER BY topic_id, topic_rank, video_id
        """
    ).fetchall():
        topics[r["topic_id"]]["members"].append(r)
    return list(topics.values())


def state_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Number of videos in each processing_state."""
    cur = conn.execute("SELECT processing_state, count(*) AS n FROM videos GROUP BY 1")
    return {r["processing_state"]: r["n"] for r in cur.fetchall()}


def queue_breakdown(conn: psycopg.Connection, max_attempts: int) -> dict[str, int]:
    """What fetch_queue would pick up, split by how far along each video already is."""
    return conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE processing_state = 'pending') AS pending,
            count(*) FILTER (WHERE processing_state = 'transcribed') AS transcribed,
            count(*) FILTER (WHERE processing_state = 'failed' AND attempts < %(m)s) AS retryable,
            count(*) FILTER (WHERE processing_state = 'failed' AND attempts >= %(m)s) AS gave_up
        FROM videos
        """,
        {"m": max_attempts},
    ).fetchone()


def existing_video_ids(conn: psycopg.Connection) -> set[str]:
    return {r["video_id"] for r in conn.execute("SELECT video_id FROM videos").fetchall()}


def list_videos(
    conn: psycopg.Connection,
    *,
    state: str | None = None,
    category: str | None = None,
    status: str | None = None,
    text: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """A page of videos for the UI, newest saves first, plus the total number matching the filters."""
    where, params = ["TRUE"], []
    if state:
        where.append("processing_state = %s")
        params.append(state)
    if category:
        where.append("%s = ANY(categories)")
        params.append(category)
    if status:
        where.append("status = %s")
        params.append(status)
    if text:
        where.append("(title ILIKE %s OR username ILIKE %s OR caption ILIKE %s)")
        params += [f"%{text}%"] * 3
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) AS n FROM videos WHERE {clause}", params).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT video_id, username, canonical_url, date_saved, status, processing_state, attempts, last_error,
               title, categories, tags, transcript_source
        FROM videos WHERE {clause}
        ORDER BY date_saved DESC NULLS LAST, video_id
        LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    ).fetchall()
    return rows, total


def replace_chunks(conn: psycopg.Connection, video_id: str, chunks: Sequence[str], embeddings: Sequence) -> None:
    """Store the per-part fingerprints of a long transcript, replacing any earlier ones."""
    conn.execute("DELETE FROM video_chunks WHERE video_id = %s", (video_id,))
    for number, (text, vector) in enumerate(zip(chunks, embeddings), start=1):
        conn.execute(
            "INSERT INTO video_chunks (video_id, chunk_no, text, embedding) VALUES (%s, %s, %s, %s)",
            (video_id, number, text, vector),
        )


def _search_filters(category, status, tags, saved_after) -> tuple[str, dict]:
    """Shared WHERE fragment for the search queries (finished videos only)."""
    where, params = ["processing_state = 'done'"], {}
    if category:
        where.append("%(category)s = ANY(categories)")
        params["category"] = category
    if status:
        where.append("status = %(status)s")
        params["status"] = status
    if tags:
        where.append("tags && %(tags)s")
        params["tags"] = list(tags)
    if saved_after:
        where.append("date_saved >= %(saved_after)s")
        params["saved_after"] = saved_after
    return " AND ".join(where), params


def vector_ranking(
    conn: psycopg.Connection,
    query_embedding,
    *,
    pool: int,
    category: str | None = None,
    status: str | None = None,
    tags: Sequence[str] | None = None,
    saved_after: datetime | None = None,
) -> list[tuple[str, float, int]]:
    """(video_id, cosine similarity, part) for the `pool` videos closest in meaning to the query, best first.

    A video's score is its best over the whole-video fingerprint (part 0) and the fingerprints of
    the parts of its transcript (parts 1, 2, ...), so one idea inside a long video can match.
    """
    clause, params = _search_filters(category, status, tags, saved_after)
    cur = conn.execute(
        f"""
        WITH scores AS (
            SELECT video_id, 0 AS part, 1 - (embedding <=> %(emb)s) AS similarity
            FROM videos
            WHERE embedding IS NOT NULL AND {clause}
          UNION ALL
            SELECT c.video_id, c.chunk_no AS part, 1 - (c.embedding <=> %(emb)s) AS similarity
            FROM video_chunks c JOIN videos v ON v.video_id = c.video_id
            WHERE {clause}
        ), best AS (
            SELECT DISTINCT ON (video_id) video_id, part, similarity
            FROM scores ORDER BY video_id, similarity DESC
        )
        SELECT video_id, similarity, part FROM best ORDER BY similarity DESC, video_id LIMIT %(pool)s
        """,
        {**params, "emb": query_embedding, "pool": pool},
    )
    return [(r["video_id"], float(r["similarity"]), r["part"]) for r in cur.fetchall()]


def keyword_ranking(
    conn: psycopg.Connection,
    query: str,
    *,
    pool: int,
    category: str | None = None,
    status: str | None = None,
    tags: Sequence[str] | None = None,
    saved_after: datetime | None = None,
) -> list[str]:
    """video_ids whose text contains the query's words, best match first. Quotes, OR and -word work."""
    clause, params = _search_filters(category, status, tags, saved_after)
    cur = conn.execute(
        f"""
        SELECT video_id
        FROM videos, websearch_to_tsquery('simple', %(q)s) AS q
        WHERE search_text @@ q AND {clause}
        ORDER BY ts_rank_cd(search_text, q) DESC, video_id
        LIMIT %(pool)s
        """,
        {**params, "q": query, "pool": pool},
    )
    return [r["video_id"] for r in cur.fetchall()]


def videos_summary(conn: psycopg.Connection, video_ids: Sequence[str]) -> dict[str, dict]:
    """The fields search results show, keyed by video_id."""
    cur = conn.execute(
        """
        SELECT video_id, username, title, categories, tags, status, canonical_url, date_saved
        FROM videos WHERE video_id = ANY(%s)
        """,
        (list(video_ids),),
    )
    return {r["video_id"]: r for r in cur.fetchall()}
