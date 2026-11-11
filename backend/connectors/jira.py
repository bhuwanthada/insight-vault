"""Jira Service Management (Jira Cloud): JQL search (the /search/jql endpoint with page tokens)."""
import re

from .base import Connector, ConnectorError, checked_host, clean_text, days_ago, iso, make_ticket, parse_time, split_tools, tools_from_tags

_PROJECT = re.compile(r"[A-Z][A-Z0-9_]+")
PAGE = 100
FIELDS = ["summary", "description", "resolution", "resolutiondate", "created", "assignee", "labels", "comment"]


BLOCKS = {"paragraph", "heading", "listItem", "blockquote", "codeBlock", "tableCell"}


def adf_text(node):
    """Plain text of an Atlassian Document Format value (Jira stores rich text this way).

    Text runs inside one paragraph are joined as they are; paragraphs and list items are separated by a space."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return " "
    inner = "".join(adf_text(child) for child in node.get("content", []))
    return inner + " " if node.get("type") in BLOCKS else inner


class JiraServiceManagement(Connector):
    KIND = "jira"
    LABEL = "Jira Service Management"
    CONFIG_FIELDS = (("site", "Jira site (for example acme.atlassian.net)", True), ("email", "Atlassian account e-mail", True),
                     ("project", "Project key (optional, for example HELP)", False), ("jql", "Extra JQL filter (optional)", False))
    SECRET_FIELDS = (("api_token", "Atlassian API token", True),)

    def validate(self):
        self.host = checked_host(self.config.get("site"), (".atlassian.net",), "the Jira site")
        project = str(self.config.get("project", "")).strip()
        if project and not _PROJECT.fullmatch(project):
            raise ConnectorError("project must be a Jira project key such as HELP")
        self.project, self.extra_jql = project, str(self.config.get("jql", "")).strip()
        self.auth = (self.config["email"], self.secrets["api_token"])
        self.base = f"https://{self.host}/rest/api/3"

    def _jql(self, since):
        clauses = ["statusCategory = Done", f'resolved >= "{parse_time(since):%Y-%m-%d %H:%M}"']
        if self.project:
            clauses.append(f'project = "{self.project}"')
        if self.extra_jql:
            clauses.append(f"({self.extra_jql})")
        return " AND ".join(clauses) + " ORDER BY resolved ASC"

    def _resolution(self, fields):
        custom = self.mapping.get("resolution_field")
        if custom:
            return clean_text(adf_text(fields.get(custom)))
        comments = (fields.get("comment") or {}).get("comments", [])
        assignee = (fields.get("assignee") or {}).get("displayName")
        by_agent = [c for c in comments if (c.get("author") or {}).get("displayName") == assignee]
        chosen = (by_agent or comments)[-1:] or []
        return clean_text(adf_text(chosen[0].get("body"))) if chosen else clean_text((fields.get("resolution") or {}).get("name"))

    def _tools(self, fields):
        tools = set(tools_from_tags(fields.get("labels"), self.mapping.get("tools_tag_prefix", "tool:")))
        field = self.mapping.get("tools_field")
        return sorted(tools | set(split_tools(adf_text(fields.get(field))))) if field else sorted(tools)

    def pages(self, cursor):
        since = cursor or iso(days_ago(self.now, self.initial_days))
        token = None
        while True:
            payload = {"jql": self._jql(since), "fields": FIELDS, "maxResults": PAGE}
            if token:
                payload["nextPageToken"] = token
            data = self.http.request("POST", self.base + "/search/jql", json=payload, auth=self.auth)
            tickets, newest = [], parse_time(since)
            for issue in data.get("issues", []):
                f = issue.get("fields", {})
                built = make_ticket(
                    "jira", issue["key"], subject=f.get("summary"), created=f.get("created"),
                    resolved=f.get("resolutiondate") or f.get("created"), description=clean_text(adf_text(f.get("description"))),
                    resolution=self._resolution(f), agent=(f.get("assignee") or {}).get("displayName", ""), tools=self._tools(f))
                if built:
                    tickets.append(built)
                    newest = max(newest, parse_time(built["resolvedAt"]))
            token = data.get("nextPageToken")
            yield tickets, newest.isoformat()
            if not token:
                return  # otherwise the page token carries the position; the query itself stays the same

    def test(self):
        return f"Connected to {self.host} as {self.http.request('GET', self.base + '/myself', auth=self.auth).get('displayName', 'a user')}"
