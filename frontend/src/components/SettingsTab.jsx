"use client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";
import Members from "./Members";
import OrgPanel from "./OrgPanel";
import OrgSettings from "./OrgSettings";

// Administrator view: LLM connection and usage, recent activity, and the active (masked) settings.
export default function SettingsTab({ locale, multi }) {
  const [status, setStatus] = useState(null);
  const [settings, setSettings] = useState(null);
  const [audit, setAudit] = useState(null); // null until loaded, so we never flash an empty state
  const [result, setResult] = useState(null);

  useEffect(() => {
    guarded(async () => {
      const [s, c, a] = await Promise.all([api("llm/status"), api("config"), api("audit?limit=50")]);
      setStatus(s);
      setSettings(c);
      setAudit(a);
    })();
  }, []);

  const test = guarded(async () => setResult(await api("llm/test", "POST", {})));
  const open = status && Object.entries(status.circuits).filter(([, isOpen]) => isOpen).map(([name]) => name);

  return (
    <>
      <OrgPanel locale={locale} />
      {multi && <OrgSettings />}
      {multi && <Members />}
      <div className="card stack">
        <div className="toolbar">
          <h2 style={{ margin: 0 }}>LLM connection</h2>
          <button className="primary" onClick={test}>Test LLM connection</button>
        </div>
        {status && (
          <table>
            <tbody>
              <tr><th>Provider</th><td>{status.provider}</td></tr>
              <tr><th>Model</th><td>{status.model || "not configured"}</td></tr>
              <tr><th>Fallbacks</th><td>{status.fallbacks.join(", ") || "none"}</td></tr>
              <tr><th>Personal data masking</th><td>{status.piiMasking ? "on" : "off"}</td></tr>
              <tr><th>Tokens today</th><td>{status.usage.tokens} / {status.usage.cap || "unlimited"} ({status.usage.calls} calls)</td></tr>
              <tr><th>Circuit open</th><td>{open.join(", ") || "none"}</td></tr>
            </tbody>
          </table>
        )}
        {result && (
          <div id="llm-test-result" className={result.ok ? "ok" : "alert"} role="status">
            {result.ok ? `Connected to ${result.provider}:${result.model} in ${result.latencyMs} ms` : `Connection failed: ${result.error}`}
          </div>
        )}
      </div>

      <div className="card stack">
        <h2>Recent activity</h2>
        {audit && audit.length === 0 && <div className="muted">Nothing recorded yet.</div>}
        {audit && audit.length > 0 && (
          <table>
            <thead><tr><th>When</th><th>Who</th><th>What</th><th>Detail</th></tr></thead>
            <tbody>
              {audit.map((e, i) => (
                <tr key={i}><td>{new Date(e.at).toLocaleString(locale)}</td><td>{e.actor}</td><td>{e.action}</td><td>{e.detail}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {settings && (
        <details className="card">
          <summary>All active settings (secrets are hidden)</summary>
          <table>
            <tbody>
              {Object.entries(settings).map(([key, value]) => (
                <tr key={key}><th>{key}</th><td>{Array.isArray(value) ? value.join(", ") || "empty" : String(value) || "empty"}</td></tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </>
  );
}
