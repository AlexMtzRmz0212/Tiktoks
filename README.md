# TikTok saved-video archive

Turns ~5,600 saved TikToks into a searchable, categorised notes archive:

```
TikTok export ──Stage 1──▶ favorites.csv ──▶ Postgres (source of truth) ──Stage 6──▶ Obsidian vault (read-only view)
                                              ▲   Stage 2: transcript (captions / local whisper)
                                              │   Stage 3: local → title (caption), tags (hashtags), categories (bge-m3 vs. category definitions)
                                              │   Stage 4: stored in `videos`
                                              └── Stage 5: pgvector embedding for semantic search
```

Everything runs locally and costs nothing: no paid API is called. An earlier version used Claude for Stage 3 (title, summary, key points, categories); it's in git history at commit `3187710` if you want it back.

- **Postgres is the single source of truth.** The vault is generated from it, one way only. Nothing reads the vault back, so edits made in Obsidian are overwritten on the next export.
- **Categories are a closed set of 32** (`src/tiktok_archive/categories.py`), enforced by the classifier *and* a database CHECK constraint. A video can have 1–3 categories; the first one is its primary category and decides its vault folder.
- **`status` is your own review flag** (`unprocessed` / `noted`). The pipeline never changes it; you do, with `tiktok_archive.mark`. Pipeline progress lives in a separate `processing_state` column.

## Layout

```
run_pipeline.py              orchestrator: migrate → load CSV → process queue → export
migrations/001_init.sql      videos table, pgvector, constraints, indexes
src/tiktok_archive/
  parse_tiktok_export.py     Stage 1: export JSON → favorites.csv
  loader.py                  favorites.csv → videos (new rows only)
  transcribe.py              Stage 2: yt-dlp + captions / faster-whisper
  classify.py                Stage 3: title, tags and categories, computed locally
  embed.py                   Stages 3 + 5: local BAAI/bge-m3 embeddings
  process.py                 Stages 2–5 per video, and the parallel work queue
  export_obsidian.py         Stage 6: Postgres → vault
  search.py                  semantic search with category/status/tag filters
  mark.py                    set status noted/unprocessed, re-export those notes
  migrate.py                 applies migrations/*.sql, tracked in schema_migrations
  categories.py              the closed category set + definitions the classifier compares against
  config.py                  settings from .env
  tls.py                     HTTPS verification against the OS certificate store
tests/                       pytest (no database needed)
```

## Setup

Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install --upgrade pip
```

If pip inside the venv fails with `CERTIFICATE_VERIFY_FAILED`, the likely cause is antivirus or a proxy inspecting HTTPS. Fix it by upgrading the venv's pip with the global one, which trusts the Windows certificate store: `pip --python "<absolute path>\.venv\Scripts\python.exe" install --upgrade pip`. The pipeline itself handles this on its own (`tiktok_archive/tls.py`): it verifies HTTPS against the OS certificate store, both for Python and for the libcurl that yt-dlp uses to impersonate a browser for TikTok.

Install PyTorch first. The CPU build is enough, because embeddings run on CPU by default and whisper uses CTranslate2 rather than torch:

```bash
.venv/Scripts/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/Scripts/python -m pip install -e ".[dev]"
```

**Postgres with pgvector.** Pick one:
- Docker: `POSTGRES_PASSWORD=... docker compose up -d` (see `docker-compose.yml`; it publishes port 5433 so it doesn't clash with a native Postgres on 5432). Use `127.0.0.1` rather than `localhost` in `DATABASE_URL`: on Windows `localhost` can resolve to IPv6 first and the connection stalls.
- A hosted Postgres that ships pgvector (Supabase, Neon, and others). This is also the easiest route once this gets deployed.
- A native install. On Windows, pgvector has to be compiled or installed separately.

**Configuration.** Copy `.env.example` to `.env` and fill in `DATABASE_URL` and `VAULT_PATH`. `.env` is gitignored.

**GPU whisper (optional).** faster-whisper uses CUDA 12 + cuDNN 9 when they are available (`WHISPER_DEVICE=auto`), and falls back to CPU otherwise. `large-v3-turbo` in `int8_float16` fits a 4 GB card.

## Running

### The web page (easiest way to see what's going on)
```bash
.venv/Scripts/python -m tiktok_archive.ui
```
Opens `http://127.0.0.1:8765` (only reachable from your own computer). Each of the four steps has a card that says in plain words what it will and won't do, with live numbers from your files and database before anything runs. Buttons start the same commands described below and show their output live, and a results table lists every video with its note, transcript and any error. Nothing runs until you press a button. Docker Desktop and the database container must be running first.

### Stage 1: export → favorites.csv
```bash
.venv/Scripts/python -m tiktok_archive.parse_tiktok_export --input user_data_tiktok.json --output data/favorites.csv --failures data/favorites_failed.csv
```
This resolves every share link to its canonical URL. It is resume-safe, and it only needs to run again when there's a new export.

### Everything else in one command
```bash
.venv/Scripts/python run_pipeline.py --limit 20   # try a small sample first, then check the notes
.venv/Scripts/python run_pipeline.py              # the rest; stop with Ctrl+C and re-run to resume
```
`run_pipeline.py` does the following:
1. Applies pending migrations.
2. Loads new rows from `favorites.csv`.
3. Processes the queue, newest saves first: new videos plus earlier failures that still have retries left.
4. Exports notes for just the videos it processed.
5. Prints counts of what was processed and which transcript sources were used.

Useful flags:

| Flag | Effect |
|---|---|
| `--limit N` | process at most N videos |
| `--workers N` | videos in flight (default 4); downloads and classification overlap, whisper runs one at a time |
| `--ids ID ...` | only these videos |
| `--reprocess` + `--ids` / `--category` / `--limit` | redo Stage 3 and the embedding on finished videos, reusing stored transcripts (e.g. after changing category definitions) |
| `--only-export` | no processing; refresh every note in the vault |
| `--no-export` | process only |
| `--keep-media` | don't delete the (rarely downloaded) videos from `MEDIA_DIR` after each job |

### Stages individually
```bash
.venv/Scripts/python -m tiktok_archive.migrate [--status]            # schema
.venv/Scripts/python -m tiktok_archive.loader                        # favorites.csv → videos
.venv/Scripts/python -m tiktok_archive.transcribe <tiktok url>       # Stage 2 smoke test, no DB needed
.venv/Scripts/python -m tiktok_archive.process [--limit N] [--ids …]  # loader + Stages 2–5, no vault
.venv/Scripts/python -m tiktok_archive.export_obsidian [--ids …]      # Stage 6
```

### Marking videos as noted
```bash
.venv/Scripts/python -m tiktok_archive.mark 7688047916308499744 7685551818059009300
.venv/Scripts/python -m tiktok_archive.mark --unnote 7688047916308499744
```

### Searching
On the page: type in the box at the top of **Results**, pick a type, press Search. Or in a terminal:
```bash
.venv/Scripts/python -m tiktok_archive.search "how to negotiate a raise"
.venv/Scripts/python -m tiktok_archive.search "quick dinner" --category Food --status unprocessed -k 20
.venv/Scripts/python -m tiktok_archive.search "xlookup" --mode keywords
```
Three types: **meaning** (the query's fingerprint is compared with every video's, so it works across wording and across English and Spanish), **exact words** (the words must appear in the title, caption, hashtags or transcript; quotes, `OR` and `-word` work), and **both** (the default: the two rankings are merged, so a video that ranks well in either list, and best of all in both, comes first). Each result says how it was found.

- Meaning matches scoring below `SEARCH_MIN_SIMILARITY` (default 0.45) are dropped, so a nonsense query returns nothing instead of the least-bad video. On real queries good matches scored about 0.5 to 0.65.
- Only finished videos are searched. The exact-words side has no stemming (`simple` text search), so "lecture" won't find "lectures"; the meaning side covers that.
- The first search after starting the page loads the model (15 to 20 seconds, about 2.3 GB of memory); later ones take about a second. The scan is exact; add the commented-out HNSW index in the first migration once the table passes roughly 50k rows.

## How processing behaves

- **States.** A video moves `pending` → `transcribed` → `done`. Errors set `failed` and are retried on later runs until `MAX_ATTEMPTS` (3). Deleted or private posts become `unavailable` and are never retried. The transcript is saved before classification runs, so a failure there never costs another download or whisper pass. To check on things:
  `SELECT processing_state, count(*) FROM videos GROUP BY 1;` and `SELECT video_id, last_error FROM videos WHERE processing_state = 'failed';`
- **Transcripts.** TikTok's own captions are used when the post has them (track preference is set by `CAPTION_LANGS`); many posts do, and for those **no video is downloaded**, only metadata and the caption file. Otherwise the video is downloaded to `MEDIA_DIR/<video_id>/`, local faster-whisper transcribes the audio, and the folder is deleted as soon as that video's job ends (so at most `--workers` videos are on disk at once, 4 by default), with voice-activity detection so music isn't turned into invented words. On an RTX 3050, an 80-second video takes about 10 seconds. `transcript_source` is `captions`, `whisper` or `none` (no speech).
- **Categories.** The video's caption, hashtags and the first ~2,000 characters of its transcript are embedded once with bge-m3. That vector is compared with the embedding of each category's `name: definition` from `categories.py`. The best match is the primary category (it decides the vault folder); up to two more are added only when they score within 0.03 of the best and at least 0.30 (`EXTRA_MARGIN` / `EXTRA_FLOOR` in `classify.py`). The same vector is stored for search, so classification adds no extra model pass. It looks at text only: a video with no speech and a bare caption (a silent piano clip, a photo slideshow whose content is text on the slides) gets a weak guess. Nothing looks at images or frames.
- **Unavailable posts.** A deleted or private post becomes `unavailable`. TikTok's status 10204, which yt-dlp reports as "Your IP address is blocked from accessing this post", shows up for individual removed or region-locked posts. It isn't a sign your IP is blocked, so it's treated as an ordinary failure and retried up to `MAX_ATTEMPTS`.
- **Titles and tags.** The title is the first line of the caption without hashtags (cut at 80 characters); a post with no caption text is titled `TikTok <video_id>` in the vault. Tags are the post's own hashtags, normalised to lowercase kebab-case so they work as Obsidian tags (max 8).
- **Cost.** None: yt-dlp, faster-whisper and bge-m3 all run on your machine, and no API key is needed.
- **Time.** Downloading and transcription dominate, so the full backfill takes hours. Chunk it with `--limit`, or just let it run and resume.

## The vault

Each note is `<VAULT_PATH>/<primary category>/<video_id>.md`:

```markdown
---
video_id: '7688047916308499744'
title: Three Excel tricks for lookups
aliases:
- Three Excel tricks for lookups
category: Excel
categories:
- Excel
- Life Hacks
status: unprocessed
tags:
- excel
- xlookup
date_saved: '2026-09-21T19:10:08'
url: https://www.tiktok.com/@someone/video/7688047916308499744
username: someone
transcript_source: captions
---
# Three Excel tricks for lookups
@someone · saved 2026-09-21 · [Open on TikTok](…)
Three Excel tricks for lookups #excel #xlookup
## Related
- [[7685551818059009300|Excel formulas you must know]] · shared: #excel
## Transcript
Today I'll show you…
```

### Topics
Step 4 on the page (optional), or `python -m tiktok_archive.topics`. It groups all finished videos whose fingerprints are close (average-linkage clustering on cosine distance) and names each group after the hashtags that set it apart from the rest of your archive (a tag's score is how many of the group's videos have it times how rare it is overall, so `fyp` never wins). A group needs `TOPIC_MIN_SIZE` videos (default 4) and members must be on average `TOPIC_SIMILARITY` alike (default 0.50, between the 0.59 to 0.67 measured for same-topic videos and the 0.38 to 0.46 for unrelated ones). Run it with `--preview`, or press "Show the topics it would make" on the page, to see the result before anything is saved.

Step 5 then adds `topic: <name>` and a `Topic: [[...]]` link to each note, and writes one hub note per topic to `<vault>/_Topics/`, listing its videos with the most typical first. The export only touches files in `_Topics/` that carry the `tiktok_archive_topic: true` property, so anything else you put there is safe. Topics are rebuilt from scratch every time (hubs of topics that no longer exist are deleted), so run steps 4 and 5 again after processing more videos. The thresholds need a real archive to tune: with a handful of videos nothing groups, which is correct.

### Long videos are matched by part
A video's main fingerprint covers its caption, hashtags and the first ~2,000 characters of the transcript. When the transcript is longer than that, it is also cut into parts of about `CHUNK_CHARS` (default 800) at sentence ends, up to `CHUNK_MAX` parts (default 8, so roughly the first 6,400 characters), and each part gets its own fingerprint. Search then scores a video by its best part, and the results page says which part matched ("part 2 of its transcript"). Example: a video about remote work with a few sentences about a workout scores 0.34 against a fitness query on its main fingerprint alone, and 0.59 once its parts are included.

Videos that fit in the main fingerprint (most captions-only TikToks) get no parts and cost nothing extra. Embedding takes about 0.5 seconds per part on CPU, so a long video adds up to about 4 seconds. Related links, categories and topics keep using the main fingerprint. Set `CHUNK_CHARS=0` to turn it off; parts of videos processed earlier appear when you reprocess them (`run_pipeline.py --reprocess --ids ...`, free).

### Links between notes
Every note ends up linked to the videos closest to it in meaning. When a note is written, the database is asked for the finished videos whose search fingerprint (the bge-m3 vector of caption, hashtags and the start of the transcript) is most similar. Up to `RELATED_MAX` of them (default 5) that score at least `RELATED_MIN_SIMILARITY` (default 0.52) become `[[links]]` in the **Related** section, with the hashtags the two videos share. Obsidian then draws them in its graph and shows them as backlinks, so videos about the same idea connect even when their tags differ, and across English and Spanish.

- **Where 0.52 comes from:** with this model, videos on the same subject score about 0.59 to 0.67 and unrelated ones about 0.38 to 0.46, so 0.52 sits in the gap. Lower it in `.env` for more links, raise it for fewer, closer ones. Check it again after a big run, because real captions are messier than test sentences.
- **Only finished videos are linked**, and a note's links reflect what was in the database when it was written. After processing more videos, run step 5 again (or `run_pipeline.py --only-export`) so older notes pick up their new neighbours. Notes whose links didn't change aren't rewritten.
- **Text only:** a video with a bare caption and no speech has little to match on and gets few or no links.

Re-exporting updates notes in place. When a video's primary category changes, its note moves to the new folder. Files are only rewritten when their content changes. Only `<video_id>.md` files for videos in the database are ever touched, so your own notes in the same vault are safe. `aliases` lets Obsidian's quick switcher find notes by title.

Dataview examples (prefix the folder if `VAULT_PATH` is a subfolder of your vault):

````markdown
```dataview
TABLE category, date_saved
FROM "Coding"
WHERE status = "unprocessed"
SORT date_saved DESC
```

```dataview
TABLE categories, tags
WHERE contains(categories, "AI") AND contains(tags, "prompting")
```

```dataview
TABLE length(rows) AS videos
WHERE video_id
GROUP BY category
SORT length(rows) DESC
```
````

## Changing things later

- **Category definitions**: edit the descriptions in `categories.py`. They are embedded and compared with each video, so the wording directly shapes the result; keep each one about what the videos *say*, and avoid words that overlap with another category (for example "coding projects" under Projects pulls programming videos away from Coding). Then `run_pipeline.py --reprocess --category <name>` (or `--limit N`) re-judges the videos you want, for free.
- **Adding AI again**: Stage 3 is the only place a paid model was involved. `process_video` in `process.py` is where it plugs in (see commit `3187710` for the Claude version, which also produced summaries and key points).
- **Category names** (adding, renaming or removing one): update `categories.py` *and* add a migration that replaces the `videos_categories_check` constraint. `tests/test_categories.py` fails if the two drift apart.
- **Embedding model**: must produce 1024-d vectors, or add a migration that changes the `embedding` column and re-embeds.

## Stage 7 (TODO, not built yet): capturing new saves as they happen

TikTok's data export is a manual "request, wait, download" flow with no API, so it can't be the ongoing feed. The plan:

1. **A capture point.** Use a Telegram bot, or a phone share-sheet shortcut (iOS Shortcuts / Android HTTP Shortcuts), that receives a TikTok share link the moment you save something.
2. **A small endpoint** receives the link and does the following:
   1. Resolves the link to its canonical URL (the same logic as Stage 1's `resolve_link`).
   2. Inserts the video with `db.insert_new_videos` (`date_saved` = now).
   3. Calls `process.process_video(video_id)`, then `export_obsidian.export([video_id])`.

   Because `process_video` is already the single per-video entry point, no pipeline code changes are needed.
3. **The periodic TikTok export stays as a safety net.** Run it every few months through Stage 1 + `run_pipeline.py` to catch anything the capture point missed. Existing videos are skipped automatically.

Deployment notes for later: all configuration is environment variables, the database can be any hosted pgvector Postgres, and whisper and embeddings can run on CPU (slower) if the host has no GPU. A Dockerfile plus a small web framework for the Stage 7 endpoint are the remaining pieces.

## Tests

```bash
.venv/Scripts/python -m pytest
```
These cover subtitle parsing, caption track choice, CSV loading, the export parser's two JSON layouts, the category list matching the database constraint, title and tag helpers, the category-picking rules, note rendering and YAML round-tripping, and vault upsert/move behaviour. None of them need a database or a model download.
