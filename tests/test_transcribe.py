from tiktok_archive.transcribe import UNAVAILABLE_RE, pick_caption_track, subtitles_to_text

VTT = """WEBVTT
Kind: captions
Language: en

1
00:00:00.000 --> 00:00:02.000
Hey guys, <c>today</c> I&#39;ll show you

2
00:00:02.000 --> 00:00:04.000
Hey guys, <c>today</c> I&#39;ll show you

00:00:04.000 --> 00:00:06.500 align:start position:0%
three Excel tricks
"""

SRT = """1
00:00:00,000 --> 00:00:02,000
First line

2
00:00:02,000 --> 00:00:04,000
2024 was a big year
"""


def test_vtt_drops_headers_timings_tags_and_repeats():
    assert subtitles_to_text(VTT) == "Hey guys, today I'll show you three Excel tricks"


def test_srt_drops_cue_numbers_but_keeps_numeric_text():
    assert subtitles_to_text(SRT) == "First line 2024 was a big year"


def test_pick_caption_track_prefers_by_prefix_order():
    tracks = {
        "spa-ES": {"filepath": "a.spa-ES.vtt"},
        "eng-US": {"filepath": "a.eng-US.vtt"},
    }
    assert pick_caption_track(tracks, ("eng", "en", "spa"))[0] == "eng-US"
    assert pick_caption_track(tracks, ("spa", "eng"))[0] == "spa-ES"


def test_pick_caption_track_falls_back_and_ignores_missing_files():
    tracks = {"fra-FR": {"filepath": "a.fra.vtt"}, "eng-US": {"url": "no file"}}
    assert pick_caption_track(tracks, ("eng",))[0] == "fra-FR"
    assert pick_caption_track({}, ("eng",)) is None


def test_unavailable_detection():
    assert UNAVAILABLE_RE.search("ERROR: [TikTok] 123: Video not available, status code 10204")
    assert not UNAVAILABLE_RE.search("ERROR: Unable to download webpage: HTTP Error 429")
