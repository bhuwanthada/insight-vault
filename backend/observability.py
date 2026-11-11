"""Structured logging and Prometheus-style metrics."""
import json
import logging
import threading
from collections import Counter
from datetime import datetime, timezone

from flask import g, has_request_context

_EXTRA_FIELDS = ("method", "path", "status", "ms", "user")


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = g.request_id if has_request_context() and "request_id" in g else "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        entry.update({k: getattr(record, k) for k in _EXTRA_FIELDS if hasattr(record, k)})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level="INFO", fmt="text"):
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        JsonFormatter() if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


class Metrics:
    """Tiny thread-safe counter set rendered in the Prometheus text format."""

    def __init__(self):
        self._counts = Counter()
        self._lock = threading.Lock()

    def inc(self, name, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counts[key] += 1

    def render(self, extra=None):
        with self._lock:
            lines = []
            for (name, labels), value in sorted(self._counts.items()):
                label_text = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""
                lines.append(f"{name}{label_text} {value}")
        lines += [f"{name} {value}" for name, value in sorted((extra or {}).items())]
        return "\n".join(lines) + "\n"
