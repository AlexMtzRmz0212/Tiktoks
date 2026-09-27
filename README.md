# TikTok saved-video archive

Turns ~5,600 saved TikToks into a searchable, categorised notes archive:

```
TikTok export ──Stage 1──▶ favorites.csv ──▶ Postgres (source of truth) ──Stage 6──▶ Obsidian vault (read-only view)
                                              ▲   Stage 2: transcript (captions / local whisper)
                                              │   Stage 3: Claude → title, summary, key points, categories, tags
                                              │   Stage 4: stored in `videos`
                                              └── Stage 5: pgvector embedding for semantic search
```

- **Postgres is the single source of truth.** The vault is generated from it, one way only. Nothing reads the vault back, so edits made in Obsidian are overwritten on the next export.
- **Categories are a closed set of 32** (`src/tiktok_archive/categories.py`), enforced by the Claude output schema *and* a database CHECK constraint. A video can have 1–3 categories; the first one is its primary category and decides its vault folder.
- **`status` is your own review flag** (`unprocessed` / `noted`). The pipeline never changes it; you do, with `tiktok_archive.mark`. Pipeline progress lives in a separate `processing_state` column.

## Layout

```
run_pipeline.py              orchestrator: migrate → load CSV → process queue → export
migrations/001_init.sql      videos table, pgvector, constraints, indexes
src/tiktok_archive/
  parse_tiktok_export.py     Stage 1: export JSON → favorites.csv
  loader.py                  favorites.csv → videos (new rows only)
  transcribe.py              Stage 2: yt-dlp + captions / faster-whisper
  understand.py              Stage 3: Claude (+ ffmpeg keyframes for visual videos)
  embed.py                   Stage 5: local BAAI/bge-m3 embeddings
  process.py                 Stages 2–5 per video, and the parallel work queue
  export_obsidian.py         Stage 6: Postgres → vault
  search.py                  semantic search with category/status/tag filters
  mark.py                    set status noted/unprocessed, re-export those notes
  migrate.py                 applies migrations/*.sql, tracked in schema_migrations
  categories.py              the closed category set + definitions used in the prompt
  config.py                  settings from .env
  tls.py                     HTTPS verification against the OS certificate store
tests/                       pytest (no database or API key needed)
```

## Setup

Python 3.11+ and ffmpeg on `PATH` (for keyframes).

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
- Docker: `POSTGRES_PASSWORD=... docker compose up -d` (see `docker-compose.yml`).
- A hosted Postgres that ships pgvector (Supabase, Neon, and others). This is also the easiest route once this gets deployed.
- A native install. On Windows, pgvector has to be compiled or installed separately.

**Configuration.** Copy `.env.example` to `.env` and fill in `DATABASE_URL`, `ANTHROPIC_API_KEY` and `VAULT_PATH`. `.env` is gitignored.

**GPU whisper (optional).** faster-whisper uses CUDA 12 + cuDNN 9 when they are available (`WHISPER_DEVICE=auto`), and falls back to CPU otherwise. `large-v3-turbo` in `int8_float16` fits a 4 GB card.

## Running

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
5. Prints counts, token usage and an estimated cost.

Useful flags:

| Flag | Effect |
|---|---|
| `--limit N` | process at most N videos |
| `--workers N` | videos in flight (default 4); downloads and Claude calls overlap, whisper runs one at a time |
| `--ids ID ...` | only these videos |
| `--model M` | Claude model for this run |
| `--reprocess` + `--ids` / `--category` / `--analyzed-with` / `--limit` | redo Stage 3 on finished videos, reusing stored transcripts (e.g. after changing model or category definitions) |
| `--only-export` | no processing; refresh every note in the vault |
| `--no-export` | process only |
| `--keep-media` | keep downloaded videos in `MEDIA_DIR` |

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
```bash
.venv/Scripts/python -m tiktok_archive.search "how to negotiate a raise"
.venv/Scripts/python -m tiktok_archive.search "quick dinner" --category Food --status unprocessed -k 20
.venv/Scripts/python -m tiktok_archive.search "automation" --tag python --since 2025-01-01
```
Search embeds the query with the same local model and ranks by cosine similarity over `summary + key points`, after applying the filters. The scan is exact; add the commented-out HNSW index in the migration once the table passes roughly 50k rows.

## How processing behaves

- **States.** A video moves `pending` → `transcribed` → `done`. Errors set `failed` and are retried on later runs until `MAX_ATTEMPTS` (3). Deleted or private posts become `unavailable` and are never retried. The transcript is saved before Claude runs, so a failed Claude call never costs another download or whisper pass. To check on things:
  `SELECT processing_state, count(*) FROM videos GROUP BY 1;` and `SELECT video_id, last_error FROM videos WHERE processing_state = 'failed';`
- **Transcripts.** TikTok's own captions are used when the post has them (track preference is set by `CAPTION_LANGS`); many posts do. Otherwise local faster-whisper transcribes the audio, with voice-activity detection so music isn't turned into invented words. On an RTX 3050, an 80-second video takes about 10 seconds. `transcript_source` is `captions`, `whisper` or `none` (no speech).
- **Images.** For 3D, Piano and Gadgets the picture matters more than the audio. The category isn't known until Claude answers, so images go on the first call only when the post looks visual: under 25 words of speech, or visual keywords in the caption or hashtags. Otherwise a second call with images runs only if Claude picks one of those three categories. For a normal video, the images are 4 ffmpeg keyframes. **Photo-mode (slideshow) posts** download as just their music, and their content is usually the text on the slides, so their slides are sent instead (up to 6). `used_keyframes` records whether images were sent.
- **Unavailable posts.** A deleted or private post becomes `unavailable`. TikTok's status 10204, which yt-dlp reports as "Your IP address is blocked from accessing this post", shows up for individual removed or region-locked posts. It isn't a sign your IP is blocked, so it's treated as an ordinary failure and retried up to `MAX_ATTEMPTS`.
- **Tags.** Claude sees the ~150 most-used tags so far and is asked to reuse them. Tags are normalised to lowercase kebab-case so they work as Obsidian tags.
- **Cost.** Each video goes to Claude once (twice for some visual videos). With `claude-sonnet-5` at `ANTHROPIC_EFFORT=low`, the one-time backfill is roughly $50–65 and each new save afterwards about $0.01. Re-runs cost nothing unless you use `--reprocess`. The run summary prints the actual token usage.
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
## Summary
…
## Key points
- …
```

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

- **Model**: set `ANTHROPIC_MODEL` (every row records `analysis_model`). To redo existing notes with the new model:
  `run_pipeline.py --reprocess --analyzed-with claude-sonnet-5 --limit 50`
- **Category definitions**: edit the descriptions in `categories.py`. They go into the prompt verbatim. Then `--reprocess` whichever videos you want re-judged.
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
These cover subtitle parsing, caption track choice, CSV loading, the export parser's two JSON layouts, the category list matching the database constraint, tag normalisation, the output schema's category enum, note rendering and YAML round-tripping, and vault upsert/move behaviour. None of them need a database or an API key.
