-- 001_init: the videos table (Stage 4) with pgvector embeddings (Stage 5).
-- Applied by `python -m tiktok_archive.migrate`, which records it in schema_migrations.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE videos (
    video_id          TEXT PRIMARY KEY,
    username          TEXT,
    canonical_url     TEXT NOT NULL,
    share_link        TEXT,
    date_saved        TIMESTAMP,

    -- Your own review flag. The pipeline never changes it; see tiktok_archive.mark.
    status            TEXT NOT NULL DEFAULT 'unprocessed'
                      CONSTRAINT videos_status_check CHECK (status IN ('unprocessed', 'noted')),

    -- Pipeline progress: pending -> transcribed -> done. 'failed' rows are retried
    -- until attempts reaches MAX_ATTEMPTS; 'unavailable' (deleted/private) never is.
    processing_state  TEXT NOT NULL DEFAULT 'pending'
                      CONSTRAINT videos_processing_state_check
                      CHECK (processing_state IN ('pending', 'transcribed', 'done', 'failed', 'unavailable')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,

    -- Stage 2: from yt-dlp + captions / faster-whisper
    caption           TEXT,                       -- TikTok's description text
    hashtags          TEXT[] NOT NULL DEFAULT '{}',
    duration_seconds  INTEGER,
    transcript        TEXT,
    transcript_source TEXT
                      CONSTRAINT videos_transcript_source_check
                      CHECK (transcript_source IN ('captions', 'whisper', 'none')),
    language          TEXT,

    -- Stage 3: computed locally (classify.py)
    title             TEXT,                       -- first line of the caption
    categories        TEXT[]                      -- multi-label, most relevant first
                      CONSTRAINT videos_categories_check CHECK (
                          cardinality(categories) >= 1
                          AND categories <@ ARRAY[
                              'Good Habits', 'Job Search', 'Information', 'Motivation', 'Thoughts',
                              'Apps & Tools', 'Food', 'Movies & Series', 'Life Hacks', 'Social Skills',
                              'Coding', 'AI', 'Music', 'Ideas', 'Side Hustles', 'Acquire', 'Reading',
                              '3D', 'Job Tips', 'To Talk', 'To Question', 'Videogames', 'Languages',
                              'Break into Tech', 'Around the World', 'Piano', 'Gadgets', 'Technology',
                              'Remote Jobs', 'Hobbies', 'Excel', 'Projects'
                          ]::TEXT[]
                      ),
    tags              TEXT[] NOT NULL DEFAULT '{}',  -- the post's hashtags, lowercase kebab-case
    processed_at      TIMESTAMPTZ,

    -- Stage 5: BAAI/bge-m3 over caption + hashtags + start of the transcript
    embedding         VECTOR(1024),

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER videos_set_updated_at
    BEFORE UPDATE ON videos
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX videos_categories_idx       ON videos USING GIN (categories);
CREATE INDEX videos_tags_idx             ON videos USING GIN (tags);
CREATE INDEX videos_status_idx           ON videos (status);
CREATE INDEX videos_processing_state_idx ON videos (processing_state);
-- No vector index yet: an exact scan over ~6k rows takes milliseconds and stays
-- exact under category/status filters. Add HNSW past ~50k rows:
--   CREATE INDEX videos_embedding_idx ON videos USING hnsw (embedding vector_cosine_ops);
