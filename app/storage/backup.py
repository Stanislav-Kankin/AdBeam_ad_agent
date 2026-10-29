"""Daily copy of the SQLite database: client settings, goals, exclusions and
portfolios live only there. VACUUM INTO writes a compact copy while the bot runs."""

import logging
import shutil
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)


def sqlite_file(url):
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite") or parsed.database in (None, "", ":memory:"):
        return None
    source = Path(parsed.database)
    return source if source.exists() else None


def page_usage(conn):
    """Bytes held by live data and by free pages a VACUUM would return."""
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return (pages - free) * size, free * size


def backup_sqlite(url, keep=7, today=None):
    source = sqlite_file(url)
    if source is None:
        return None
    folder = source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    # Copies cut short by a full disk left empty files and journals behind.
    for leftover in [*folder.glob("*.partial"), *folder.glob("*.db-journal")]:
        leftover.unlink(missing_ok=True)
    for copy in folder.glob(f"{source.stem}_*.db"):
        if copy.stat().st_size == 0:
            copy.unlink(missing_ok=True)
    target = folder / f"{source.stem}_{(today or date.today()).isoformat()}.db"
    partial = target.with_name(target.name + ".partial")
    with closing(sqlite3.connect(source, timeout=60)) as conn:
        live, _ = page_usage(conn)
        free = shutil.disk_usage(folder).free
        # A copy must never be what fills the disk under the live database.
        if free < 2 * live:
            logger.error("Database backup skipped: free=%s needed=%s", free, 2 * live)
            return None
        try:
            conn.execute("VACUUM INTO ?", (str(partial),))
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    partial.replace(target)
    copies = sorted(folder.glob(f"{source.stem}_*.db"))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    logger.info("Database backup saved path=%s size=%s", target, target.stat().st_size)
    return target


def compact_sqlite(url, min_free_share=0.25):
    """Return space freed by deleted rows to the disk: SQLite keeps the file size."""
    source = sqlite_file(url)
    if source is None:
        return None
    with closing(sqlite3.connect(source, timeout=60)) as conn:
        live, free = page_usage(conn)
        if free < min_free_share * (live + free):
            return None
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = source.stat().st_size
    logger.info("Database compacted freed=%s size=%s", live + free - after, after)
    return after
