"""Durable baseline, alert outbox, launch watches and per-product availability state."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from .models import Product
from .config import launch_time
from .discovery import discovery_priority, discovery_hint


class State:
    @classmethod
    def open_readonly(cls, path):
        """Inspect existing state without creating tables or changing history."""
        state = cls.__new__(cls)
        state.path = Path(path)
        if not state.path.is_file():
            raise FileNotFoundError('No state database yet. Run scan to establish a baseline.')
        state.db = sqlite3.connect(state.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        state.db.row_factory = sqlite3.Row
        return state

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, baseline INTEGER NOT NULL,
              alert_state TEXT NOT NULL, alert_id INTEGER,
              purchase_state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
              revision INTEGER NOT NULL DEFAULT 0, edited_revision INTEGER NOT NULL DEFAULT -1,
              first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS launch_watches (
              handle TEXT PRIMARY KEY, launch_at TEXT NOT NULL,
              product_id TEXT, enabled INTEGER NOT NULL DEFAULT 1,
              check_after REAL NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual');
            CREATE TABLE IF NOT EXISTS inspections (
              product_id TEXT PRIMARY KEY, status TEXT NOT NULL,
              next_check REAL NOT NULL DEFAULT 0);
        ''')
        if 'source' not in {row[1] for row in self.db.execute('PRAGMA table_info(launch_watches)')}:
            self.db.execute("ALTER TABLE launch_watches ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if 'reminded_for' not in {row[1] for row in self.db.execute('PRAGMA table_info(launch_watches)')}:
            # Launch time a reminder was sent for; a rescheduled launch gets a new reminder.
            self.db.execute("ALTER TABLE launch_watches ADD COLUMN reminded_for TEXT")
        self.db.commit()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def has_baseline(self):
        return self.db.execute("SELECT 1 FROM settings WHERE key='scope'").fetchone() is not None

    def validate_scope(self, scope):
        saved = self.db.execute("SELECT value FROM settings WHERE key='scope'").fetchone()
        if saved and saved['value'] != scope:
            raise ValueError('Collection scope changed: use a new state_path to baseline the new scope')

    def recover(self):
        # Call only while owning the process lock: a crash might have occurred
        # after the remote side acted but before our local commit.
        with self.transaction():
            self.db.execute("UPDATE products SET purchase_state='uncertain', "
                            "detail='Process stopped during checkout; inspect order history before any manual action', "
                            "revision=revision+1 WHERE purchase_state='in_progress'")
            self.db.execute("UPDATE products SET alert_state='uncertain' WHERE alert_state='sending'")

    def observe(self, products, scope):
        """Atomic: every configured source succeeds before baseline/diff is stored."""
        if not products:
            raise ValueError("Cannot baseline an empty snapshot")
        with self.transaction():
            setting = self.db.execute("SELECT value FROM settings WHERE key='scope'").fetchone()
            if setting and setting[0] != scope:
                raise ValueError("Collection scope changed: use a new state_path to baseline the new scope")
            first = setting is None
            if first:
                self.db.execute("INSERT INTO settings VALUES ('scope', ?)", (scope,))
            new = 0
            for p in products:
                payload = json.dumps(p.to_json(), ensure_ascii=False)
                row = self.row(p.id)
                if row:
                    # Preserve verified detail prices until the next refresh.
                    old = Product.from_json(json.loads(row['payload']))
                    p.launch_check, p.launch_label, p.ui_buyable = old.launch_check, old.launch_label, old.ui_buyable
                    payload = json.dumps(p.to_json(), ensure_ascii=False)
                    if old.launch_at and not p.launch_at:
                        p.launch_at = old.launch_at
                        payload = json.dumps(p.to_json(), ensure_ascii=False)
                    if p.currency is None and old.currency:
                        p.currency = old.currency
                        price_map = {v.id: v.price_cents for v in old.variants}
                        for v in p.variants:
                            v.price_cents = price_map.get(v.id)
                        payload = json.dumps(p.to_json(), ensure_ascii=False)
                    self.db.execute("UPDATE products SET payload=?, last_seen=CURRENT_TIMESTAMP WHERE id=?", (payload, p.id))
                else:
                    base = first or not p.eligible()
                    self.db.execute("INSERT INTO products (id,payload,baseline,alert_state,purchase_state) VALUES (?,?,?,?,?)",
                                    (p.id, payload, int(base), 'ignored' if base else 'pending',
                                     'baseline' if base else 'pending'))
                    if not base:
                        new += 1
        return {"baseline": first, "new_products": new, "observed": len(products)}

    def row(self, pid):
        return self.db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

    def product(self, pid):
        return Product.from_json(json.loads(self.row(pid)['payload']))

    def active(self):
        return self.db.execute("SELECT * FROM products WHERE baseline=0 ORDER BY first_seen,id").fetchall()

    def save_product(self, product):
        with self.db:
            self.db.execute("UPDATE products SET payload=? WHERE id=?", (json.dumps(product.to_json(), ensure_ascii=False), product.id))

    def claim_alert(self, pid):
        with self.transaction():
            return self.db.execute("UPDATE products SET alert_state='sending' WHERE id=? AND alert_state='pending'", (pid,)).rowcount == 1

    def finish_alert(self, pid, state, message_id=None):
        with self.db:
            self.db.execute("UPDATE products SET alert_state=?,alert_id=? WHERE id=?", (state, message_id, pid))

    def claim_purchase(self, pid):
        with self.transaction():
            return self.db.execute("UPDATE products SET purchase_state='in_progress' WHERE id=? AND purchase_state='pending' "
                                   "AND (baseline=0 OR EXISTS (SELECT 1 FROM launch_watches w WHERE w.product_id=products.id AND w.enabled=1))", (pid,)).rowcount == 1

    def finish_purchase(self, pid, status, detail):
        with self.db:
            self.db.execute("UPDATE products SET purchase_state=?,detail=?,revision=revision+1 WHERE id=?", (status, detail, pid))

    def edit_done(self, pid, revision):
        with self.db:
            self.db.execute("UPDATE products SET edited_revision=? WHERE id=?", (revision, pid))

    def rows_for_status(self):
        return [dict(r) for r in self.db.execute("SELECT id,baseline,alert_state,purchase_state,detail FROM products ORDER BY first_seen,id")]

    def configure_watches(self, watches):
        with self.transaction():
            self.db.execute("UPDATE launch_watches SET enabled=0 WHERE source='manual'")
            for w in watches:
                at = launch_time(w['launch_at']).isoformat()
                previous = self.db.execute('SELECT * FROM launch_watches WHERE handle=?', (w['handle'],)).fetchone()
                if previous:
                    changed = at != previous['launch_at'] or not previous['enabled']
                    self.db.execute("UPDATE launch_watches SET enabled=1,source='manual',launch_at=?,check_after=?,phase=? WHERE handle=?",
                                    (at, 0 if changed else previous['check_after'], '' if changed else previous['phase'], w['handle']))
                else:
                    self.db.execute('INSERT INTO launch_watches (handle,launch_at) VALUES (?,?)', (w['handle'],at))

    def watches(self):
        return self.db.execute('SELECT * FROM launch_watches WHERE enabled=1 ORDER BY launch_at').fetchall()

    def watched_handle(self, handle):
        # Includes disabled watches: removing a watch must not release a pending
        # timed product through the ordinary newly-added-product path.
        return self.db.execute('SELECT 1 FROM launch_watches WHERE handle=?', (handle,)).fetchone() is not None

    def bind_watch(self, watch, product):
        with self.transaction():
            current = self.db.execute('SELECT * FROM launch_watches WHERE handle=?', (watch['handle'],)).fetchone()
            if current['product_id'] and current['product_id'] != product.id:
                raise ValueError('Watched handle resolved to a different product ID; review it manually')
            self.db.execute('UPDATE launch_watches SET product_id=? WHERE handle=?', (product.id,watch['handle']))
            row = self.row(product.id)
            payload = json.dumps(product.to_json(), ensure_ascii=False)
            if row:
                self.db.execute("UPDATE products SET payload=?,purchase_state=CASE WHEN purchase_state='baseline' THEN 'pending' ELSE purchase_state END, "
                                "alert_state=CASE WHEN alert_state='ignored' THEN 'pending' ELSE alert_state END WHERE id=?", (payload,product.id))
            else:
                self.db.execute("INSERT INTO products (id,payload,baseline,alert_state,purchase_state) VALUES (?,?,1,'pending','pending')", (product.id,payload))

    def update_watch(self, handle, check_after, phase=None):
        with self.db:
            if phase is None:
                self.db.execute('UPDATE launch_watches SET check_after=? WHERE handle=?', (check_after,handle))
            else:
                self.db.execute('UPDATE launch_watches SET check_after=?,phase=? WHERE handle=?', (check_after,phase,handle))

    def bump_revision(self, pid):
        with self.db:
            self.db.execute('UPDATE products SET revision=revision+1 WHERE id=?', (pid,))

    def auto_watch(self, product, now):
        """Only a rendered, timezone-resolved future launch can arm a baseline row."""
        if product.launch_check != 'scheduled' or not product.launch_at or launch_time(product.launch_at).timestamp() <= now:
            return False
        with self.transaction():
            old = self.db.execute('SELECT * FROM launch_watches WHERE handle=?', (product.handle,)).fetchone()
            if old and old['product_id'] and old['product_id'] != product.id:
                return False
            if old and old['source']=='manual':
                return False
            self.db.execute("INSERT INTO launch_watches (handle,launch_at,product_id,source) VALUES (?,?,?,'auto') "
                            "ON CONFLICT(handle) DO UPDATE SET launch_at=excluded.launch_at,enabled=1,product_id=excluded.product_id",
                            (product.handle,product.launch_at,product.id))
            self.db.execute("UPDATE products SET purchase_state=CASE WHEN purchase_state='baseline' THEN 'pending' ELSE purchase_state END, "
                            "alert_state=CASE WHEN alert_state='ignored' THEN 'pending' ELSE alert_state END WHERE id=?", (product.id,))
        return True

    def note_inspection(self, pid, status, next_check):
        with self.db:
            self.db.execute('INSERT INTO inspections VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET status=excluded.status,next_check=excluded.next_check',
                            (pid,status,next_check))

    def audit_queue(self, now):
        rows=self.db.execute("SELECT p.payload, i.product_id AS inspected FROM products p "
                             "LEFT JOIN inspections i ON i.product_id=p.id "
                             "WHERE p.baseline=1 AND p.purchase_state='baseline' AND (i.product_id IS NULL OR i.next_check<=?)", (now,))
        watched = {row[0] for row in self.db.execute('SELECT handle FROM launch_watches')}
        queued = []
        for row in rows:
            p=Product.from_json(json.loads(row['payload']))
            if p.eligible() and p.handle not in watched:
                queued.append(((0 if row['inspected'] is None else 1, *discovery_priority(p)), p))
        return [p for _, p in sorted(queued, key=lambda item: item[0])]

    def next_audit_product(self, now):
        queue = self.audit_queue(now)
        return queue[0] if queue else None

    def product_for_handle(self, handle):
        for row in self.db.execute('SELECT payload FROM products'):
            p = Product.from_json(json.loads(row['payload']))
            if p.handle == handle:
                return p
        return None

    def product_status(self, handle, now):
        p = self.product_for_handle(handle)
        if p is None:
            return {'read_only': True, 'found_in_catalogue': False}
        row = self.row(p.id)
        inspection = self.db.execute('SELECT * FROM inspections WHERE product_id=?', (p.id,)).fetchone()
        watch = self.db.execute('SELECT * FROM launch_watches WHERE handle=?', (handle,)).fetchone()
        position = next((i for i, item in enumerate(self.audit_queue(now), 1) if item.id == p.id), None)
        return {'read_only': True, 'found_in_catalogue': True, 'title': p.title,
                'eligible': p.eligible(), 'baseline': bool(row['baseline']),
                'purchase_state': row['purchase_state'], 'alert_state': row['alert_state'],
                'inspection_status': inspection['status'] if inspection else None,
                'inspection_next_check': inspection['next_check'] if inspection else None,
                'launch_watch': dict(watch) if watch else None,
                'queue_position': position, 'priority': discovery_hint(p)}

    def get_setting(self, key):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def mark_reminded(self, handle, launch_at):
        with self.db:
            self.db.execute('UPDATE launch_watches SET reminded_for=? WHERE handle=?', (launch_at, handle))

    def rescope(self, products, scope):
        """Switch to a new catalogue scope without alerting what is already listed.

        Products not saved yet become baseline (no alert). Existing rows, alert IDs
        and watches are kept, so earlier history and pending launches carry over.
        """
        if not products:
            raise ValueError('Cannot rescope from an empty snapshot')
        added = 0
        with self.transaction():
            for p in products:
                if self.row(p.id) is None:
                    self.db.execute("INSERT INTO products (id,payload,baseline,alert_state,purchase_state) VALUES (?,?,1,'ignored','baseline')",
                                    (p.id, json.dumps(p.to_json(), ensure_ascii=False)))
                    added += 1
            self.db.execute("INSERT INTO settings VALUES ('scope', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (scope,))
        return added

    def daily_stats(self):
        new = [(p.title, p.url) for p in (Product.from_json(json.loads(r[0])) for r in self.db.execute(
            "SELECT payload FROM products WHERE baseline=0 AND first_seen >= datetime('now','-1 day') ORDER BY first_seen"))]
        listed = self.db.execute("SELECT count(*) FROM products WHERE last_seen >= datetime('now','-1 day')").fetchone()[0]
        waiting = self.db.execute("SELECT count(*) FROM products WHERE purchase_state='pending'").fetchone()[0]
        return {'listed': listed, 'new': new, 'waiting': waiting}

    def update_auto_time(self, handle, at):
        with self.db:
            self.db.execute("UPDATE launch_watches SET launch_at=? WHERE handle=? AND source='auto'",(at,handle))

    def audit_unchecked(self):
        rows=self.db.execute("SELECT p.payload FROM products p LEFT JOIN inspections i ON i.product_id=p.id "
                             "WHERE p.baseline=1 AND p.purchase_state='baseline' AND i.product_id IS NULL")
        watched = {row[0] for row in self.db.execute('SELECT handle FROM launch_watches')}
        products = (Product.from_json(json.loads(row['payload'])) for row in rows)
        return sum(1 for p in products if p.eligible() and p.handle not in watched)
