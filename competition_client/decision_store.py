"""Durable action bodies and leave intent. No credentials or observations."""
import json
import os
import sqlite3
from pathlib import Path
from .protocol import require


class DecisionStore:
    def __init__(self, directory):
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(directory.stat().st_mode & 0o077 == 0, "state-dir must have mode 0700")
        self.path = directory / "decisions.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path)
        # WAL retains FULL durability while avoiding the rollback journal's
        # extra disk syncs on every decision and observation metadata update.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS actions (competition TEXT, table_id TEXT, request TEXT, body TEXT, status TEXT, reason TEXT, PRIMARY KEY (competition, table_id, request))")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS revisions (competition TEXT, table_id TEXT, request TEXT, body TEXT, reason TEXT)")
        self.db.commit()

    def get(self, key):
        row = self.db.execute("SELECT body, status FROM actions WHERE competition=? AND table_id=? AND request=?", key).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def save(self, body):
        key = (body["competitionId"], body["tableId"], body["actionRequestId"])
        old = self.get(key)
        require(old is None or old[0] == body, "Attempt to change durable Action")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?,?)", (*key, json.dumps(body, sort_keys=True), "pending", None))

    def mark(self, key, status, reason=None):
        with self.db:
            self.db.execute("UPDATE actions SET status=?, reason=? WHERE competition=? AND table_id=? AND request=?", (status, reason, *key))

    def revise_rejected(self, key, body, reason):
        old = self.get(key)
        require(old is not None and old[1] == "rejected", "Only explicitly rejected Actions can change")
        with self.db:
            self.db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (*key, json.dumps(old[0]), reason))
            self.db.execute("UPDATE actions SET body=?, status='pending', reason=? WHERE competition=? AND table_id=? AND request=?", (json.dumps(body, sort_keys=True), reason, *key))

    def archive_other_requests(self, current):
        with self.db:
            rows = self.db.execute("SELECT competition, table_id, request FROM actions WHERE status IN ('pending','rejected')").fetchall()
            for key in rows:
                if key != current:
                    self.db.execute("UPDATE actions SET status='archived' WHERE competition=? AND table_id=? AND request=?", key)

    def meta(self, key, value=...):
        if value is ...:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        self.meta_many({key: value})

    def meta_many(self, values):
        changed = []
        for key, value in values.items():
            encoded = json.dumps(value, sort_keys=True)
            old = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if old is None or old[0] != encoded:
                changed.append((key, encoded))
        if changed:
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", changed)

    def close(self):
        self.db.close()
