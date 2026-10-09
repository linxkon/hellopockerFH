"""Passive public table SSE capture, independent of the competition driver.

The official website exposes settled hands here even when Competition Observe
already points at the next hand. No credentials, chat, or strategy input flow
through this collector. Each thread uses its own telemetry DB connection.
"""
import json
import logging
from http.client import HTTPException
import sqlite3
import threading
from urllib.parse import quote
from urllib.request import Request, build_opener

from .http_client import NoRedirect
from .replay_store import ReplayJournal, encoded

LOG = logging.getLogger(__name__)
MAX_FRAME_BYTES = 8 * 1024 * 1024


def table_snapshots(response):
    event, data, size = None, [], 0
    while True:
        line = response.readline(MAX_FRAME_BYTES + 1)
        if not line:
            return
        size += len(line)
        if size > MAX_FRAME_BYTES:
            raise ValueError("Public stream frame too large")
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if event == "snapshot" and data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid public stream snapshot")
                table = payload.get("table")
                if isinstance(table, dict):
                    # Drop the top-level messages/feed before anything is stored.
                    yield table
            event, data, size = None, [], 0
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


class PublicTableStream:
    def __init__(self, journal, base_url, competition_id):
        self.journal = journal
        self.base_url, self.competition_id = base_url.rstrip("/"), competition_id
        self.table_id = self.stop = self.thread = None

    def follow(self, table_id, hero_agent_id):
        if table_id == self.table_id:
            return
        self.close()
        self.table_id = table_id
        if not table_id:
            return
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._capture,
            args=(self.stop, table_id, hero_agent_id), name="public-table-replay", daemon=True)
        self.thread.start()

    def _capture(self, stop, table_id, hero_agent_id):
        url = self.base_url + "/api/tables/" + quote(table_id, safe="") + "/stream"
        db = None
        previous = None
        try:
            db = sqlite3.connect(self.journal.path, timeout=2)
            db.execute("PRAGMA synchronous=NORMAL")

            def write(kind, body):
                with db:
                    db.execute("INSERT INTO events VALUES (NULL,?,?,?,?)",
                        (self.journal.run_id, ReplayJournal.now(), kind, encoded(body)))

            opener = build_opener(NoRedirect())
            while not stop.is_set():
                try:
                    # This is the website's public stream. Never send the Agent key.
                    request = Request(url, headers={"Accept": "text/event-stream"})
                    with opener.open(request, timeout=15) as response:
                        if "text/event-stream" not in response.headers.get("Content-Type", ""):
                            raise ValueError("Invalid public stream response")
                        if stop.is_set():
                            return
                        write("public_stream_status", {"table_id": table_id, "status": "connected", "source_url": url})
                        LOG.info("Public table replay stream connected table=%s", table_id)
                        for table in table_snapshots(response):
                            if stop.is_set():
                                return
                            if table.get("id") != table_id:
                                raise ValueError("Public stream table changed")
                            payload = encoded(table)
                            if payload == previous:
                                continue
                            hand = table.get("hand") or {}
                            if not isinstance(hand, dict):
                                raise ValueError("Invalid public stream hand")
                            write("public_table_snapshot", {"competition_id": self.competition_id,
                                "table_id": table_id, "hand_id": hand.get("id"),
                                "hero_agent_id": hero_agent_id, "visibility": "public_table_stream",
                                "source_url": url, "table": table})
                            previous = payload
                            if hand.get("result") is not None:
                                LOG.info("Public settlement recorded hand=%s table=%s", hand.get("id"), table_id)
                    if stop.is_set():
                        return
                    raise EOFError("Public stream closed")
                except (OSError, ValueError, EOFError, HTTPException, sqlite3.Error):
                    if stop.is_set():
                        return
                    LOG.warning("Public table replay stream interrupted; reconnecting table=%s", table_id)
                    try:
                        write("public_stream_status", {"table_id": table_id, "status": "reconnecting"})
                    except sqlite3.Error:
                        pass
                    stop.wait(2)
        except (OSError, sqlite3.Error):
            LOG.error("Public table replay stream could not record table=%s", table_id)
        finally:
            if db:
                db.close()

    def close(self):
        if self.stop:
            self.stop.set()
        # A blocked network read must never delay Leave or the next Action.
        self.table_id = None
