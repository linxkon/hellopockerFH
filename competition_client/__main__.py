import hashlib
import logging
import os
import signal
import sys
from .config import DriverLock, parse_config, read_key
from .decision_store import DecisionStore
from .driver import Driver
from .http_client import HTTPClient, RedactingFormatter, redact
from .strategy_worker import StrategyWorker
from .protocol import require
from .replay_store import ReplayJournal, attach_runtime_log
from .public_stream import PublicTableStream


class LazyWorker:
    def __init__(self, path):
        self.path, self.worker = path, None

    def warm(self):
        if self.worker is None:
            self.worker = StrategyWorker(self.path)

    def decide(self, state, timeout):
        self.warm()
        return self.worker.decide(state, timeout)

    def close(self):
        if self.worker:
            self.worker.close()


def main(argv=None):
    os.umask(0o077)
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    lock = store = worker = journal = file_handler = public_stream = None
    try:
        config, check = parse_config(argv)
        key = read_key(config.key_file)
        lock = DriverLock(key)
        store = DecisionStore(config.state_dir)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        require(store.meta("credential_fingerprint") in (None, fingerprint), "State directory belongs to different credentials")
        store.meta("credential_fingerprint", fingerprint)
        client = HTTPClient(config.base_url, key, config.http_timeout, config.max_clock_skew)
        if not check:
            journal = ReplayJournal(config)
            file_handler = attach_runtime_log(journal)
            logging.info("Replay recording started: %s", journal.path)
            if journal.source == "sohu_online":
                public_stream = PublicTableStream(journal, config.base_url, config.competition_id)
        worker = LazyWorker(config.strategy)
        driver = Driver(config, client, store, worker, journal=journal, public_stream=public_stream)
        signal.signal(signal.SIGINT, driver.request_stop)
        signal.signal(signal.SIGTERM, driver.request_stop)
        leave = store.meta("leave")
        if check:
            competition = driver.discover()
            worker.warm()
            logging.info("Read-only check passed; Competition status=%s summary=%s", competition["status"], competition["summary"])
            return 0
        if not (leave and leave["status"] == "pending") and not driver.stop.is_set():
            # Start worker before Join so deployment problems cannot take a seat.
            worker.warm()
        driver.run()
        return 0
    except Exception as exc:
        # Avoid traceback/cause leaking transport headers or strategy state.
        logging.error("Stopped: %s", redact(str(exc)))
        if journal:
            journal.event("session_error", {"error": redact(str(exc))})
        return 1
    finally:
        if public_stream:
            public_stream.close()
        if journal:
            journal.event("session_end", {"recording_failed": journal.failed})
            journal.close()
        if worker:
            worker.close()
        if store:
            store.close()
        if lock:
            lock.close()
        if file_handler:
            logging.getLogger().removeHandler(file_handler)
            file_handler.flush()
            file_handler.stream.close()
            file_handler.close()


if __name__ == "__main__":
    sys.exit(main())
