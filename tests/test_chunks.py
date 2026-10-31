from tiktok_archive.classify import TRANSCRIPT_CHARS, chunk_text


def long_text(sentences=60):
    return " ".join(f"This is sentence number {i} about one idea." for i in range(sentences))


def test_short_text_gets_no_parts_because_the_whole_fingerprint_covers_it():
    assert chunk_text("x" * TRANSCRIPT_CHARS, size=800) == []
    assert chunk_text("", size=800) == []
    assert chunk_text(None, size=800) == []


def test_chunking_can_be_turned_off():
    assert chunk_text(long_text(), size=0) == []


def test_parts_are_about_the_requested_size_and_cover_the_text():
    text = long_text()
    parts = chunk_text(text, size=800, overlap=100, max_chunks=50)
    assert len(parts) >= 3
    assert all(len(p) <= 800 for p in parts)
    assert parts[0].startswith("This is sentence number 0")
    assert parts[-1].endswith("sentence number 59 about one idea.")


def test_parts_end_at_sentence_ends_and_neighbours_overlap():
    parts = chunk_text(long_text(), size=800, overlap=100, max_chunks=50)
    assert all(p.endswith(".") for p in parts)
    last_sentence_of_first = parts[0].split(". ")[-1]
    assert last_sentence_of_first in parts[1]  # shared so an idea across the boundary is whole in one part


def test_never_more_than_max_chunks():
    assert len(chunk_text(long_text(300), size=800, max_chunks=4)) == 4


def test_text_without_sentence_ends_is_still_cut():
    parts = chunk_text("word " * 1000, size=800, max_chunks=50)
    assert len(parts) > 1 and all(len(p) <= 800 for p in parts)
