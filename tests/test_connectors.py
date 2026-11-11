"""Connector tests use recorded-style fixtures shaped like each vendor's documented API responses.

They prove our parsing, paging, safety checks and error handling. They cannot prove the vendors' live
behaviour: that needs a sandbox account for each tool."""
from datetime import datetime, timezone

import pytest
import requests

from backend.connectors import KINDS, ConnectorAuthError, ConnectorError, HttpClient
from backend.connectors import base, csvimport, freshdesk, servicenow
from backend.connectors.base import checked_host, clean_text, make_ticket, parse_time, split_tools, tools_from_tags

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code, self._payload, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("not json")
        return self._payload


_BAD_JSON = object()


class FakeSession:
    """Routes by the end of the URL. A list is a sequence of responses (the last repeats); anything else repeats."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        for suffix in sorted(self.routes, key=len, reverse=True):
            if url.endswith(suffix) or (suffix + "?") in url:
                value = self.routes[suffix]
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, FakeResponse) else FakeResponse(200, value)
        raise AssertionError(f"unexpected request: {method} {url}")


def http(routes):
    session = FakeSession(routes)
    return HttpClient(session, sleep=lambda s: None, retries=2), session


def make(kind, routes, config, secrets, mapping=None, **kw):
    client, session = http(routes)
    return KINDS[kind](config, secrets, mapping or {}, client, NOW, **kw), session


def drain(connector, cursor=None):
    out, last = [], cursor
    for tickets, last in connector.pages(cursor):
        out.extend(tickets)
    return out, last


# ---------- HTTP client ----------
def test_http_client_returns_json_and_never_follows_redirects():
    client, session = http({"/x": {"ok": 1}})
    assert client.request("GET", "https://h/x", params={"a": 1}) == {"ok": 1}
    assert session.calls[0]["allow_redirects"] is False and session.calls[0]["timeout"] == 30


@pytest.mark.parametrize("status", [401, 403])
def test_http_client_reports_rejected_credentials(status):
    client, _ = http({"/x": FakeResponse(status)})
    with pytest.raises(ConnectorAuthError, match=f"HTTP {status}"):
        client.request("GET", "https://h/x")


def test_http_client_explains_other_client_errors_and_bad_bodies():
    with pytest.raises(ConnectorError, match="HTTP 404: missing"):
        http({"/x": FakeResponse(404, text="missing")})[0].request("GET", "https://h/x")
    with pytest.raises(ConnectorError, match="not JSON"):
        http({"/x": FakeResponse(200, _BAD_JSON)})[0].request("GET", "https://h/x")


def test_http_client_retries_rate_limits_and_server_errors_honouring_retry_after():
    sleeps = []
    session = FakeSession({"/x": [FakeResponse(429, headers={"Retry-After": "4"}), FakeResponse(503), {"done": True}]})
    client = HttpClient(session, sleep=sleeps.append, retries=3)
    assert client.request("GET", "https://h/x") == {"done": True}
    assert sleeps == [4.0, 1.0]  # the tool's own Retry-After first, then our backoff
    capped = HttpClient(FakeSession({"/x": [FakeResponse(429, headers={"Retry-After": "900"}), {"ok": 1}]}), sleep=sleeps.append)
    capped.request("GET", "https://h/x")
    assert sleeps[-1] == 30.0


def test_http_client_gives_up_after_the_last_attempt():
    client, session = http({"/x": FakeResponse(500)})
    with pytest.raises(ConnectorError, match="HTTP 500 after 3 attempts"):
        client.request("GET", "https://h/x")
    assert len(session.calls) == 3
    net = HttpClient(FakeSession({"/x": requests.ConnectionError("dns")}), sleep=lambda s: None, retries=1)
    with pytest.raises(ConnectorError, match="network error: dns after 2 attempts"):
        net.request("GET", "https://h/x")


# ---------- helpers ----------
def test_checked_host_accepts_only_the_vendors_domains():
    assert checked_host("https://Acme.Atlassian.net/", (".atlassian.net",), "site") == "acme.atlassian.net"
    for bad in ("evil.com", "atlassian.net", "acme.atlassian.net.evil.com", "169.254.169.254", "a..atlassian.net", "", None,
                "acme.atlassian.net/../x", "user@acme.atlassian.net"):
        with pytest.raises(ConnectorError, match="must be a host ending in"):
            checked_host(bad, (".atlassian.net",), "site")
    assert checked_host("it.corp.example", (".service-now.com",), "x", extra=(".corp.example",)) == "it.corp.example"


@pytest.mark.parametrize("text", ["2026-06-14T12:00:00Z", "2026-06-14T12:00:00+00:00", "2026-06-14 12:00:00", "2026-06-14 12:00",
                                  "2026-06-14T12:00:00.000+0000", "2026-06-14T17:30:00+05:30"])
def test_parse_time_understands_what_tools_send(text):
    parsed = parse_time(text)
    assert parsed.tzinfo is not None and parsed.year == 2026 and parsed.day == 14


def test_parse_time_rejects_garbage():
    with pytest.raises(ValueError, match="unreadable date"):
        parse_time("yesterday-ish")


def test_make_ticket_shapes_and_guards():
    t = make_ticket("zd", 7, subject="  Hi  ", created="2026-06-01T10:00:00Z", resolved="2026-06-01T12:00:00Z", agent="", tools=["ssh"])
    assert t["id"] == "zd-7" and t["subject"] == "Hi" and t["agent"] == "unknown" and t["status"] == "resolved" and t["tools_used"] == ["ssh"]
    assert make_ticket("zd", 1, subject="  ", created="2026-06-01", resolved="2026-06-01") is None
    assert make_ticket("zd", 1, subject="s", created="garbage", resolved="2026-06-01") is None
    clamped = make_ticket("zd", 1, subject="s", created="2026-06-02T00:00:00Z", resolved="2026-06-01T00:00:00Z")
    assert clamped["resolvedAt"] == clamped["createdAt"]  # never a negative duration


def test_text_and_tag_helpers():
    assert clean_text("a \n  b\t c") == "a b c" and clean_text(None) == "" and len(clean_text("x" * 50, limit=10)) == 10
    assert tools_from_tags(["Tool:Grafana", "tool:ssh", "tool:", "vip"], "tool:") == ["grafana", "ssh"] and tools_from_tags(["a"], "") == []
    assert split_tools("Grafana; ssh | Jira,grafana") == ["grafana", "jira", "ssh"] and split_tools(None) == []
    assert base.days_ago(NOW, 2).day == 13 and base.iso("2026-06-01T00:00:00Z").startswith("2026-06-01")


def test_connectors_describe_themselves_and_report_missing_settings():
    assert sorted(KINDS) == ["freshdesk", "jira", "salesforce", "servicenow", "zendesk"]
    d = KINDS["zendesk"].describe()
    assert d["label"] == "Zendesk" and d["config"][0] == {"name": "subdomain", "label": d["config"][0]["label"], "required": True}
    assert d["secrets"][0]["name"] == "api_token"
    assert KINDS["zendesk"].missing({"subdomain": "a"}, {}) == ["email", "api_token"] and KINDS["zendesk"].missing({"subdomain": "a", "email": "e"}, {"api_token": "t"}) == []
    assert KINDS["salesforce"].missing({"instance_url": "a.salesforce.com"}, {}) == ["access_token or client_id and client_secret"]
    assert KINDS["salesforce"].missing({"instance_url": "a.salesforce.com"}, {"access_token": "t"}) == []
    assert KINDS["salesforce"].missing({}, {"client_id": "i", "client_secret": "s"}) == ["instance_url"]
    with pytest.raises(NotImplementedError):
        base.Connector({}, {}, {}, None, NOW).pages(None)
    with pytest.raises(NotImplementedError):
        base.Connector({}, {}, {}, None, NOW).test()


# ---------- Zendesk ----------
ZD = {"subdomain": "acme", "email": "ops@acme.com"}
ZD_SECRET = {"api_token": "tok"}


def zendesk_routes():
    page1 = {"tickets": [
        {"id": 1, "subject": "Cannot log in", "description": "locked out", "status": "solved", "created_at": "2026-06-01T10:00:00Z",
         "updated_at": "2026-06-02T10:00:00Z", "assignee_id": 77, "requester_id": 5, "tags": ["tool:admin console", "vip"],
         "custom_fields": [{"id": 900, "value": "Admin console; Audit log"}, {"id": 901, "value": "Reset the password"}]},
        {"id": 2, "subject": "Still open", "status": "open", "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-01T11:00:00Z"},
    ], "metric_sets": [{"ticket_id": 1, "solved_at": "2026-06-01T12:00:00Z"}], "after_cursor": "c1", "end_of_stream": False}
    page2 = {"tickets": [{"id": 3, "subject": "Billing question", "status": "closed", "created_at": "2026-06-03T10:00:00Z",
                          "updated_at": "2026-06-03T13:00:00Z", "assignee_id": 77, "requester_id": 6, "tags": []},
                         {"id": 4, "subject": "", "status": "solved", "created_at": "2026-06-03T10:00:00Z", "updated_at": "2026-06-03T11:00:00Z"}],
             "after_cursor": "c2", "end_of_stream": True}
    return {
        "/incremental/tickets/cursor.json": [page1, page2],
        "/users/77.json": {"user": {"name": "Asha"}},
        "/users/me.json": {"user": {"name": "Ops Bot"}},
        "/tickets/1/comments.json": {"comments": [
            {"public": False, "author_id": 77, "body": "internal note"},
            {"public": True, "author_id": 77, "plain_body": "Reset the password and cleared cache.", "body": "<p>x</p>"},
            {"public": True, "author_id": 5, "body": "I cannot log in"}]},
        "/tickets/3/comments.json": {"comments": [{"public": True, "author_id": 77, "body": "Issued a refund."}]},
    }


def test_zendesk_exports_solved_tickets_page_by_page_with_cursors():
    connector, session = make("zendesk", zendesk_routes(), ZD, ZD_SECRET)
    tickets, last = drain(connector)
    assert [t["id"] for t in tickets] == ["zd-1", "zd-3"] and last == "c2"  # open and subject-less tickets are skipped
    first = tickets[0]
    assert first["subject"] == "Cannot log in" and first["agent"] == "Asha" and first["tools_used"] == ["admin console"]
    assert first["resolution"] == "Reset the password and cleared cache." and first["resolvedAt"].startswith("2026-06-01T12:00:00")
    assert tickets[1]["resolution"] == "Issued a refund." and tickets[1]["resolvedAt"].startswith("2026-06-03T13:00:00")  # no metric: updated_at
    assert session.calls[0]["params"]["start_time"] == int(base.days_ago(NOW, 90).timestamp()) and "cursor" not in session.calls[0]["params"]
    second = [c for c in session.calls if c["url"].endswith("cursor.json")][1]
    assert second["params"] == {"cursor": "c1", "include": "metric_sets"} and second["auth"] == ("ops@acme.com/token", "tok")


def test_zendesk_resumes_from_a_saved_cursor_and_maps_custom_fields():
    routes = zendesk_routes()
    routes["/incremental/tickets/cursor.json"] = [routes["/incremental/tickets/cursor.json"][1]]
    connector, session = make("zendesk", routes, ZD, ZD_SECRET, {"resolution_field": "901", "tools_field": "900", "tools_tag_prefix": ""})
    drain(connector, "c1")
    assert session.calls[0]["params"]["cursor"] == "c1"
    routes = zendesk_routes()
    tickets, _ = drain(make("zendesk", routes, ZD, ZD_SECRET, {"resolution_field": "901", "tools_field": "900"})[0])
    assert tickets[0]["resolution"] == "Reset the password" and tickets[0]["tools_used"] == ["admin console", "audit log"]


def test_zendesk_stops_when_the_cursor_does_not_advance():
    stuck = {"tickets": [], "after_cursor": "same", "end_of_stream": False}
    connector, session = make("zendesk", {"/incremental/tickets/cursor.json": stuck}, ZD, ZD_SECRET)
    drain(connector, "same")
    assert len(session.calls) == 1


def test_zendesk_test_connection_and_agent_lookups_are_cached():
    connector, session = make("zendesk", zendesk_routes(), ZD, ZD_SECRET)
    assert connector.test() == "Connected to acme.zendesk.com as Ops Bot"
    drain(connector)
    assert sum(c["url"].endswith("/users/77.json") for c in session.calls) == 1  # asked once for both tickets
    assert connector._agent(None) == ""


@pytest.mark.parametrize("config", [{"subdomain": "evil.com", "email": "e"}, {"subdomain": "a/b", "email": "e"},
                                    {"subdomain": "169.254.169.254", "email": "e"}, {"subdomain": "", "email": "e"}])
def test_zendesk_refuses_unsafe_hosts(config):
    with pytest.raises(ConnectorError):
        make("zendesk", {}, config, ZD_SECRET)


def test_zendesk_accepts_the_full_host_name_too():
    assert make("zendesk", {}, {"subdomain": "Acme.zendesk.com", "email": "e"}, ZD_SECRET)[0].host == "acme.zendesk.com"


# ---------- Freshdesk ----------
FD, FD_SECRET = {"domain": "acme"}, {"api_key": "key"}


def freshdesk_routes():
    return {
        "/tickets": [[
            {"id": 10, "subject": "Export fails", "status": 4, "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-02T09:00:00Z",
             "responder_id": 9, "description_text": "csv export times out", "tags": ["tool:grafana"], "stats": {"resolved_at": "2026-06-01T15:00:00Z"},
             "custom_fields": {"cf_fix": "Applied a hotfix", "cf_tools": "ssh, jira"}},
            {"id": 11, "subject": "Open", "status": 2, "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-02T10:00:00Z"},
            {"id": 12, "subject": "Closed one", "status": 5, "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-03T10:00:00Z", "description": "<b>x</b>"},
        ]],
        "/agents/9": {"contact": {"name": "Meera"}},
        "/tickets/10/conversations": [[{"incoming": True, "body_text": "help please"}, {"incoming": False, "body_text": "Restarted the worker and cleared the cache."}]],
        "/tickets/12/conversations": [[]],
    }


def test_freshdesk_reads_resolved_and_closed_tickets():
    connector, session = make("freshdesk", freshdesk_routes(), FD, FD_SECRET)
    tickets, last = drain(connector)
    assert [t["id"] for t in tickets] == ["fd-10", "fd-12"] and last.startswith("2026-06-03T10:00:00")
    assert tickets[0]["agent"] == "Meera" and tickets[0]["tools_used"] == ["grafana"] and tickets[0]["resolvedAt"].startswith("2026-06-01T15:00:00")
    assert tickets[0]["resolution"] == "Restarted the worker and cleared the cache." and tickets[1]["agent"] == "unknown" and tickets[1]["resolution"] == ""
    call = session.calls[0]
    assert call["auth"] == ("key", "X") and call["params"]["include"] == "description,stats" and call["params"]["order_by"] == "updated_at"
    assert call["params"]["updated_since"] == base.iso(base.days_ago(NOW, 90))


def test_freshdesk_pages_until_a_short_page_and_honours_custom_field_mappings(monkeypatch):
    monkeypatch.setattr(freshdesk, "PAGE", 1)
    routes = freshdesk_routes()
    first = routes["/tickets"][0][0]
    routes["/tickets"] = [[first], [], ]
    connector, session = make("freshdesk", routes, FD, FD_SECRET, {"resolution_field": "cf_fix", "tools_field": "cf_tools"})
    tickets, _ = drain(connector)
    assert tickets[0]["resolution"] == "Applied a hotfix" and tickets[0]["tools_used"] == ["grafana", "jira", "ssh"]
    assert [c["params"]["page"] for c in session.calls if c["url"].endswith("/tickets")] == [1, 2]  # the empty page ends it


def test_freshdesk_stops_at_the_page_limit_and_keeps_the_cursor_when_nothing_has_a_date(monkeypatch):
    monkeypatch.setattr(freshdesk, "PAGE", 1)
    monkeypatch.setattr(freshdesk, "MAX_PAGES", 2)
    routes = {"/tickets": [[{"id": 1, "subject": "s", "status": 4, "created_at": "2026-06-01T10:00:00Z"}]]}
    connector, session = make("freshdesk", routes, FD, FD_SECRET, {"resolution_field": "x"})
    drain(connector, "2026-06-01T00:00:00+00:00")
    assert len(session.calls) == 2


def test_freshdesk_connection_test_and_validation():
    connector, _ = make("freshdesk", {"/tickets": [[]]}, FD, FD_SECRET)
    assert connector.test() == "Connected to acme.freshdesk.com" and connector._agent(None) == ""
    for bad in ("evil.com", "a b", "x/y"):
        with pytest.raises(ConnectorError):
            make("freshdesk", {}, {"domain": bad}, FD_SECRET)
    assert make("freshdesk", {}, {"domain": "Acme.freshdesk.com"}, FD_SECRET)[0].host == "acme.freshdesk.com"


# ---------- Jira Service Management ----------
JIRA = {"site": "acme.atlassian.net", "email": "ops@acme.com", "project": "HELP", "jql": 'labels = "support"'}
JIRA_SECRET = {"api_token": "tok"}


def adf(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def jira_routes():
    p1 = {"issues": [{"key": "HELP-1", "fields": {
        "summary": "Slow dashboard", "description": adf("Dashboard takes minutes"), "resolution": {"name": "Done"},
        "resolutiondate": "2026-06-02T10:00:00.000+0000", "created": "2026-06-01T09:00:00.000+0000", "assignee": {"displayName": "Ravi"},
        "labels": ["tool:grafana", "other"], "customfield_1": adf("Restarted via runbook"), "customfield_2": adf("ssh, jira"),
        "comment": {"comments": [{"author": {"displayName": "Customer"}, "body": adf("it is slow")},
                                 {"author": {"displayName": "Ravi"}, "body": adf("Restarted the reporting service.")},
                                 {"author": {"displayName": "Customer"}, "body": adf("thanks!")}]}}}],
          "nextPageToken": "t2"}
    p2 = {"issues": [{"key": "HELP-2", "fields": {"summary": "Quick one", "resolution": {"name": "Won't fix"}, "created": "2026-06-03T09:00:00.000+0000",
                                                  "resolutiondate": "2026-06-03T10:00:00.000+0000"}},
                     {"key": "HELP-3", "fields": {"summary": "", "created": "2026-06-03T09:00:00.000+0000"}}]}
    return {"/search/jql": [p1, p2], "/myself": {"displayName": "Ops Bot"}}


def test_jira_searches_with_jql_and_follows_page_tokens():
    connector, session = make("jira", jira_routes(), JIRA, JIRA_SECRET)
    tickets, last = drain(connector)
    assert [t["id"] for t in tickets] == ["jira-HELP-1", "jira-HELP-2"] and last.startswith("2026-06-03T10:00:00")
    first = tickets[0]
    assert first["agent"] == "Ravi" and first["tools_used"] == ["grafana"] and first["description"] == "Dashboard takes minutes"
    assert first["resolution"] == "Restarted the reporting service."  # the assignee's own newest comment
    assert tickets[1]["resolution"] == "Won't fix"  # no comments: fall back to the resolution name
    body = session.calls[0]["json"]
    assert body["jql"].startswith("statusCategory = Done AND resolved >= ") and 'project = "HELP"' in body["jql"]
    assert '(labels = "support")' in body["jql"] and body["jql"].endswith("ORDER BY resolved ASC") and "nextPageToken" not in body
    assert session.calls[1]["json"]["nextPageToken"] == "t2" and session.calls[0]["auth"] == ("ops@acme.com", "tok")


def test_jira_custom_field_mappings_and_commentless_issues():
    config = {"site": "acme.atlassian.net", "email": "e"}
    connector, session = make("jira", jira_routes(), config, JIRA_SECRET, {"resolution_field": "customfield_1", "tools_field": "customfield_2"})
    tickets, _ = drain(connector, "2026-06-01T00:00:00+00:00")
    assert tickets[0]["resolution"] == "Restarted via runbook" and tickets[0]["tools_used"] == ["grafana", "jira", "ssh"]
    assert "project" not in session.calls[0]["json"]["jql"]
    no_assignee = {"issues": [{"key": "H-9", "fields": {"summary": "s", "created": "2026-06-01T09:00:00.000+0000", "resolutiondate": "2026-06-01T10:00:00.000+0000",
                                                         "comment": {"comments": [{"author": {"displayName": "X"}, "body": adf("done")}]}}}]}
    out, _ = drain(make("jira", {"/search/jql": no_assignee}, config, JIRA_SECRET)[0])
    assert out[0]["resolution"] == "done" and out[0]["agent"] == "unknown"


def test_jira_connection_test_and_safety_checks():
    assert make("jira", jira_routes(), JIRA, JIRA_SECRET)[0].test() == "Connected to acme.atlassian.net as Ops Bot"
    for site in ("evil.com", "acme.atlassian.net.evil.com", "atlassian.net"):
        with pytest.raises(ConnectorError):
            make("jira", {}, {**JIRA, "site": site}, JIRA_SECRET)
    with pytest.raises(ConnectorError, match="project key"):
        make("jira", {}, {**JIRA, "project": "help; DROP"}, JIRA_SECRET)


def test_adf_text_flattens_rich_text():
    from backend.connectors.jira import adf_text

    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": "world"}]},
                                       {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]}]}
    assert " ".join(adf_text(doc).split()) == "Helloworld Second" and adf_text("plain") == "plain" and adf_text(None) == "" and adf_text(5) == ""


# ---------- ServiceNow ----------
SN, SN_SECRET = {"instance": "acme", "username": "api"}, {"password": "pw"}


def servicenow_rows(n=2, offset=0):
    return [{"sys_id": f"id{offset + i}", "number": f"INC00{offset + i}", "short_description": f"Issue {offset + i}", "description": "d",
             "close_notes": "Restarted service", "close_code": "Solved (Permanently)", "opened_at": "2026-06-01 10:00:00",
             "resolved_at": "2026-06-01 12:00:00", "sys_updated_on": f"2026-06-0{2 + offset + i} 09:00:00", "assigned_to": "abc", "u_tools": "ssh;grafana"}
            for i in range(n)]


def test_servicenow_pages_by_offset_and_maps_close_notes():
    routes = {"/incident": [{"result": servicenow_rows(2)}, {"result": servicenow_rows(1, 2)}], "/sys_user/abc": {"result": {"name": "Priya"}}}
    connector, session = make("servicenow", routes, SN, SN_SECRET, {"tools_field": "u_tools"})
    original = servicenow.PAGE
    servicenow.PAGE = 2
    try:
        tickets, last = drain(connector)
    finally:
        servicenow.PAGE = original
    assert [t["id"] for t in tickets] == ["snow-INC000", "snow-INC001", "snow-INC002"] and last.startswith("2026-06-04T09:00:00")
    assert tickets[0]["resolution"] == "Restarted service (Solved (Permanently))" and tickets[0]["agent"] == "Priya" and tickets[0]["tools_used"] == ["grafana", "ssh"]
    params = [c["params"] for c in session.calls if c["url"].endswith("/incident")]
    assert [p["sysparm_offset"] for p in params] == [0, 2] and params[0]["sysparm_query"].startswith("stateIN6,7^sys_updated_on>=")
    assert params[0]["sysparm_exclude_reference_link"] == "true" and session.calls[0]["auth"] == ("api", "pw")
    assert sum(c["url"].endswith("/sys_user/abc") for c in session.calls) == 1


def test_servicenow_handles_sparse_records():
    rows = [{"sys_id": "s1", "short_description": "Only notes", "close_notes": "Fixed", "opened_at": "2026-06-01 10:00:00", "sys_updated_on": "2026-06-02 10:00:00"},
            {"sys_id": "s2", "short_description": "Only code", "close_code": "Duplicate", "opened_at": "2026-06-01 10:00:00", "closed_at": "2026-06-01 11:00:00"},
            {"sys_id": "s3", "short_description": "", "opened_at": "2026-06-01 10:00:00"}]
    tickets, last = drain(make("servicenow", {"/incident": {"result": rows}}, SN, SN_SECRET)[0], "2026-06-01T00:00:00+00:00")
    assert [t["id"] for t in tickets] == ["snow-s1", "snow-s2"] and tickets[0]["resolution"] == "Fixed" and tickets[1]["resolution"] == "Duplicate"
    assert tickets[0]["agent"] == "unknown" and last.startswith("2026-06-02T10:00:00")


def test_servicenow_validation_and_custom_domains():
    connector, _ = make("servicenow", {"/u_support_case": {"result": []}}, {**SN, "table": "u_support_case"}, SN_SECRET)
    assert connector.host == "acme.service-now.com" and connector.table == "u_support_case" and connector.test() == "Connected to acme.service-now.com"
    for bad in ({"instance": "evil.com"}, {"instance": "a b"}, {"instance": "x/y"}, {"table": "incident; drop"}, {"table": "../x"}):
        with pytest.raises(ConnectorError):
            make("servicenow", {}, {**SN, **bad}, SN_SECRET)
    with pytest.raises(ConnectorError):  # a full host is allowed only on an allowed domain
        make("servicenow", {}, {**SN, "instance": "it.corp.example"}, SN_SECRET)
    assert make("servicenow", {}, {**SN, "instance": "it.corp.example"}, SN_SECRET, extra_hosts=(".corp.example",))[0].host == "it.corp.example"
    assert make("servicenow", {}, {**SN, "instance": "acme.service-now.com"}, SN_SECRET)[0].host == "acme.service-now.com"
    assert connector._agent(None) == ""


# ---------- Salesforce ----------
SF = {"instance_url": "https://acme.my.salesforce.com", "api_version": "v60.0"}


def case(i, comment="Re-issued the refund.", **over):
    base_case = {"Id": f"500{i}", "CaseNumber": f"0000{i}", "Subject": f"Case {i}", "Description": "d", "CreatedDate": "2026-06-01T10:00:00.000+0000",
                 "ClosedDate": "2026-06-01T12:00:00.000+0000", "LastModifiedDate": f"2026-06-0{1 + i}T12:00:00.000+0000", "Owner": {"Name": "Asha"},
                 "CaseComments": {"records": [{"CommentBody": comment}]} if comment else None, "Resolution__c": "Custom fix", "Tools__c": "ssh;jira"}
    return {**base_case, **over}


def salesforce_routes():
    return {
        "/services/oauth2/token": {"access_token": "tok1"},
        "/services/data/v60.0/query": {"records": [case(1)], "done": False, "nextRecordsUrl": "/services/data/v60.0/query/01g-2000"},
        "/services/data/v60.0/query/01g-2000": {"records": [case(2, comment=None), case(3, Subject="")], "done": True},
        "/services/data/v60.0/limits": {"DailyApiRequests": {}},
    }


def test_salesforce_logs_in_runs_soql_and_follows_next_records():
    connector, session = make("salesforce", salesforce_routes(), SF, {"client_id": "id", "client_secret": "sec"})
    tickets, last = drain(connector)
    assert [t["id"] for t in tickets] == ["sf-00001", "sf-00002"] and last.startswith("2026-06-04T12:00:00")  # the cursor moves past skipped cases too
    assert tickets[0]["resolution"] == "Re-issued the refund." and tickets[0]["agent"] == "Asha" and tickets[1]["resolution"] == ""
    login = session.calls[0]
    assert login["method"] == "POST" and login["data"]["grant_type"] == "client_credentials" and login["data"]["client_id"] == "id"
    query = session.calls[1]
    assert query["headers"]["Authorization"] == "Bearer tok1" and "FROM Case WHERE IsClosed = true AND LastModifiedDate >" in query["params"]["q"]
    assert "(SELECT CommentBody FROM CaseComments" in query["params"]["q"] and "Resolution__c" not in query["params"]["q"]


def test_salesforce_custom_fields_and_a_static_token():
    connector, session = make("salesforce", salesforce_routes(), SF, {"access_token": "static"},
                              {"resolution_field": "Resolution__c", "tools_field": "Tools__c"})
    tickets, _ = drain(connector, "2026-06-01T00:00:00+00:00")
    assert tickets[0]["resolution"] == "Custom fix" and tickets[0]["tools_used"] == ["jira", "ssh"]
    assert not any("oauth2/token" in c["url"] for c in session.calls)  # no login with a static token
    assert "Resolution__c, Tools__c" in session.calls[0]["params"]["q"] and "2026-06-01T00:00:00Z" in session.calls[0]["params"]["q"]


def test_salesforce_refreshes_an_expired_token_once():
    routes = salesforce_routes()
    routes["/services/oauth2/token"] = [{"access_token": "old"}, {"access_token": "new"}]
    routes["/services/data/v60.0/query"] = [FakeResponse(401), {"records": [], "done": True}]
    connector, session = make("salesforce", routes, SF, {"client_id": "id", "client_secret": "sec"})
    drain(connector)
    assert [c["headers"]["Authorization"] for c in session.calls if "query" in c["url"]] == ["Bearer old", "Bearer new"]


def test_salesforce_does_not_retry_a_rejected_static_token():
    routes = salesforce_routes()
    routes["/services/data/v60.0/query"] = FakeResponse(401)
    connector, _ = make("salesforce", routes, SF, {"access_token": "static"})
    with pytest.raises(ConnectorAuthError):
        drain(connector)


def test_salesforce_refuses_unexpected_next_page_addresses():
    routes = salesforce_routes()
    routes["/services/data/v60.0/query"] = {"records": [], "done": False, "nextRecordsUrl": "https://evil.example/steal"}
    with pytest.raises(ConnectorError, match="unexpected next-page address"):
        drain(make("salesforce", routes, SF, {"access_token": "t"})[0])


def test_salesforce_connection_test_and_validation():
    assert make("salesforce", salesforce_routes(), SF, {"access_token": "t"})[0].test() == "Connected to acme.my.salesforce.com"
    for bad in ({"instance_url": "evil.com"}, {"instance_url": "acme.salesforce.com.evil.com"}, {"api_version": "latest"}):
        with pytest.raises(ConnectorError):
            make("salesforce", {}, {**SF, **bad}, {"access_token": "t"})
    for field in ("resolution_field", "tools_field"):
        with pytest.raises(ConnectorError, match="custom field API name"):
            make("salesforce", {}, SF, {"access_token": "t"}, {field: "Name; DROP"})
    assert make("salesforce", {}, {"instance_url": "acme.salesforce.com"}, {"access_token": "t"})[0].version == "v60.0"


# ---------- CSV import ----------
CSV = "Ticket ID,Title,Description,Resolution,Assignee,Tools,Created,Resolved\n" \
      "A-1,Cannot log in,locked out,Reset the password,Asha,admin console; audit log,2026-06-01 10:00:00,2026-06-01 12:00:00\n" \
      "A-2,Slow page,loading,Restarted service,Ravi,grafana,2026-06-02T10:00:00Z,2026-06-02T15:00:00Z\n" \
      "A-3,,no subject,x,x,x,2026-06-02T10:00:00Z,2026-06-02T15:00:00Z\n" \
      "A-4,Bad date,d,r,a,t,someday,2026-06-02T15:00:00Z\n"


def test_csv_import_finds_columns_by_common_names_and_reports_skipped_rows():
    tickets, skipped = csvimport.parse_csv(CSV)
    assert [t["id"] for t in tickets] == ["csv-A-1", "csv-A-2"]
    assert tickets[0]["subject"] == "Cannot log in" and tickets[0]["resolution"] == "Reset the password" and tickets[0]["agent"] == "Asha"
    assert tickets[0]["tools_used"] == ["admin console", "audit log"] and tickets[0]["createdAt"].startswith("2026-06-01T10:00:00")
    assert skipped == [{"row": 4, "reason": "missing subject"}, {"row": 5, "reason": "unreadable date"}]


def test_csv_import_supports_other_delimiters_explicit_mapping_and_date_formats():
    text = "ref;what;opened;done\nX1;Printer jam;01/06/2026 09:00;01/06/2026 10:30\nX2;No toner;02/06/2026 09:00;02/06/2026 09:45\n"
    tickets, skipped = csvimport.parse_csv(text, {"subject": "what", "id": "ref", "created": "opened", "resolved": "done"}, "%d/%m/%Y %H:%M")
    assert [t["id"] for t in tickets] == ["csv-X1", "csv-X2"] and tickets[0]["resolvedAt"].startswith("2026-06-01T10:30:00") and skipped == []
    wrong, skipped = csvimport.parse_csv(text, {"subject": "what", "id": "ref", "created": "opened", "resolved": "done"}, "%Y")
    assert wrong == [] and len(skipped) == 2


def test_csv_ids_are_stable_when_the_file_has_no_id_column():
    text = "subject,created,resolved\nHello,2026-06-01T10:00:00Z,2026-06-01T11:00:00Z\n"
    first, _ = csvimport.parse_csv(text)
    second, _ = csvimport.parse_csv(text)
    assert first[0]["id"] == second[0]["id"] and first[0]["id"].startswith("csv-") and len(first[0]["id"]) == 16  # re-importing never duplicates


@pytest.mark.parametrize(
    "text,mapping,expected",
    [("only a header", None, "header row and at least one ticket"), ("a,b\n1,2\n", None, "could not find a column for: subject, created, resolved"),
     ("subject,created,resolved\nx,2026-06-01,2026-06-01\n", {"subject": "nope"}, "column 'nope' for subject was not found")],
)
def test_csv_import_explains_what_is_wrong(text, mapping, expected):
    with pytest.raises(ConnectorError, match=expected):
        csvimport.parse_csv(text, mapping)


def test_csv_import_enforces_a_row_limit(monkeypatch):
    monkeypatch.setattr(csvimport, "MAX_ROWS", 2)
    text = "subject,created,resolved\n" + "x,2026-06-01T10:00:00Z,2026-06-01T11:00:00Z\n" * 3
    with pytest.raises(ConnectorError, match="too many rows"):
        csvimport.parse_csv(text)
