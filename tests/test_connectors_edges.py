"""Edge cases in the connectors and the connector registry."""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from backend import schema
from backend.connectors import freshdesk
from backend.connectors.jira import adf_text
from backend.db import make_engine
from backend.connectors.repo import ConnectorRepo
from tests.test_connectors import FD, FD_SECRET, SF, ZD, ZD_SECRET, case, drain, make, salesforce_routes, zendesk_routes
from tests.dburl import db_url

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def test_agent_names_are_looked_up_once_even_for_many_tickets():
    ticket = {"status": 4, "subject": "s", "created_at": "2026-06-01T10:00:00Z", "updated_at": "2026-06-02T10:00:00Z", "responder_id": 9}
    routes = {"/tickets": [[{**ticket, "id": 1}, {**ticket, "id": 2}]], "/agents/9": {"contact": {"name": "Meera"}},
              "/tickets/1/conversations": [[]], "/tickets/2/conversations": [[]]}
    tickets, _ = drain(make("freshdesk", routes, FD, FD_SECRET)[0])
    assert [t["agent"] for t in tickets] == ["Meera", "Meera"]


def test_rich_text_line_breaks_become_spaces():
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "line one"}, {"type": "hardBreak"},
                                                                         {"type": "text", "text": "line two"}]}]}
    assert " ".join(adf_text(doc).split()) == "line one line two"


def test_tickets_with_unreadable_dates_are_skipped_not_fatal():
    routes = zendesk_routes()
    routes["/incremental/tickets/cursor.json"] = {"tickets": [
        {"id": 1, "subject": "bad dates", "status": "solved", "created_at": "garbage", "updated_at": "garbage", "assignee_id": 77, "requester_id": 5},
        {"id": 3, "subject": "fine", "status": "closed", "created_at": "2026-06-03T10:00:00Z", "updated_at": "2026-06-03T13:00:00Z", "assignee_id": 77, "requester_id": 6},
    ], "after_cursor": "c9", "end_of_stream": True}
    tickets, last = drain(make("zendesk", routes, ZD, ZD_SECRET)[0])
    assert [t["id"] for t in tickets] == ["zd-3"] and last == "c9"


def test_salesforce_records_without_a_modified_date_do_not_break_the_cursor():
    routes = salesforce_routes()
    routes["/services/data/v60.0/query"] = {"records": [case(1, LastModifiedDate=None)], "done": True}
    tickets, last = drain(make("salesforce", routes, SF, {"access_token": "t"})[0], "2026-06-01T00:00:00+00:00")
    assert [t["id"] for t in tickets] == ["sf-00001"] and last.startswith("2026-06-01T00:00:00")  # the cursor stays where it was


def test_freshdesk_without_any_updated_dates_keeps_the_previous_cursor(monkeypatch):
    monkeypatch.setattr(freshdesk, "PAGE", 5)
    routes = {"/tickets": [[{"id": 1, "subject": "s", "status": 4, "created_at": "2026-06-01T10:00:00Z", "responder_id": None,
                              "stats": {"resolved_at": "2026-06-01T11:00:00Z"}}]],
              "/tickets/1/conversations": [[]]}
    tickets, last = drain(make("freshdesk", routes, FD, FD_SECRET)[0], "2026-06-01T00:00:00+00:00")
    assert len(tickets) == 1 and last.startswith("2026-06-01T00:00:00")


def test_a_worker_that_loses_a_race_for_one_connector_takes_the_next(tmp_path):
    """Another worker leases the first candidate after this one listed it: this one must move on, not fail."""
    engine = make_engine(db_url(tmp_path, 'race'))
    schema.migrate(engine, NOW.isoformat())
    repo = ConnectorRepo(engine)
    ids = [repo.create("acme", "zendesk", f"c{i}", {}, "", {}, 30, True, (NOW - timedelta(minutes=10 - i)).isoformat())["id"] for i in range(2)]
    stolen = []

    @sa.event.listens_for(engine, "after_cursor_execute")
    def steal(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT connectors.id") and not stolen:
            stolen.append(ids[0])
            repo.lease(ids[0], NOW, 900)  # the other worker wins the first connector

    claimed = repo.claim_due(NOW, 900)
    assert stolen == [ids[0]] and claimed["id"] == ids[1]
