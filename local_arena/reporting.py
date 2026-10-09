"""Private admin reports and reproducible full hand logs."""
import json
import os
from pathlib import Path


def private_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Reporter:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.directory / "report.json").exists() or (self.directory / "hands.jsonl").exists():
            raise ValueError("This run already has results; create a new run instead of overwriting it")
        fd = os.open(self.directory / "hands.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.log = os.fdopen(fd, "w", encoding="utf-8")

    def hand(self, record):
        self.log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.log.flush()
        os.fsync(self.log.fileno())

    def report(self, report):
        private_json(self.directory / "report.json", report)

    def close(self):
        self.log.close()
