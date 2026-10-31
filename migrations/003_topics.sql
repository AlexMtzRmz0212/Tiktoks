-- 003_topics: themes found by grouping videos whose fingerprints are close (topics.py).
-- The whole set is rebuilt every time, so topic ids are not stable between runs.

CREATE TABLE topics (
    topic_id   SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE videos
    ADD COLUMN topic_id   INTEGER REFERENCES topics (topic_id) ON DELETE SET NULL,
    ADD COLUMN topic_rank INTEGER;  -- 1 = closest to the topic's centre

CREATE INDEX videos_topic_idx ON videos (topic_id);
