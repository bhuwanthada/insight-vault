"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

const WINDOWS = [7, 14, 30, 60, 90];
const STATUS_LABEL = { draft: "Draft", pending_approval: "Pending approval", approved: "Approved", rejected: "Rejected" };

const fail = (e) => window.alert(e.message);

function Bar({ share }) {
  return (
    <div className="bar" aria-hidden="true">
      <div style={{ width: `${Math.round(share * 100)}%` }} />
    </div>
  );
}

// "What issues did we diagnose in the last X days, and how did agents fix them?"
export default function SupportTab() {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState(null);
  const [docs, setDocs] = useState([]);
  const [reviewer, setReviewer] = useState("");
  const [open, setOpen] = useState(null);

  const loadReport = useCallback(() => api("report?days=" + days).then(setReport).catch(fail), [days]);
  const loadDocs = useCallback(() => api("reports").then(setDocs).catch(fail), []);

  useEffect(() => {
    loadReport();
  }, [loadReport]);
  useEffect(() => {
    loadDocs();
  }, [loadDocs]);

  const run = (action) => async () => {
    try {
      await action();
    } catch (e) {
      fail(e);
    }
  };

  const loadSample = run(async () => {
    await api("tickets/sample", "POST");
    await loadReport();
  });
  const generate = run(async () => {
    await api("reports", "POST", { days });
    await loadDocs();
  });
  const submit = (id) => run(async () => {
    await api(`reports/${id}/submit`, "POST", {});
    await loadDocs();
  });
  const decide = (id, decision) => run(async () => {
    await api(`reports/${id}/decision`, "POST", { decision, reviewer });
    await loadDocs();
  });

  const names = (list) => list.map((x) => `${x.name} (${x.count})`).join(", ") || "none recorded";

  return (
    <>
      <div className="card stack">
        <div className="toolbar">
          <h2 style={{ margin: 0 }}>Support resolution report</h2>
          <label>
            Time window{" "}
            <select aria-label="Time window" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {WINDOWS.map((d) => <option key={d} value={d}>Last {d} days</option>)}
            </select>
          </label>
          <button onClick={loadSample}>Load sample tickets</button>
          <button className="primary" onClick={generate}>Generate document</button>
        </div>
        {report && <p id="report-summary">{report.summary}</p>}
        {report && report.totalTickets === 0 && <div className="muted">No resolved tickets in this window. Load the sample tickets to see a demo.</div>}
        {report && report.categories.length > 0 && (
          <table>
            <thead>
              <tr><th>Issue diagnosed</th><th>Tickets</th><th>Share</th><th>Avg hours</th><th>How it was fixed</th><th>Tools used</th></tr>
            </thead>
            <tbody>
              {report.categories.map((c) => (
                <tr key={c.category}>
                  <td>{c.category}</td>
                  <td>{c.count}</td>
                  <td><Bar share={c.share} /></td>
                  <td>{c.avgResolutionHours}</td>
                  <td>{names(c.topActions)}</td>
                  <td>{names(c.topTools)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {report && report.lowConfidence > 0 && (
          <div className="muted">{report.lowConfidence} ticket(s) could not be diagnosed with confidence (AI unavailable or ambiguous).</div>
        )}
      </div>

      <div className="card stack">
        <h2>Documents and approval (mock workflow, nothing is sent)</h2>
        <input placeholder="Reviewer name (needed to approve or reject)" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
        {docs.length === 0 && <div className="muted">No documents yet. Generate one from the report above.</div>}
        {docs.map((d) => (
          <div key={d.id} className="quote">
            <strong>{d.title}</strong> <span className={"status " + d.status}>{STATUS_LABEL[d.status]}</span>
            <div className="muted">{d.events[d.events.length - 1].detail}</div>
            <div className="row">
              <button onClick={() => setOpen(open === d.id ? null : d.id)}>{open === d.id ? "Hide" : "View"}</button>
              {(d.status === "draft" || d.status === "rejected") && <button className="primary" onClick={submit(d.id)}>Send for approval (mock)</button>}
              {d.status === "pending_approval" && (
                <>
                  <button className="primary" onClick={decide(d.id, "approve")}>Approve</button>
                  <button className="danger" onClick={decide(d.id, "reject")}>Reject</button>
                </>
              )}
            </div>
            {open === d.id && <pre className="doc">{d.markdown}</pre>}
          </div>
        ))}
      </div>
    </>
  );
}
