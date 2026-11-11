"use client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";

const limit = (value) => value || "unlimited";

// The organization's plan, what it includes, and how much of each limit has been used.
export default function OrgPanel({ locale }) {
  const [org, setOrg] = useState(null);

  useEffect(() => {
    guarded(async () => setOrg(await api("org")))();
  }, []);

  if (!org) return null;
  const { usage, quotas } = org;
  return (
    <div className="card stack" id="org-panel">
      <h2>Organization</h2>
      {org.readOnly && <div className="alert" role="alert">This organization's license has expired. It is read-only until a renewal key is activated.</div>}
      <table>
        <tbody>
          <tr><th>Name</th><td>{org.name}</td></tr>
          <tr><th>Plan</th><td>{org.plan} ({org.deploymentMode === "multi_tenant" ? "SaaS" : "single company"})</td></tr>
          <tr><th>Features</th><td>{org.features.join(", ")}</td></tr>
          <tr><th>Valid until</th><td>{org.expiresAt ? new Date(org.expiresAt).toLocaleDateString(locale) : "no expiry"}</td></tr>
          <tr><th>Tickets</th><td>{usage.tickets} / {limit(quotas.tickets)}</td></tr>
          {usage.seats !== null && <tr><th>Seats</th><td>{usage.seats} / {limit(quotas.seats)}</td></tr>}
          <tr><th>Connectors</th><td>{usage.connectors} / {limit(quotas.connectors)}</td></tr>
          <tr><th>LLM tokens today</th><td>{usage.tokensToday} / {limit(quotas.llm_tokens_per_day)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}
