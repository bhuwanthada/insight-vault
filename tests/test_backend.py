import json
import runpy

import pytest

from backend import app as app_module
from backend.analysis import analyze
from backend.app import create_app, export_markdown
from backend.store import Store, empty_db

TRANSCRIPT = (
    "Interviewer: Tell me about onboarding?\n"
    "Priya: Onboarding is confusing and slow for new users. The search is great and easy to use.\n"
    "Priya: Pricing feels expensive for small teams.\n"
)


@pytest.fixture
def client(tmp_path):
    return create_app(str(tmp_path / "db.json")).test_client()


def add_note(client, text=TRANSCRIPT, title="Interview 1"):
    return client.post("/api/notes", json={"title": title, "text": text}).get_json()


def add_highlight(client, note, start=0, end=10, tags=None):
    return client.post(
        "/api/highlights", json={"noteId": note["id"], "start": start, "end": end, "tags": tags or []}
    ).get_json()


# ---------- analysis ----------
def test_analyze_summary_keywords_and_label_stripping():
    result = analyze(TRANSCRIPT)
    assert "priya" not in result["keywords"] and "interviewer" not in result["keywords"]
    assert "onboarding" in result["keywords"]
    assert all(not s.endswith("?") for s in result["summary"])
    assert len(result["summary"]) == 3  # the interviewer question is skipped
    assert result["positive"] == 2 and result["negative"] == 3


@pytest.mark.parametrize(
    "text,label",
    [("This is great and easy to use, love it so much.", "positive"),
     ("Slow and confusing and a real problem for us.", "negative"),
     ("The meeting happened on Tuesday afternoon.", "neutral"),
     ("", "neutral")],
)
def test_sentiment_labels(text, label):
    assert analyze(text)["sentiment"] == label


# ---------- store ----------
def test_store_missing_file_and_corrupt_file(tmp_path):
    path = tmp_path / "sub" / "db.json"
    store = Store(str(path))
    assert store.load() == empty_db()
    store.save({"notes": [1], "highlights": [], "insights": []})  # creates the folder
    assert json.loads(path.read_text())["notes"] == [1]
    path.write_text("{not json")
    assert store.load() == empty_db()


# ---------- notes ----------
def test_state_starts_empty_and_hides_note_text(client):
    assert client.get("/api/state").get_json() == {"notes": [], "highlights": [], "insights": []}
    note = add_note(client)
    listed = client.get("/api/state").get_json()["notes"][0]
    assert "text" not in listed and listed["length"] == len(TRANSCRIPT) and listed["id"] == note["id"]


def test_note_validation_and_defaults(client):
    assert client.post("/api/notes", json={"title": "x"}).status_code == 400
    assert client.post("/api/notes", data="not json", content_type="text/plain").status_code == 400
    note = client.post("/api/notes", json={"title": "t" * 300, "text": "hi", "source": "survey"}).get_json()
    assert len(note["title"]) == 200 and note["source"] == "survey"
    assert add_note(client)["source"] == "interview"


def test_get_note_and_analysis(client):
    note = add_note(client)
    assert client.get(f"/api/notes/{note['id']}").get_json()["text"] == TRANSCRIPT
    assert client.get(f"/api/notes/{note['id']}/analysis").get_json()["keywords"]
    assert client.get("/api/notes/nope").status_code == 404
    assert client.get("/api/notes/nope/analysis").status_code == 404


def test_delete_note_cascades(client):
    note = add_note(client)
    other = add_note(client, title="Other")
    h1 = add_highlight(client, note)
    h2 = add_highlight(client, other)
    client.post("/api/insights", json={"title": "I", "highlightIds": [h1["id"], h2["id"]]})
    assert client.delete(f"/api/notes/{note['id']}").get_json() == {"ok": True}
    state = client.get("/api/state").get_json()
    assert [h["id"] for h in state["highlights"]] == [h2["id"]]
    assert state["insights"][0]["highlightIds"] == [h2["id"]]
    assert client.delete(f"/api/notes/{note['id']}").status_code == 404


# ---------- highlights ----------
def test_highlight_create_and_tag_cleaning(client):
    note = add_note(client)
    h = add_highlight(client, note, 15, 25, ["Onboarding", " onboarding ", "", "UX"])
    assert h["tags"] == ["onboarding", "ux"] and h["text"] == TRANSCRIPT[15:25]
    assert add_highlight(client, note)["tags"] == []  # tags omitted -> empty


@pytest.mark.parametrize(
    "payload",
    [{"start": "a", "end": 5}, {"start": 0, "end": True}, {"start": -1, "end": 5},
     {"start": 5, "end": 5}, {"start": 0, "end": 10**6}],
)
def test_highlight_rejects_bad_ranges(client, payload):
    note = add_note(client)
    assert client.post("/api/highlights", json={"noteId": note["id"], **payload}).status_code == 400


def test_highlight_unknown_note(client):
    assert client.post("/api/highlights", json={"noteId": "x", "start": 0, "end": 1}).status_code == 404


def test_retag_and_delete_highlight(client):
    note = add_note(client)
    h = add_highlight(client, note, tags=["a"])
    assert client.patch(f"/api/highlights/{h['id']}", json={"tags": ["B", "b"]}).get_json()["tags"] == ["b"]
    assert client.patch("/api/highlights/nope", json={}).status_code == 404
    client.post("/api/insights", json={"title": "I", "highlightIds": [h["id"]]})
    assert client.delete(f"/api/highlights/{h['id']}").get_json() == {"ok": True}
    assert client.get("/api/state").get_json()["insights"][0]["highlightIds"] == []
    assert client.delete(f"/api/highlights/{h['id']}").status_code == 404


# ---------- insights ----------
def test_insight_lifecycle(client):
    note = add_note(client)
    h = add_highlight(client, note)
    assert client.post("/api/insights", json={"description": "no title"}).status_code == 400
    ins = client.post("/api/insights", json={"title": "T", "highlightIds": [h["id"], "bogus"]}).get_json()
    assert ins["highlightIds"] == [h["id"]] and ins["description"] == ""
    bare = client.post("/api/insights", json={"title": "No evidence", "description": "d"}).get_json()
    assert bare["highlightIds"] == [] and bare["description"] == "d"
    assert client.delete(f"/api/insights/{ins['id']}").get_json() == {"ok": True}
    assert client.delete(f"/api/insights/{ins['id']}").status_code == 404


# ---------- search ----------
def test_search(client):
    note = add_note(client)
    add_highlight(client, note, 15, 25, ["pricing"])
    other = add_note(client, text="nothing relevant here", title="Pricing deep dive")
    assert client.get("/api/search").get_json() == []
    assert client.get("/api/search?q=%20").get_json() == []
    by_text = client.get("/api/search?q=onboarding").get_json()
    assert by_text[0]["type"] == "note" and by_text[0]["noteId"] == note["id"]
    by_tag = client.get("/api/search?q=pricing").get_json()
    kinds = sorted((r["type"], r["noteId"]) for r in by_tag)
    assert ("highlight", note["id"]) in kinds and ("note", other["id"]) in kinds  # title-only hit
    assert client.get("/api/search?q=zzz").get_json() == []
    add_highlight(client, note, 0, 9)  # untagged highlight matched by text
    untagged = [r for r in client.get("/api/search?q=interview").get_json() if r["type"] == "highlight"]
    assert untagged[0]["title"] == "highlight"


# ---------- export ----------
def test_export_markdown(client):
    note = add_note(client)
    h = add_highlight(client, note, 15, 40, ["pain"])
    client.post("/api/insights", json={"title": "Onboarding hurts", "description": "Evidence", "highlightIds": [h["id"]]})
    response = client.get("/api/export")
    assert response.mimetype == "text/markdown"
    md = response.get_data(as_text=True)
    assert "## Onboarding hurts" in md and "[pain]" in md and "*Interview 1*" in md


def test_export_skips_dangling_highlights_and_unknown_notes():
    db = {
        "notes": [],
        "highlights": [{"id": "h1", "noteId": "gone", "text": "a\n b", "tags": []}],
        "insights": [{"title": "T", "description": "", "highlightIds": ["h1", "missing"]}],
    }
    md = export_markdown(db)
    assert "> a b" in md and "*unknown*" in md and "missing" not in md


# ---------- app wiring ----------
def test_unknown_route_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404 and response.get_json() == {"error": "not found"}


def test_default_data_file_comes_from_env(tmp_path, monkeypatch):
    target = tmp_path / "env.json"
    monkeypatch.setenv("DATA_FILE", str(target))
    c = create_app().test_client()
    add_note(c)
    assert target.exists()


def test_main_serves_with_waitress(monkeypatch):
    calls = {}
    monkeypatch.setenv("PORT", "5999")
    monkeypatch.setattr("waitress.serve", lambda app, **kw: calls.update(kw))
    app_module.main()
    assert calls == {"host": "127.0.0.1", "port": 5999, "threads": 8}


def test_python_dash_m_entrypoint(monkeypatch):
    calls = []
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr("waitress.serve", lambda app, **kw: calls.append(kw["port"]))
    runpy.run_module("backend", run_name="__main__")
    assert calls == [5001]


def test_health_headers_and_request_id(client):
    r = client.get("/api/health", headers={"X-Request-ID": "abc123"})
    assert r.get_json() == {"status": "ok"} and r.headers["X-Request-ID"] == "abc123"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert len(client.get("/api/health").headers["X-Request-ID"]) == 16  # generated


def test_llm_status(client, make_client):
    assert client.get("/api/llm/status").get_json() == {"enabled": False, "model": None, "circuitOpen": False}
    from tests.conftest import FakeLLM
    status = make_client(llm=FakeLLM(None), settings=app_module.Settings(model="fake/model")).get("/api/llm/status").get_json()
    assert status["enabled"] is True and status["model"] == "fake/model"


def test_http_errors_and_unexpected_errors_are_json(client, monkeypatch):
    assert client.delete("/api/state").get_json() == {"error": "method not allowed"}
    monkeypatch.setattr(app_module.Store, "load", lambda self: (_ for _ in ()).throw(RuntimeError("disk gone")))
    resp = client.get("/api/state")
    assert resp.status_code == 500 and resp.get_json() == {"error": "internal server error"}


def test_suggest_insights_endpoint(client):
    note = add_note(client)
    a = add_highlight(client, note, 0, 5, ["theme"])
    b = add_highlight(client, note, 6, 12, ["theme"])
    add_highlight(client, note, 13, 20, ["other"])
    everything = client.post("/api/insights/suggest", json={}).get_json()
    assert everything["method"] == "rules" and everything["suggestions"][0]["highlightIds"] == [a["id"], b["id"]]
    only_one = client.post("/api/insights/suggest", json={"highlightIds": [a["id"]]}).get_json()
    assert only_one["suggestions"] == []
