#!/usr/bin/env bash
# Commit state/state.sqlite3 when alerts, watches or products changed (see state_changed.py).
set -u
python -c "import sqlite3; db = sqlite3.connect('state/state.sqlite3'); db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.close()"
if ! python deploy/state_changed.py; then exit 0; fi
git add state/state.sqlite3
if git diff --cached --quiet; then echo "No state change"; exit 0; fi
git commit -q -m "Update watch state [skip ci]"
# A code push during this run makes the plain push fail; rebase onto it (code and state
# never touch the same file) and push again, so the run's state is not lost.
if ! git push -q; then
  git pull -q --rebase origin main && git push -q || echo "State push failed; it is retried after the next round"
fi