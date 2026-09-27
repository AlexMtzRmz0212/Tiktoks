"""Apply unapplied migrations/*.sql in filename order, each in its own transaction.

Usage:
    python -m tiktok_archive.migrate            # apply pending migrations
    python -m tiktok_archive.migrate --status   # list applied / pending
"""

import argparse

from . import db
from .config import MIGRATIONS_DIR, utf8_console


def _ensure_table(conn) -> set[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    # End the implicit transaction so each conn.transaction() below is a real, separate one.
    conn.commit()
    return applied


def pending_migrations(applied: set[str]) -> list:
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.stem not in applied]


def apply_migrations(verbose: bool = True) -> list[str]:
    """Returns the versions applied by this call."""
    done = []
    # No pgvector registration here: the first migration is what creates the extension.
    with db.connect(vector=False) as conn:
        for path in pending_migrations(_ensure_table(conn)):
            with conn.transaction():
                # No parameters, so psycopg sends this as one simple query (multi-statement is fine).
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            done.append(path.stem)
            if verbose:
                print(f"applied {path.name}")
    return done


def main() -> None:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="show applied and pending migrations, change nothing")
    args = parser.parse_args()

    if args.status:
        with db.connect(vector=False) as conn:
            applied = _ensure_table(conn)
        for v in sorted(applied):
            print(f"applied  {v}")
        for p in pending_migrations(applied):
            print(f"pending  {p.stem}")
        return

    if not apply_migrations():
        print("database is up to date")


if __name__ == "__main__":
    main()
