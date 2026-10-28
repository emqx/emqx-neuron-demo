"""Durable order deduplication and restart outcomes for the simulator."""
import sqlite3
import threading
import time


class Journal:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, state TEXT, updated REAL)")
        self.db.commit()

    def claim(self, order_id):
        with self.lock:
            cursor = self.db.execute("INSERT OR IGNORE INTO orders VALUES (?, 'pending', ?)", (order_id, time.time()))
            self.db.commit()
            return cursor.rowcount == 1

    def state(self, order_id):
        with self.lock:
            row = self.db.execute("SELECT state FROM orders WHERE id=?", (order_id,)).fetchone()
            return row[0] if row else None

    def set(self, order_id, state):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?)", (order_id, state, time.time()))
            self.db.commit()

    def interrupted(self):
        with self.lock:
            ids = [row[0] for row in self.db.execute("SELECT id FROM orders WHERE state IN ('pending','running')")]
            self.db.execute("UPDATE orders SET state='failed', updated=? WHERE state IN ('pending','running')", (time.time(),))
            self.db.commit()
            return ids

    def prune(self):
        with self.lock:
            self.db.execute("DELETE FROM orders WHERE state IN ('completed','failed') AND updated < ?", (time.time() - 86400,))
            self.db.commit()
