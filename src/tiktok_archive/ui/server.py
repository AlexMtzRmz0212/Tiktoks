"""A local web page for the pipeline: what each stage will do, run it, watch it, see the results.

    python -m tiktok_archive.ui            # opens http://127.0.0.1:8765

Nothing runs until you press a button. The buttons start the same commands you
would type in a terminal (`python -m tiktok_archive.process ...` and so on), as a
child process, and show their output live. Uses only the standard library and
listens on 127.0.0.1, so it is reachable only from this computer.
"""

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import db, embed, export_obsidian, migrate, parse_tiktok_export, topics
from .. import search as search_module
from ..categories import CATEGORY_NAMES
from ..config import PROJECT_ROOT, get_settings

INDEX_HTML = Path(__file__).with_name("index.html")
MAX_LOG_LINES = 4000
VIDEO_ID_RE = re.compile(r"^\d{6,25}$")
STATES = ("pending", "transcribed", "done", "failed", "unavailable")


def export_json_path() -> Path:
    return Path(os.environ.get("EXPORT_JSON") or PROJECT_ROOT / "user_data_tiktok.json")


def failures_csv_path() -> Path:
    return get_settings().favorites_csv.with_name("favorites_failed.csv")


def first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


# --- running commands -----------------------------------------------------------

class Job:
    """At most one pipeline command at a time, with its output kept for the page to poll."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stage: str | None = None
        self.command = ""
        self.lines: list[str] = []
        self.dropped = 0  # lines trimmed off the front, so offsets stay stable
        self.exit_code: int | None = None
        self.started: float | None = None
        self.finished: float | None = None
        self.stopped = False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, stage: str, argv: list[str]) -> None:
        with self.lock:
            if self.running():
                raise RuntimeError("something is already running; wait for it or stop it first")
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(
                [sys.executable, "-u", *argv], cwd=PROJECT_ROOT, env=env, creationflags=flags,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
            self.stage = stage
            self.command = "python " + " ".join(argv)
            self.lines, self.dropped = [], 0
            self.exit_code, self.finished, self.stopped = None, None, False
            self.started = time.time()
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _add(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                cut = len(self.lines) - MAX_LOG_LINES
                del self.lines[:cut]
                self.dropped += cut

    def _read(self, proc: subprocess.Popen) -> None:
        pending = ""
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            parts = re.split(r"[\r\n]", pending + chunk.decode("utf-8", errors="replace"))
            pending = parts.pop()
            for part in parts:
                if part.strip():
                    self._add(part.rstrip())
        if pending.strip():
            self._add(pending.rstrip())
        code = proc.wait()
        with self.lock:
            self.exit_code, self.finished = code, time.time()

    def stop(self) -> bool:
        with self.lock:
            if not self.running():
                return False
            self.stopped = True
            self.proc.terminate()
            return True

    def snapshot(self, since: int) -> dict:
        with self.lock:
            start = max(since - self.dropped, 0)
            return {
                "running": self.running(),
                "stage": self.stage,
                "command": self.command,
                "exit_code": self.exit_code,
                "stopped": self.stopped,
                "started": self.started,
                "finished": self.finished,
                "next": self.dropped + len(self.lines),
                "lines": self.lines[start:],
            }


JOB = Job()


def build_command(stage: str, opts: dict) -> list[str]:
    s = get_settings()
    if stage == "stage1":
        return ["-m", "tiktok_archive.parse_tiktok_export", "--input", str(export_json_path()),
                "--output", str(s.favorites_csv), "--failures", str(failures_csv_path())]
    if stage == "migrate":
        return ["-m", "tiktok_archive.migrate"]
    if stage == "load":
        return ["-m", "tiktok_archive.loader"]
    if stage == "process":
        limit, workers = int(opts.get("limit") or 0), int(opts.get("workers") or 4)
        if not 1 <= limit <= 100_000:
            raise ValueError("choose how many videos to process (1 or more)")
        if not 1 <= workers <= 8:
            raise ValueError("workers must be between 1 and 8")
        return ["-m", "tiktok_archive.process", "--limit", str(limit), "--workers", str(workers)]
    if stage == "topics":
        return ["-m", "tiktok_archive.topics"]
    if stage == "export":
        return ["-m", "tiktok_archive.export_obsidian"]
    raise ValueError(f"unknown stage {stage!r}")


# --- status and previews --------------------------------------------------------

def count_csv_rows(path: Path, *, header: bool) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as f:
        return max(sum(1 for _ in csv.reader(f)) - (1 if header else 0), 0)


def read_failed_links(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {row[0] for row in csv.reader(f) if row}


def db_state() -> dict:
    """Is the database reachable, is the schema there, and how many videos are in each state."""
    try:
        with db.connect(vector=False, timeout=3) as conn:
            try:
                counts = db.state_counts(conn)
                applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
                return {"ok": True, "migrated": True, "counts": counts,
                        "pending_migrations": len(migrate.pending_migrations(applied))}
            except Exception as e:  # relation "videos" does not exist
                conn.rollback()
                if "does not exist" in str(e):
                    return {"ok": True, "migrated": False, "counts": {}}
                raise
    except (Exception, SystemExit) as e:
        return {"ok": False, "migrated": False, "counts": {}, "error": first_line(e)}


def status() -> dict:
    s = get_settings()
    export = export_json_path()
    return {
        "db": db_state(),
        "files": {
            "export_json": {"path": str(export), "exists": export.exists()},
            "favorites_csv": {"path": str(s.favorites_csv), "rows": count_csv_rows(s.favorites_csv, header=True)},
            "vault": {"path": str(s.vault_path), "exists": s.vault_path.exists()},
            "media_dir": {"path": str(s.media_dir)},
        },
        "config": {
            "whisper_model": s.whisper_model, "whisper_device": s.whisper_device,
            "caption_langs": list(s.caption_langs), "embedding_model": s.embedding_model,
            "max_attempts": s.max_attempts,
        },
        "categories": list(CATEGORY_NAMES),
    }


def preview(stage: str, full: bool = False) -> dict:
    """Plain-language facts about what a stage would do right now, computed without doing it.

    full=True also does the slow part for the topics stage: working out the topics themselves."""
    s = get_settings()
    out = {"stage": stage, "summary": "", "facts": [], "will": [], "wont": [], "blocked": None}

    if stage == "stage1":
        src = export_json_path()
        if not src.exists():
            out["blocked"] = f"Your TikTok export file was not found at {src}."
            return out
        entries = parse_tiktok_export.find_favorite_entries(src)
        seen_links, _ = parse_tiktok_export.load_progress(s.favorites_csv)
        todo = [e for e in entries if e.get("Link") not in seen_links]
        failed = read_failed_links(failures_csv_path())
        retry = sum(1 for e in todo if e.get("Link") in failed)
        local = sum(1 for e in todo if parse_tiktok_export.id_from_link(e.get("Link")))
        online = len(todo) - local
        out["facts"] = [
            ["Saved videos in your export", len(entries)],
            ["Already resolved on an earlier run", len(entries) - len(todo)],
            ["Still to resolve", len(todo)],
        ]
        if retry:
            out["facts"].append(["Of those, failed before and will be tried again", retry])
        out["will"] = [
            f"Read the video id straight out of {local:,} links. No internet needed.",
            *([f"Look up {online:,} other links online, 10 at a time."] if online else []),
            f"Add one line per video (video id, username, address, date saved) to {s.favorites_csv.name}. Existing lines are kept. The username is filled in later, in step 3.",
            "Write links that can't be resolved to a separate file instead.",
        ]
        out["wont"] = ["Download any video.", "Touch the database.", "Change your export file."]
        out["summary"] = f"{len(todo):,} links to resolve"
        if not todo:
            out["blocked"] = "Nothing left to resolve."
        return out

    try:
        conn = db.connect(timeout=3)
    except (Exception, SystemExit) as e:
        out["blocked"] = f"The database isn't reachable ({first_line(e)}). Start Docker Desktop and the tiktoks-db-1 container."
        out["summary"] = "database not reachable"
        return out
    with conn:
        try:
            counts = db.state_counts(conn)
        except Exception:
            conn.rollback()
            out["blocked"] = "The database is running but has no tables yet. Apply the schema first (button above)."
            out["summary"] = "database has no tables yet"
            return out

        if stage == "load":
            from .. import loader
            try:
                rows = loader.read_favorites(s.favorites_csv)
            except SystemExit as e:
                out["blocked"] = str(e)
                return out
            known = db.existing_video_ids(conn)
            new = [r for r in rows if r["video_id"] not in known]
            out["facts"] = [
                [f"Videos listed in {s.favorites_csv.name}", len(rows)],
                ["Already in the database", len(rows) - len(new)],
                ["New, to be added", len(new)],
            ]
            out["will"] = [f"Add {len(new):,} rows to the database, each marked 'pending' (nothing has been read from the video yet)."]
            out["wont"] = ["Download anything.", "Change rows that are already in the database."]
            out["summary"] = f"{len(new):,} new to add"
            if not new:
                out["blocked"] = "Nothing new to add."

        elif stage == "process":
            q = db.queue_breakdown(conn, s.max_attempts)
            todo = q["pending"] + q["transcribed"] + q["retryable"]
            out["facts"] = [
                ["In the queue", todo],
                ["  never looked at yet", q["pending"]],
                ["  already have a transcript (just need labels)", q["transcribed"]],
                ["  failed before, will retry", q["retryable"]],
                ["Finished", counts.get("done", 0)],
                ["Gave up after repeated failures", q["gave_up"]],
                ["Deleted or private posts", counts.get("unavailable", 0)],
            ]
            out["will"] = [
                "For each video, newest saves first: ask TikTok for the post details and its caption file. The video itself is NOT downloaded.",
                "Only if the post has no captions: download the video, let Whisper (on your computer) listen to it, then delete the video right away.",
                "Pick a title from the caption's first line, tags from its hashtags, and 1-3 categories by comparing the text with your category definitions.",
                "Save all of that, plus a search fingerprint of the text, to the database.",
            ]
            out["wont"] = [
                "Use any paid service or API key.",
                "Keep downloaded videos (a video exists only while its own job runs).",
                "Write Obsidian notes (that is the next stage).",
                "Process more than the number you choose.",
            ]
            out["note"] = "The first run also loads the search model (a few seconds, about 2 GB of memory). Try a small number first, then look at the results."
            out["summary"] = f"{todo:,} in the queue"
            if not todo:
                out["blocked"] = "The queue is empty. Add videos to the database first, or everything is already done."

        elif stage == "topics":
            inputs = db.topic_inputs(conn)
            saved = len(db.topics_with_members(conn))
            out["facts"] = [
                ["Finished videos to group", len(inputs)],
                ["Topics saved right now", saved],
                ["How alike videos must be (0 to 1)", s.topic_similarity],
                ["Fewest videos in a topic", s.topic_min_size],
            ]
            out["will"] = [
                "Compare the fingerprints of all finished videos and group the ones that are close in meaning.",
                "Name each group after the hashtags that set it apart from the rest of your archive.",
                "Replace the topics saved before. Optional: skip this step and notes just have no topic.",
            ]
            out["wont"] = ["Use any online service.", "Change your notes. Step 5 writes the topic links and hub notes."]
            out["summary"] = f"{len(inputs):,} videos to group"
            if len(inputs) < 2 * s.topic_min_size:
                out["blocked"] = f"Needs at least {2 * s.topic_min_size} finished videos to find a topic; there are {len(inputs)}."
            elif full:
                found, ungrouped = topics.build_topics(inputs, similarity=s.topic_similarity, min_size=s.topic_min_size)
                out["items"] = [f"{t['name']}: {len(t['members'])} videos, e.g. " + " / ".join(x[:50] for x in t["titles"][:2]) for t in found[:15]]
                out["items_title"] = f"Would create {len(found)} topics ({len(inputs) - ungrouped} videos grouped, {ungrouped} left out)" + (", showing the 15 biggest" if len(found) > 15 else "")
            else:
                out["note"] = "Press \"Show the topics it would make\" to see them first. It can take a minute with thousands of videos."

        elif stage == "export":
            rows = export_obsidian.rows_for_export(conn)
            stats = export_obsidian.export_rows(rows, s.vault_path, dry_run=True)
            hubs = export_obsidian.export_topic_hubs(db.topics_with_members(conn), s.vault_path, dry_run=True)
            out["facts"] = [
                ["Finished videos", len(rows)],
                ["Notes to create or update", stats.written],
                ["Notes to move to another folder", stats.moved],
                ["Notes already up to date", stats.unchanged],
                ["Topic hub notes to write", hubs.hubs],
                ["Vault folder", str(s.vault_path)],
            ]
            out["will"] = [
                f"Write {stats.written:,} Markdown files into {s.vault_path}, in one folder per primary category.",
                "Only touch files named <video id>.md; your own notes in the same folder are left alone.",
            ]
            out["wont"] = ["Read anything back from Obsidian.", "Change the database."]
            out["summary"] = f"{stats.written + stats.moved:,} notes to write"
            if not (stats.written or stats.moved or hubs.hubs or hubs.removed):
                out["blocked"] = "No notes to write or update."
        else:
            raise ValueError(f"unknown stage {stage!r}")
    return out


# --- results --------------------------------------------------------------------

def videos(query: dict) -> dict:
    def one(name: str) -> str | None:
        return (query.get(name) or [""])[0].strip() or None

    state = one("state")
    if state and state not in STATES:
        raise ValueError("bad state")
    category = one("category")
    if category and category not in CATEGORY_NAMES:
        raise ValueError("bad category")
    status_ = one("status")
    if status_ and status_ not in ("unprocessed", "noted"):
        raise ValueError("bad status")
    limit = min(max(int(one("limit") or 50), 1), 200)
    offset = max(int(one("offset") or 0), 0)
    with db.connect(vector=False, timeout=3) as conn:
        rows, total = db.list_videos(
            conn, state=state, category=category, status=status_, text=one("q"), limit=limit, offset=offset,
        )
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


def search_videos(query: dict) -> dict:
    """Search by meaning, keywords or both. The first call loads the search model (about 10 s)."""
    def one(name: str) -> str | None:
        return (query.get(name) or [""])[0].strip() or None

    q = one("q")
    if not q or len(q) > 300:
        raise ValueError("type something to search for (up to 300 characters)")
    mode = one("mode") or "both"
    if mode not in search_module.MODES:
        raise ValueError("bad mode")
    category = one("category")
    if category and category not in CATEGORY_NAMES:
        raise ValueError("bad category")
    status_ = one("status")
    if status_ and status_ not in ("unprocessed", "noted"):
        raise ValueError("bad status")
    loaded_before = embed.is_loaded()
    started = time.time()
    rows = search_module.search(q, mode=mode, category=category, status=status_, k=min(int(one("limit") or 20), 50))
    return {"rows": rows, "seconds": round(time.time() - started, 1), "loaded_model": mode != "keywords" and not loaded_before,
            "min_similarity": get_settings().search_min_similarity}


def video_detail(video_id: str) -> dict:
    with db.connect(timeout=3) as conn:
        row = db.get_video(conn, video_id)
    row.pop("embedding", None)
    with db.connect(timeout=3) as conn:
        row["related"] = export_obsidian.rows_for_export(conn, [video_id])[0]["related"] if row["processing_state"] == "done" else []
    row["note"] = export_obsidian.render_note(row)
    row["note_path"] = str(export_obsidian.note_path(get_settings().vault_path, row)) if row.get("categories") else None
    return row


def mark(ids: list[str], noted: bool) -> dict:
    if not ids or not all(VIDEO_ID_RE.match(i) for i in ids):
        raise ValueError("bad video ids")
    with db.connect(timeout=3) as conn:
        updated = db.set_status(conn, ids, "noted" if noted else "unprocessed")
    stats = export_obsidian.export(updated) if updated else None
    return {"updated": updated, "vault": str(stats) if stats else None}


# --- http -----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "tiktok-archive-ui"
    token = ""
    port = 0

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        # Refuse other hostnames (DNS rebinding) and, for the API, anything without our per-run token.
        if self.headers.get("Host") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return False
        return secrets.compare_digest(self.headers.get("X-UI-Token", ""), self.token) if self.path.startswith("/api/") else True

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size > 100_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(size) or b"{}")

    def _handle(self, fn) -> None:
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        try:
            fn()
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": first_line(e)}, 500)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        def route() -> None:
            if url.path == "/":
                html = INDEX_HTML.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/status":
                return self._json(status())
            if url.path.startswith("/api/preview/"):
                return self._json(preview(url.path.rsplit("/", 1)[1], full=(query.get("full") or [""])[0] == "1"))
            if url.path == "/api/job":
                return self._json(JOB.snapshot(int((query.get("since") or ["0"])[0])))
            if url.path == "/api/search":
                return self._json(search_videos(query))
            if url.path == "/api/videos":
                return self._json(videos(query))
            if url.path.startswith("/api/video/"):
                video_id = url.path.rsplit("/", 1)[1]
                if not VIDEO_ID_RE.match(video_id):
                    raise ValueError("bad video id")
                return self._json(video_detail(video_id))
            self._json({"error": "not found"}, 404)

        self._handle(route)

    def do_POST(self) -> None:
        url = urlparse(self.path)

        def route() -> None:
            body = self._body()
            if url.path == "/api/run":
                stage = str(body.get("stage"))
                JOB.start(stage, build_command(stage, body))
                return self._json({"started": True})
            if url.path == "/api/stop":
                return self._json({"stopped": JOB.stop()})
            if url.path == "/api/mark":
                return self._json(mark([str(i) for i in body.get("ids") or []], bool(body.get("noted"))))
            self._json({"error": "not found"}, 404)

        self._handle(route)


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server bind a port that is already being served,
    # so two copies would answer the same address with different tokens. Fail instead.
    allow_reuse_address = False
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    args = parser.parse_args()

    Handler.token, Handler.port = secrets.token_urlsafe(24), args.port
    try:
        httpd = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        raise SystemExit(
            f"Can't use port {args.port} ({e}).\n"
            f"The page is probably already running: open http://127.0.0.1:{args.port}/ . "
            f"Otherwise start this on another port with --port {args.port + 1}."
        )
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Pipeline UI running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        JOB.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
