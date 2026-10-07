"""Daily copy of the SQLite database: client settings, goals, exclusions and
portfolios live only there. The online backup API is safe while the bot writes."""

import logging
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)


def backup_sqlite(url, keep=14, today=None):
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite") or parsed.database in (None, "", ":memory:"):
        return None
    source = Path(parsed.database)
    if not source.exists():
        return None
    folder = source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{source.stem}_{(today or date.today()).isoformat()}.db"
    # sqlite3's own context manager commits but does not close the files.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    copies = sorted(folder.glob(f"{source.stem}_*.db"))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    logger.info("Database backup saved path=%s size=%s", target, target.stat().st_size)
    return target
