"""Shared pieces for ticketing-tool connectors: HTTP with retries, host safety, and ticket normalisation.

Every connector turns a vendor's tickets into the app's one ticket shape:

    id, subject, description, resolution, agent, tools_used, status, createdAt, resolvedAt
"""
import re
import time
from datetime import datetime, timedelta, timezone

import requests


class ConnectorError(Exception):
    """A connector could not reach the tool or the tool refused the request. The message is safe to show."""


class ConnectorAuthError(ConnectorError):
    """The tool rejected the credentials (HTTP 401 or 403)."""


class HttpClient:
    """JSON over HTTPS with retries for rate limits and server errors. Redirects are never followed:
    a redirect could send our credentials (or our server) somewhere the organization chose."""

    def __init__(self, session=None, sleep=time.sleep, retries=3, timeout=30):
        self.session, self._sleep, self.retries, self.timeout = session or requests.Session(), sleep, retries, timeout

    def _wait(self, response, attempt):
        header = response.headers.get("Retry-After") if response is not None else None
        if header and header.replace(".", "", 1).isdigit():
            return min(float(header), 30.0)  # honour the tool's own request, within reason
        return min(8.0, 0.5 * 2**attempt)

    def request(self, method, url, **kwargs):
        last = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, allow_redirects=False, **kwargs)
            except requests.RequestException as exc:
                last, response = f"network error: {exc}", None
            else:
                code = response.status_code
                if code in (401, 403):
                    raise ConnectorAuthError(f"authentication failed (HTTP {code}); check the credentials and their permissions")
                if code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ConnectorError("the tool returned something that is not JSON") from exc
                if code != 429 and code < 500:
                    raise ConnectorError(f"HTTP {code}: {response.text[:200]}")
                last = f"HTTP {code}"
            if attempt < self.retries:
                self._sleep(self._wait(response, attempt))
        raise ConnectorError(f"{last} after {self.retries + 1} attempts")


def checked_host(host, suffixes, label, extra=()):
    """Validate a user-supplied host name against the vendor's real domains. Returns it lower-cased."""
    host = (host or "").strip().lower().removeprefix("https://").rstrip("/")
    allowed = tuple(suffixes) + tuple(extra)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host) or ".." in host or not host.endswith(allowed):
        raise ConnectorError(f"{label} must be a host ending in {' or '.join(allowed)}")
    return host


def parse_time(value):
    """Parse the timestamp styles tools use into an aware UTC datetime. Raises ValueError if unreadable."""
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    raise ValueError(f"unreadable date '{value}'")


def iso(value):
    return parse_time(value).isoformat()


def days_ago(now, days):
    return (now - timedelta(days=days)).astimezone(timezone.utc)


def clean_text(value, limit=20000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def tools_from_tags(tags, prefix):
    """Tags such as 'tool:grafana' name the tools an agent used."""
    if not prefix:
        return []
    return sorted({t[len(prefix):].strip().lower() for t in tags or [] if str(t).lower().startswith(prefix.lower()) and t[len(prefix):].strip()})


def split_tools(value):
    return sorted({p.strip().lower() for p in re.split(r"[;,|]", str(value or "")) if p.strip()})


def make_ticket(source, ticket_id, *, subject, created, resolved, description="", resolution="", agent="", tools=()):
    """Build the app's ticket shape, or return None when a ticket lacks what a resolution report needs."""
    if not str(subject or "").strip():
        return None
    try:
        created_at, resolved_at = iso(created), iso(resolved)
    except ValueError:
        return None
    if parse_time(resolved_at) < parse_time(created_at):
        resolved_at = created_at  # a tool's timestamps can disagree; never produce a negative duration
    return {
        "id": f"{source}-{ticket_id}", "subject": clean_text(subject, 500), "description": clean_text(description),
        "resolution": clean_text(resolution), "agent": clean_text(agent, 200) or "unknown", "tools_used": list(tools),
        "status": "resolved", "createdAt": created_at, "resolvedAt": resolved_at,
    }


class Connector:
    """One configured connection to a ticketing tool. Subclasses define KIND and implement ``pages``."""

    KIND = ""
    LABEL = ""
    CONFIG_FIELDS = ()  # ((name, label, required), ...)
    SECRET_FIELDS = ()  # ((name, label, required), ...)

    def __init__(self, config, secrets, mapping, http, now, initial_days=90, extra_hosts=()):
        self.config, self.secrets, self.mapping, self.http = config, secrets, mapping or {}, http
        self.now, self.initial_days, self.extra_hosts = now, initial_days, tuple(extra_hosts)
        self.validate()

    def validate(self):
        """Raise ConnectorError if required settings are missing or unsafe."""

    def pages(self, cursor):
        """Yield (tickets, cursor_after_this_page) until the tool has nothing newer."""
        raise NotImplementedError

    def test(self):
        """A cheap call proving the credentials work. Returns a short human-readable description."""
        raise NotImplementedError

    @classmethod
    def describe(cls):
        return {
            "kind": cls.KIND, "label": cls.LABEL,
            "config": [{"name": n, "label": label, "required": req} for n, label, req in cls.CONFIG_FIELDS],
            "secrets": [{"name": n, "label": label, "required": req} for n, label, req in cls.SECRET_FIELDS],
        }

    @classmethod
    def missing(cls, config, secrets):
        """Names of required settings that were not provided."""
        return ([n for n, _, req in cls.CONFIG_FIELDS if req and not str(config.get(n, "")).strip()]
                + [n for n, _, req in cls.SECRET_FIELDS if req and not str(secrets.get(n, "")).strip()])
