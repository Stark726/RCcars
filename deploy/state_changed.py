"""Exit 0 when the saved state changed in a way worth committing, 1 otherwise.

Every scan rewrites timestamps (last_seen, next check times), so the database file
always differs. Committing that every five minutes would bloat the repository, so
only alert/watch/product changes count; timing-only changes are saved every 6 hours.
"""
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = "state/state.sqlite3"
REFRESH_SECONDS = 6 * 3600
# Columns that matter; timing columns (last_seen, check_after, next_check) are left out.
TABLES = (
    ("settings", ("key", "value"), "key"),
    ("products", ("id", "baseline", "alert_state", "alert_id", "purchase_state", "detail",
                  "revision", "edited_revision"), "id"),
    ("launch_watches", ("handle", "launch_at", "product_id", "enabled", "phase", "source", "reminded_for"), "handle"),
)


def digest(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for table, wanted, order in TABLES:
            # An older committed database may lack newer columns; a missing column reads as NULL.
            present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            columns = ", ".join(c if c in present else "NULL" for c in wanted)
            for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY {order}"):
                h.update(repr(tuple(row)).encode())
        return h.hexdigest()
    finally:
        db.close()


def main():
    try:
        committed = subprocess.run(["git", "show", f"HEAD:{STATE}"], capture_output=True, check=True).stdout
    except subprocess.CalledProcessError:
        return 0  # Not committed yet.
    with tempfile.TemporaryDirectory() as folder:
        previous = Path(folder) / "previous.sqlite3"
        previous.write_bytes(committed)
        if digest(previous) != digest(STATE):
            print("Alert, watch or product state changed")
            return 0
    last = int(subprocess.run(["git", "log", "-1", "--format=%ct", "--", STATE],
                              capture_output=True, text=True, check=True).stdout.strip() or 0)
    if time.time() - last >= REFRESH_SECONDS:
        print("Periodic save of timing-only changes")
        return 0
    print("Only timestamps changed; not committing")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # Never skip a save because this check failed.
        print(f"State check failed ({type(exc).__name__}: {exc}); committing to be safe")
        sys.exit(0)
