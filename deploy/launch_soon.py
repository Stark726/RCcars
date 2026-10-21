"""Exit 0 when a watched, not-yet-available launch is due soon, so the run keeps going.

Covers 15 minutes ahead (the 10-minute reminder and the 10-second launch checks) and
10 minutes after launch (waiting for "Available now"), so a run never ends mid-launch.
"""
import sqlite3
import sys
import time
from datetime import datetime

AHEAD, AFTER = 15 * 60, 10 * 60


def launch_soon(path="state/state.sqlite3", now=None):
    now = time.time() if now is None else now
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT w.launch_at FROM launch_watches w JOIN products p ON p.id = w.product_id "
                          "WHERE w.enabled = 1 AND p.purchase_state = 'pending'").fetchall()
    finally:
        db.close()
    for (launch_at,) in rows:
        at = datetime.fromisoformat(launch_at.replace("Z", "+00:00")).timestamp()
        if now - AFTER <= at <= now + AHEAD:
            print(f"Launch at {launch_at} is close; extending this run")
            return True
    return False


if __name__ == "__main__":
    try:
        sys.exit(0 if launch_soon() else 1)
    except Exception as exc:  # When unsure, keep watching rather than leave a gap.
        print(f"Launch check failed ({type(exc).__name__}); extending this run")
        sys.exit(0)
