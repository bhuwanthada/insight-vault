"""JSON-file storage. Data lives on the local machine; writes are atomic."""
import json
import os
import tempfile
import threading


def empty_db():
    return {"notes": [], "highlights": [], "insights": [], "tickets": [], "reports": [], "usage": {}, "audit": []}


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()  # reentrant: routes hold it across load -> modify -> save

    def load(self):
        with self.lock:  # on Windows os.replace fails if a reader has the file open, so reads share the write lock
            try:
                with open(self.path, encoding="utf-8") as handle:
                    return {**empty_db(), **json.load(handle)}  # tolerate files written by older versions
            except (FileNotFoundError, json.JSONDecodeError):
                return empty_db()

    def save(self, db):
        with self.lock:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(db, handle, indent=2)
            os.replace(tmp, self.path)
