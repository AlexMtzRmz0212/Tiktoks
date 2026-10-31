-- 002_search: keyword search over the same text the meaning search uses.
-- 'simple' means no stemming or stop words, so English and Spanish captions are treated alike.

ALTER TABLE videos ADD COLUMN search_text tsvector;

-- A trigger rather than a generated column: array_to_string isn't immutable.
CREATE FUNCTION videos_update_search_text() RETURNS trigger AS $$
BEGIN
    NEW.search_text := to_tsvector('simple', concat_ws(' ',
        NEW.title, NEW.caption, array_to_string(NEW.hashtags, ' '), array_to_string(NEW.tags, ' '),
        NEW.username, NEW.transcript));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER videos_search_text
    BEFORE INSERT OR UPDATE ON videos
    FOR EACH ROW EXECUTE FUNCTION videos_update_search_text();

CREATE INDEX videos_search_text_idx ON videos USING GIN (search_text);

-- Fill it in for rows that already exist (the trigger runs on this no-op update).
UPDATE videos SET video_id = video_id;
