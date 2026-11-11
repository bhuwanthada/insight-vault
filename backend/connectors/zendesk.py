"""Zendesk Support: incremental ticket export (cursor based) with metric sets for the solved time."""
import re

from .base import Connector, ConnectorError, checked_host, clean_text, days_ago, make_ticket, split_tools, tools_from_tags

_SUBDOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*")


class Zendesk(Connector):
    KIND = "zendesk"
    LABEL = "Zendesk"
    CONFIG_FIELDS = (("subdomain", "Zendesk subdomain (the part before .zendesk.com)", True),
                     ("email", "E-mail of the agent that owns the API token", True))
    SECRET_FIELDS = (("api_token", "API token", True),)

    def validate(self):
        sub = str(self.config.get("subdomain", "")).strip().lower().removesuffix(".zendesk.com")
        if not _SUBDOMAIN.fullmatch(sub):
            raise ConnectorError("subdomain may only contain letters, digits and hyphens")
        self.host = checked_host(f"{sub}.zendesk.com", (".zendesk.com",), "the Zendesk host")
        self.auth = (f"{self.config['email']}/token", self.secrets["api_token"])  # Zendesk's API-token sign-in
        self.base = f"https://{self.host}/api/v2"
        self._names = {}

    def _get(self, path, **params):
        return self.http.request("GET", self.base + path, params=params, auth=self.auth)

    def _agent(self, user_id):
        if not user_id:
            return ""
        if user_id not in self._names:
            self._names[user_id] = self._get(f"/users/{user_id}.json")["user"].get("name", "")
        return self._names[user_id]

    def _resolution(self, ticket):
        field = self.mapping.get("resolution_field")
        if field:
            return next((c.get("value") for c in ticket.get("custom_fields", []) if str(c.get("id")) == str(field)), "") or ""
        comments = self._get(f"/tickets/{ticket['id']}/comments.json", sort_order="desc").get("comments", [])
        # newest public reply that was not written by the customer
        return next((c.get("plain_body") or c.get("body") or "" for c in comments
                     if c.get("public") and c.get("author_id") != ticket.get("requester_id")), "")

    def _tools(self, ticket):
        tools = set(tools_from_tags(ticket.get("tags"), self.mapping.get("tools_tag_prefix", "tool:")))
        field = self.mapping.get("tools_field")
        if field:
            tools |= set(split_tools(next((c.get("value") for c in ticket.get("custom_fields", []) if str(c.get("id")) == str(field)), "")))
        return sorted(tools)

    def pages(self, cursor):
        params = {"cursor": cursor} if cursor else {"start_time": int(days_ago(self.now, self.initial_days).timestamp())}
        params["include"] = "metric_sets"
        while True:
            data = self._get("/incremental/tickets/cursor.json", **params)
            solved = {m.get("ticket_id"): m.get("solved_at") for m in data.get("metric_sets", [])}
            tickets = []
            for t in data.get("tickets", []):
                if t.get("status") not in ("solved", "closed") or not str(t.get("subject") or "").strip():
                    continue  # decide before spending extra API calls on a ticket we would drop
                built = make_ticket(
                    "zd", t["id"], subject=t.get("subject"), created=t.get("created_at"),
                    resolved=solved.get(t["id"]) or t.get("updated_at"), description=t.get("description"),
                    resolution=clean_text(self._resolution(t)), agent=self._agent(t.get("assignee_id")), tools=self._tools(t))
                if built:
                    tickets.append(built)
            after = data.get("after_cursor") or cursor
            yield tickets, after
            if data.get("end_of_stream") or after == params.get("cursor"):
                return
            params = {"cursor": after, "include": "metric_sets"}

    def test(self):
        return f"Connected to {self.host} as {self._get('/users/me.json')['user'].get('name', 'an agent')}"
