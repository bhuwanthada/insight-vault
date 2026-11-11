"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";

const MAPPING = [["resolution_field", "Where the fix is written (custom field; default: the agent's last reply)"],
                 ["tools_field", "Custom field that lists the tools used (optional)"], ["tools_tag_prefix", "Tag prefix naming tools (default tool:)"]];

// Connect ticketing tools (Zendesk, Freshdesk, Jira Service Management, ServiceNow, Salesforce) and watch their syncs.
export default function IntegrationsTab({ locale }) {
  const [data, setData] = useState(null);
  const [problem, setProblem] = useState("");
  const [kind, setKind] = useState("");
  const [values, setValues] = useState({});
  const [name, setName] = useState("");
  const [interval, setInterval] = useState("60");
  const [notes, setNotes] = useState({});

  const load = useCallback(async () => {
    try {
      const loaded = await api("connectors");
      setData(loaded);
      setKind((current) => current || loaded.kinds[0].kind);
    } catch (e) {
      setProblem(e.message);
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  if (problem) return <div className="card alert" role="alert">{problem}</div>;
  if (!data) return null;

  const described = data.kinds.find((k) => k.kind === kind);
  const set = (field) => (e) => setValues({ ...values, [field]: e.target.value });
  const note = (id, text) => setNotes((n) => ({ ...n, [id]: text }));

  const create = guarded(async () => {
    const pick = (fields) => Object.fromEntries(fields.filter((f) => values[f.name]).map((f) => [f.name, values[f.name]]));
    const mapping = Object.fromEntries(MAPPING.filter(([k]) => values[k]).map(([k]) => [k, values[k]]));
    await api("connectors", "POST", {
      kind, name, config: pick(described.config), secrets: pick(described.secrets), mapping, intervalMinutes: Number(interval),
    });
    setValues({});
    setName("");
    await load();
  });

  const test = (c) => guarded(async () => {
    const r = await api(`connectors/${c.id}/test`, "POST", {});
    note(c.id, r.ok ? `OK: ${r.detail}` : `Failed: ${r.error}`);
  });
  const sync = (c) => guarded(async () => {
    const r = await api(`connectors/${c.id}/sync`, "POST", {});
    note(c.id, r.status === "ok" ? `Synced ${r.fetched} tickets${r.more ? " (more to come)" : ""}` : `Sync failed: ${r.error}`);
    await load();
  });
  const remove = (c) => guarded(async () => {
    if (!window.confirm(`Disconnect ${c.name}? Tickets already imported stay.`)) return;
    await api("connectors/" + c.id, "DELETE");
    await load();
  });

  return (
    <>
      <div className="card stack" id="connectors">
        <h2>Connected tools</h2>
        {data.connectors.length === 0 && <div className="muted">Nothing connected yet. Add a tool below, or import a CSV on the Support report tab.</div>}
        {data.connectors.map((c) => (
          <div key={c.id} className="quote">
            <strong>{c.name}</strong> <span className="muted">{c.kind}, every {c.interval_minutes} min{c.enabled ? "" : ", paused"}</span>
            <div className="muted">
              {c.last_sync_at ? `Last sync ${new Date(c.last_sync_at).toLocaleString(locale)}: ${c.last_status}, ${c.last_count} tickets` : "Not synced yet"}
              {c.last_error ? ` (${c.last_error})` : ""}
            </div>
            {notes[c.id] && <div role="status">{notes[c.id]}</div>}
            <div className="row">
              <button onClick={test(c)}>Test</button>
              <button className="primary" onClick={sync(c)}>Sync now</button>
              <button className="danger" onClick={remove(c)}>Disconnect</button>
            </div>
          </div>
        ))}
      </div>

      <div className="card stack" id="add-connector">
        <h2>Connect a tool</h2>
        <label>Tool
          <select aria-label="Tool" value={kind} onChange={(e) => { setKind(e.target.value); setValues({}); }}>
            {data.kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
          </select>
        </label>
        <label>Name<input placeholder={described.label} value={name} onChange={(e) => setName(e.target.value)} /></label>
        {described.config.map((f) => <label key={f.name}>{f.label}{f.required ? " *" : ""}<input value={values[f.name] ?? ""} onChange={set(f.name)} /></label>)}
        {described.secrets.map((f) => <label key={f.name}>{f.label}{f.required ? " *" : ""}<input type="password" autoComplete="off" value={values[f.name] ?? ""} onChange={set(f.name)} /></label>)}
        <details>
          <summary>How fixes and tools are found (optional)</summary>
          {MAPPING.map(([k, label]) => <label key={k}>{label}<input value={values[k] ?? ""} onChange={set(k)} /></label>)}
        </details>
        <label>Sync every (minutes)<input type="number" min="5" max="1440" value={interval} onChange={(e) => setInterval(e.target.value)} /></label>
        <button className="primary" onClick={create}>Connect</button>
      </div>
    </>
  );
}
