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
QUERIES = (
    "SELECT key, value FROM settings ORDER BY key",
    "SELECT id, baseline, alert_state, alert_id, purchase_state, detail, revision, edited_revision "
    "FROM products ORDER BY id",
    "SELECT handle, launch_at, product_id, enabled, phase, source FROM launch_watches ORDER BY handle",
)


def digest(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for query in QUERIES:
            for row in db.execute(query):
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
    sys.exit(main())
