import threading
from datetime import timedelta

import pytest

from backend.classify import Classifier, extract_fixes, rule_scores, ticket_text
from backend.config import Settings
from backend.llm import LLMUnavailable
from backend.insights_ai import InsightSuggester, rule_suggestions, validate_llm_suggestions
from backend.report import build_report, deterministic_summary, parse_time, render_markdown
from backend.resilience import SingleFlight, TTLCache
from backend.support import normalise_ticket
from tests.conftest import NOW, FakeLLM

CLEAR = {"subject": "Cannot log in", "description": "Locked out, password rejected at login.", "resolution": "Reset the password."}
VAGUE = {"subject": "Strange behaviour", "description": "Something is different.", "resolution": "Looked at it."}
SLOW_BUT_LOGIN = {"subject": "slow login", "description": "slow", "resolution": "ok"}  # 1 Performance hit... ambiguous


def classifier(llm, votes=3):
    return Classifier(llm, TTLCache(), SingleFlight(), Settings(votes=votes, vote_temperature=0.7))


# ---------- classification: deterministic ----------
def test_rules_decide_clear_tickets_without_calling_the_llm():
    llm = FakeLLM(lambda *a: {"category": "Performance"})
    result = classifier(llm).classify(CLEAR)
    assert result["category"] == "Login & access" and result["method"] == "rules" and result["confidence"] > 0.5
    assert llm.calls == []


def test_ticket_text_and_scores():
    assert ticket_text({"subject": "A", "resolution": "B"}) == "a  b"
    assert rule_scores("password login")["Login & access"] == 2


def test_extract_fixes_actions_and_tools():
    fixes = extract_fixes({"resolution": "Restarted the service, cleared the cache and rolled back.", "tools_used": [" SSH ", "ssh", "", "Grafana"]})
    assert fixes["actions"] == ["Restarted service", "Cleared cache", "Rolled back release"]
    assert fixes["tools"] == ["grafana", "ssh"]
    assert extract_fixes({}) == {"actions": [], "tools": []}


# ---------- classification: probabilistic ----------
def test_llm_majority_vote_and_confidence():
    answers = iter(["Performance", "Performance", "Billing & payments"])
    llm = FakeLLM(lambda *a: {"category": next(answers)})
    result = classifier(llm).classify(VAGUE)
    assert result == {"category": "Performance", "confidence": 0.67, "method": "llm-vote"}
    assert len(llm.calls) == 3 and all(c["temperature"] == 0.7 for c in llm.calls)


def test_invalid_llm_categories_are_ignored():
    answers = iter(["Made Up", "Performance", "Made Up"])
    result = classifier(FakeLLM(lambda *a: {"category": next(answers)})).classify(VAGUE)
    assert result["category"] == "Performance" and result["confidence"] == 1.0


def test_all_invalid_votes_fall_back_to_rules_then_unclassified():
    llm = FakeLLM(lambda *a: {"category": "nonsense"})
    assert classifier(llm).classify(VAGUE)["method"] == "unclassified"
    one_hit = {"subject": "slow", "description": "", "resolution": ""}
    low = classifier(llm).classify(one_hit)
    assert low["method"] == "rules-low-confidence" and low["category"] == "Performance"


def test_llm_outage_degrades_gracefully_and_is_not_cached():
    outage = FakeLLM(None)
    c = classifier(outage)
    assert c.classify(VAGUE)["method"] == "unclassified"
    assert len(outage.calls) == 1  # fail fast: no further votes after the first outage
    outage.script = lambda *a: {"category": "Performance"}
    assert c.classify(VAGUE)["method"] == "llm-vote"  # recovered, because the fallback was never cached


def test_trusted_results_are_cached():
    llm = FakeLLM(lambda *a: {"category": "Performance"})
    c = classifier(llm)
    first, second = c.classify(VAGUE), c.classify(VAGUE)
    assert first == second and len(llm.calls) == 3


def test_concurrent_identical_requests_make_one_set_of_llm_calls():
    gate = threading.Event()

    def slow(*_):
        gate.wait(2)
        return {"category": "Performance"}

    llm = FakeLLM(slow)
    c = classifier(llm)
    results = []
    threads = [threading.Thread(target=lambda: results.append(c.classify(VAGUE))) for _ in range(8)]
    for t in threads:
        t.start()
    while not llm.calls:
        pass
    gate.set()
    for t in threads:
        t.join()
    assert len(results) == 8 and all(r["category"] == "Performance" for r in results)
    assert len(llm.calls) == 3  # one leader did the voting; everyone else shared it


def test_leader_rechecks_cache_before_calling_llm():
    llm = FakeLLM(lambda *a: {"category": "Performance"})
    c = classifier(llm)
    c.cache.set("k", {"category": "Billing & payments", "confidence": 1.0, "method": "llm-vote"})
    assert c._resolve_ambiguous("k", "text", "Performance", 1, 0)["category"] == "Billing & payments"
    assert llm.calls == []


# ---------- insights: deterministic baseline + validation ----------
def hl(i, tags, note="n1", text=None):
    return {"id": i, "noteId": note, "text": text or f"quote {i}", "tags": tags}


def test_rule_suggestions_group_recurring_tags():
    hs = [hl("a", ["pricing", "x"]), hl("b", ["pricing"], note="n2"), hl("c", ["x"]), hl("d", ["solo"])]
    out = rule_suggestions(hs)
    assert [s["title"] for s in out] == ["Recurring theme: pricing", "Recurring theme: x"]
    assert out[0]["highlightIds"] == ["a", "b"] and "2 note(s)" in out[0]["description"] and out[0]["confidence"] == 0.4


def test_validate_llm_suggestions_drops_bad_items_and_hallucinated_ids():
    known = {"a", "b"}
    raw = [
        {"title": "Good", "description": " d ", "highlightIds": ["a", "a", "ghost"]},
        {"title": "  ", "highlightIds": ["a"]},
        {"title": "No ids"},
        {"title": "Only ghosts", "highlightIds": ["ghost"]},
        "not a dict",
    ]
    out = validate_llm_suggestions(raw, known)
    assert out == [{"title": "Good", "description": "d", "highlightIds": ["a"], "confidence": 0.5}]
    assert validate_llm_suggestions("nope", known) == [] and validate_llm_suggestions(None, known) == []


def suggester(llm):
    return InsightSuggester(llm, TTLCache(), SingleFlight())


def test_suggester_uses_llm_and_caches():
    llm = FakeLLM(lambda *a: {"insights": [{"title": "T", "description": "D", "highlightIds": ["a", "b"]}]})
    s = suggester(llm)
    hs = [hl("a", ["x"]), hl("b", ["x"])]
    first = s.suggest(hs)
    assert first["method"] == "llm" and first["model"] == "fake/model" and first["suggestions"][0]["confidence"] == 1.0
    assert s.suggest(hs) == first and len(llm.calls) == 1


def test_suggester_falls_back_when_llm_unavailable_or_useless():
    hs = [hl("a", ["x"]), hl("b", ["x"])]
    out = suggester(FakeLLM(None)).suggest(hs)
    assert out["method"] == "rules" and "scripted outage" in out["fallbackReason"] and out["suggestions"]
    useless = suggester(FakeLLM(lambda *a: {"insights": [{"title": "T", "highlightIds": ["ghost"]}]})).suggest(hs)
    assert useless["method"] == "rules" and useless["fallbackReason"] == "LLM returned no valid insights"
    assert suggester(FakeLLM(None)).suggest([]) == {"method": "rules", "suggestions": []}


def test_suggester_caps_input_and_rechecks_cache_in_leader():
    llm = FakeLLM(lambda *a: {"insights": []})
    s = suggester(llm)
    s.suggest([hl(str(i), ["t"]) for i in range(100)])
    assert len(llm.calls[0]["user"]) > 0 and llm.calls[0]["user"].count('"id"') == 60
    s.cache.set("k", {"method": "llm", "suggestions": []})
    assert s._build("k", [], []) == {"method": "llm", "suggestions": []}


# ---------- tickets ----------
GOOD = {"id": "X-1", "subject": "S", "createdAt": "2026-06-14T10:00:00Z", "resolvedAt": "2026-06-14T12:00:00+00:00",
        "resolution": "Restarted.", "tools_used": ["ssh"], "agent": "Asha"}


def test_normalise_ticket_validation_and_defaults():
    uid = lambda: "abc"
    t = normalise_ticket({**GOOD, "id": None, "agent": None, "status": None, "tools_used": "ssh"}, uid)
    assert t["id"] == "T-abc" and t["agent"] == "unknown" and t["status"] == "resolved" and t["tools_used"] == []
    for bad, msg in [("x", "must be an object"), ({**GOOD, "subject": " "}, "subject is required"),
                     ({**GOOD, "createdAt": "nope"}, "ISO-8601"), ({**GOOD, "resolvedAt": None}, "ISO-8601"),
                     ({**GOOD, "resolvedAt": "2026-06-14T09:00:00Z"}, "must not be before")]:
        with pytest.raises(ValueError, match=msg):
            normalise_ticket(bad, uid)


def test_ticket_endpoints_upsert_and_validate(make_client):
    c = make_client()
    assert c.post("/api/tickets", json={**GOOD, "subject": ""}).status_code == 400
    assert c.post("/api/tickets", json=GOOD).get_json() == {"saved": 1}
    assert c.post("/api/tickets", json=[GOOD, {**GOOD, "id": "X-2"}]).get_json() == {"saved": 2}
    assert sorted(t["id"] for t in c.get("/api/tickets").get_json()) == ["X-1", "X-2"]  # X-1 not duplicated


def test_sample_loader_is_idempotent_and_relative_to_clock(make_client):
    c = make_client()
    assert c.post("/api/tickets/sample").get_json() == {"loaded": 30}
    c.post("/api/tickets/sample")
    tickets = c.get("/api/tickets").get_json()
    assert len(tickets) == 30
    assert max(parse_time(t["resolvedAt"]) for t in tickets) <= NOW


# ---------- report ----------
def test_report_validates_days_and_filters_window(make_client):
    c = make_client()
    c.post("/api/tickets/sample")
    for bad in ("0", "abc", "31", "-7"):
        assert c.get(f"/api/report?days={bad}").status_code == 400
    r7, r30, r90 = (c.get(f"/api/report?days={d}").get_json() for d in (7, 30, 90))
    assert r7["totalTickets"] < r30["totalTickets"] < r90["totalTickets"] == 30
    assert c.get("/api/report").get_json()["days"] == 30  # default
    login = next(x for x in r30["categories"] if x["category"] == "Login & access")
    assert login["topActions"][0]["name"] == "Reset password / credentials" and login["topTools"][0]["name"] == "admin console"
    assert sum(a["count"] for a in r30["byAgent"]) == r30["totalTickets"]
    assert "Uncategorised" in {x["category"] for x in r90["categories"]} and r90["lowConfidence"] >= 1


def test_report_uses_llm_for_ambiguous_tickets_and_reuses_cache(make_client):
    llm = FakeLLM(lambda *a: {"category": "Performance"})
    c = make_client(llm=llm)
    c.post("/api/tickets/sample")
    first = c.get("/api/report?days=90").get_json()
    n = len(llm.calls)
    assert n > 0 and first["lowConfidence"] == 0
    c.get("/api/report?days=90")
    assert len(llm.calls) == n  # second report is served from cache


def test_empty_report_and_markdown(make_client):
    c = make_client()
    r = c.get("/api/report?days=7").get_json()
    assert r["totalTickets"] == 0 and r["summary"] == "No tickets were resolved in the last 7 days."
    assert "_No resolved tickets" in render_markdown("T", r, r["summary"])


def test_summary_without_recorded_actions_and_unresolved_tickets_are_excluded():
    tickets = [{"id": "1", "subject": "slow", "status": "resolved", "resolution": "Looked", "createdAt": "2026-06-14T00:00:00+00:00",
                "resolvedAt": "2026-06-14T02:00:00+00:00", "agent": "A"},
               {"id": "2", "subject": "x", "status": "open", "createdAt": "2026-06-14T00:00:00+00:00", "resolvedAt": "2026-06-14T02:00:00+00:00"}]
    report = build_report(tickets, 7, NOW, lambda t: {"category": "Performance", "confidence": 0.5, "method": "rules"})
    assert report["totalTickets"] == 1 and "manual investigation" in deterministic_summary(report)
    md = render_markdown("T", report, "s")
    assert "none recorded" in md and "(1 ticket, 100%)" in md  # singular form
    assert build_report(tickets, 7, NOW + timedelta(days=30), lambda t: {})["totalTickets"] == 0


# ---------- documents + mock approval ----------
def make_doc(c, **body):
    c.post("/api/tickets/sample")
    return c.post("/api/reports", json=body).get_json()


def test_create_document_validates_and_renders(make_client):
    c = make_client()
    assert c.post("/api/reports", json={"days": 5}).status_code == 400
    doc = make_doc(c, days=30, title="My report")
    assert doc["status"] == "draft" and doc["title"] == "My report" and doc["summaryMethod"] == "rules"
    assert "## Issues diagnosed and how they were fixed" in doc["markdown"] and "Reset password / credentials" in doc["markdown"]
    assert c.post("/api/reports").get_json()["days"] == 30 and c.post("/api/reports").get_json()["title"].startswith("Support resolution report")
    assert len(c.get("/api/reports").get_json()) == 3


def test_document_summary_comes_from_llm_when_available(make_client):
    def script(system, user, temperature):
        return {"summary": "LLM executive summary."} if "executive summaries" in system else {"category": "Performance"}

    llm = FakeLLM(script)
    c = make_client(llm=llm)
    doc = make_doc(c, days=30)
    assert doc["summaryMethod"] == "llm" and "LLM executive summary." in doc["markdown"]
    n = len(llm.calls)
    c.post("/api/reports", json={"days": 30})
    assert len(llm.calls) == n  # narrative cached


def narrative_llm(on_summary):
    def script(system, user, temperature):
        return on_summary() if "executive summaries" in system else {"category": "Performance"}

    return FakeLLM(script)


def _down():
    raise LLMUnavailable("down")


@pytest.mark.parametrize("on_summary", [_down, lambda: {"summary": "  "}])
def test_document_summary_falls_back_when_llm_fails_or_is_empty(make_client, on_summary):
    doc = make_doc(make_client(llm=narrative_llm(on_summary)), days=30)
    assert doc["summaryMethod"] == "rules"


def test_approval_workflow_happy_path_and_rejection_loop(make_client):
    c = make_client()
    doc = make_doc(c, days=30)
    rid = doc["id"]
    assert c.post(f"/api/reports/{rid}/decision", json={"decision": "approve", "reviewer": "Boss"}).status_code == 409  # not submitted yet
    submitted = c.post(f"/api/reports/{rid}/submit", json={"approver": "lead@example.com"}).get_json()
    assert submitted["status"] == "pending_approval" and "MOCK" in submitted["events"][-1]["detail"] and "lead@example.com" in submitted["events"][-1]["detail"]
    assert c.post(f"/api/reports/{rid}/submit").status_code == 409  # already pending
    rejected = c.post(f"/api/reports/{rid}/decision", json={"decision": "reject", "reviewer": "Boss", "comment": "Add trend data"}).get_json()
    assert rejected["status"] == "rejected" and rejected["events"][-1]["detail"] == "Boss: Add trend data"
    again = c.post(f"/api/reports/{rid}/submit").get_json()  # defaults to the placeholder approver
    assert again["status"] == "pending_approval" and "approver@example.com" in again["events"][-1]["detail"]
    approved = c.post(f"/api/reports/{rid}/decision", json={"decision": "approve", "reviewer": "Boss"}).get_json()
    assert approved["status"] == "approved" and approved["events"][-1]["detail"] == "Boss"
    assert c.post(f"/api/reports/{rid}/submit").status_code == 409  # approved is final


def test_approval_validation_and_not_found(make_client):
    c = make_client()
    rid = make_doc(c, days=7)["id"]
    c.post(f"/api/reports/{rid}/submit")
    assert c.post(f"/api/reports/{rid}/decision", json={"decision": "maybe", "reviewer": "x"}).status_code == 400
    assert c.post(f"/api/reports/{rid}/decision", json={"decision": "approve", "reviewer": " "}).status_code == 400
    assert c.post("/api/reports/nope/submit").status_code == 404
    assert c.post("/api/reports/nope/decision", json={"decision": "approve", "reviewer": "x"}).status_code == 404


def test_concurrent_approve_and_reject_exactly_one_wins(make_client):
    c = make_client()
    rid = make_doc(c, days=30)["id"]
    c.post(f"/api/reports/{rid}/submit")
    barrier, codes = threading.Barrier(8), []

    def act(decision):
        barrier.wait()
        codes.append(c.post(f"/api/reports/{rid}/decision", json={"decision": decision, "reviewer": "r"}).status_code)

    threads = [threading.Thread(target=act, args=("approve" if i % 2 else "reject",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] + [409] * 7
    doc = next(r for r in c.get("/api/reports").get_json() if r["id"] == rid)
    assert doc["status"] in ("approved", "rejected") and sum(e["type"] in ("approved", "rejected") for e in doc["events"]) == 1
