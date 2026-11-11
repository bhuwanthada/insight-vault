"""Freshdesk: ticket list filtered by updated_since, resolved or closed tickets only."""
import re

from .base import Connector, ConnectorError, checked_host, days_ago, iso, make_ticket, parse_time, split_tools, tools_from_tags

_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*")
PAGE = 100
MAX_PAGES = 300  # Freshdesk serves at most 300 pages (30,000 tickets) per query
RESOLVED, CLOSED = 4, 5


class Freshdesk(Connector):
    KIND = "freshdesk"
    LABEL = "Freshdesk"
    CONFIG_FIELDS = (("domain", "Freshdesk domain (the part before .freshdesk.com)", True),)
    SECRET_FIELDS = (("api_key", "API key (Profile settings)", True),)

    def validate(self):
        sub = str(self.config.get("domain", "")).strip().lower().removesuffix(".freshdesk.com")
        if not _DOMAIN.fullmatch(sub):
            raise ConnectorError("domain may only contain letters, digits and hyphens")
        self.host = checked_host(f"{sub}.freshdesk.com", (".freshdesk.com",), "the Freshdesk host")
        self.auth = (self.secrets["api_key"], "X")  # Freshdesk: the API key is the user name, any text is the password
        self.base = f"https://{self.host}/api/v2"
        self._names = {}

    def _get(self, path, **params):
        return self.http.request("GET", self.base + path, params=params, auth=self.auth)

    def _agent(self, agent_id):
        if not agent_id:
            return ""
        if agent_id not in self._names:
            self._names[agent_id] = (self._get(f"/agents/{agent_id}").get("contact") or {}).get("name", "")
        return self._names[agent_id]

    def _resolution(self, ticket):
        field = self.mapping.get("resolution_field")
        if field:
            return (ticket.get("custom_fields") or {}).get(field) or ""
        replies = self._get(f"/tickets/{ticket['id']}/conversations")
        # newest reply written by an agent (not an incoming customer message)
        return next((c.get("body_text") or "" for c in reversed(replies) if c.get("incoming") is False), "")

    def _tools(self, ticket):
        tools = set(tools_from_tags(ticket.get("tags"), self.mapping.get("tools_tag_prefix", "tool:")))
        field = self.mapping.get("tools_field")
        if field:
            tools |= set(split_tools((ticket.get("custom_fields") or {}).get(field)))
        return sorted(tools)

    def pages(self, cursor):
        since = cursor or iso(days_ago(self.now, self.initial_days))
        for number in range(1, MAX_PAGES + 1):
            batch = self._get("/tickets", updated_since=since, per_page=PAGE, page=number, include="description,stats",
                              order_by="updated_at", order_type="asc")
            if not batch:
                return
            tickets = []
            for t in batch:
                if t.get("status") not in (RESOLVED, CLOSED) or not str(t.get("subject") or "").strip():
                    continue  # decide before spending extra API calls on a ticket we would drop
                built = make_ticket(
                    "fd", t["id"], subject=t.get("subject"), created=t.get("created_at"),
                    resolved=(t.get("stats") or {}).get("resolved_at") or t.get("updated_at"),
                    description=t.get("description_text") or t.get("description"), resolution=self._resolution(t),
                    agent=self._agent(t.get("responder_id")), tools=self._tools(t))
                if built:
                    tickets.append(built)
            since = max((parse_time(t["updated_at"]) for t in batch if t.get("updated_at")), default=parse_time(since)).isoformat()
            yield tickets, since
            if len(batch) < PAGE:
                return

    def test(self):
        self._get("/tickets", per_page=1)
        return f"Connected to {self.host}"
