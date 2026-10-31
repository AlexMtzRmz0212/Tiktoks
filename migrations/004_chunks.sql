-- 004_chunks: a fingerprint per part of a long transcript, so search can match one idea in a long video.
-- Videos whose text fits in the whole-video fingerprint (the first ~2000 characters) have no rows here.

CREATE TABLE video_chunks (
    video_id  TEXT NOT NULL REFERENCES videos (video_id) ON DELETE CASCADE,
    chunk_no  INTEGER NOT NULL,           -- 1, 2, 3 ... in reading order
    text      TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    PRIMARY KEY (video_id, chunk_no)
);
