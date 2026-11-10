"""Generate docs/sample-support-report.md: a real report built from the sample tickets and sent
through the mock approval workflow (nothing is emailed; the approval is recorded locally)."""
import os
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)  # fixed so the committed sample is reproducible
DAYS = 30

with tempfile.TemporaryDirectory() as tmp:
    client = create_app(os.path.join(tmp, "db.json"), settings=Settings(), clock=lambda: NOW).test_client()
    client.post("/api/tickets/sample")
    doc = client.post("/api/reports", json={"days": DAYS, "title": f"Support resolution report: last {DAYS} days"}).get_json()
    doc = client.post(f"/api/reports/{doc['id']}/submit", json={"approver": "support-lead@example.com"}).get_json()

trail = "\n".join(f"| {e['type']} | {e['detail']} |" for e in doc["events"])
body = doc["markdown"].split("## Approval")[0].rstrip()
out = f"""{body}

## Approval (mock)

**Status:** {doc['status'].replace('_', ' ')}  
**Requested approver:** support-lead@example.com (placeholder address; no message was sent)

| Event | Detail |
|---|---|
{trail}

> Sample document generated from `samples/support_tickets.json` by `scripts/make_sample_report.py`.
> All tickets, agents and customers in the sample data are fictional.
"""
target = os.path.join(ROOT, "docs", "sample-support-report.md")
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target, "w", encoding="utf-8") as handle:
    handle.write(out)
print("wrote", target, f"({len(out)} chars, status={doc['status']})")
