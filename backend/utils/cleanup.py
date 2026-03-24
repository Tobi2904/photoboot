"""
cleanup.py — Background housekeeping for temp session folders.

Runs on a configurable interval and removes session directories
in storage/temp_sessions/ that are older than a TTL threshold.
"""

import shutil
import time
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMP_SESSIONS_DIR = BASE_DIR / "storage" / "temp_sessions"


def _folder_age_minutes(folder: Path) -> float:
    """Return the age of a folder in minutes based on its modification time."""
    return (time.time() - folder.stat().st_mtime) / 60.0


def cleanup_old_sessions(ttl_minutes: int = 30) -> int:
    """
    Delete session folders older than *ttl_minutes*.

    Returns the number of folders removed.
    """
    if not TEMP_SESSIONS_DIR.exists():
        return 0

    removed = 0
    for entry in TEMP_SESSIONS_DIR.iterdir():
        if not entry.is_dir():
            continue
        age = _folder_age_minutes(entry)
        if age > ttl_minutes:
            try:
                shutil.rmtree(entry)
                removed += 1
                logger.info(
                    "Cleaned up session %s (age=%.1f min)", entry.name, age
                )
            except OSError as exc:
                logger.warning("Failed to delete %s: %s", entry.name, exc)

    if removed:
        logger.info("Cleanup complete — removed %d session(s).", removed)
    return removed


def start_cleanup_scheduler(
    interval_minutes: int = 10,
    ttl_minutes: int = 30,
) -> threading.Thread:
    """
    Launch a daemon thread that runs cleanup on a fixed interval.

    The thread is marked as a daemon so it won't block application shutdown.
    """

    def _loop() -> None:
        logger.info(
            "Cleanup scheduler started (interval=%d min, ttl=%d min).",
            interval_minutes,
            ttl_minutes,
        )
        while True:
            time.sleep(interval_minutes * 60)
            try:
                cleanup_old_sessions(ttl_minutes)
            except Exception:
                logger.exception("Unexpected error in cleanup loop")

    thread = threading.Thread(target=_loop, daemon=True, name="cleanup-scheduler")
    thread.start()
    return thread
