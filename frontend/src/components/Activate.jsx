"use client";
import { useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";

// Multi-tenant onboarding: paste the organization key you were given to create (or renew) your organization.
export default function Activate() {
  const [key, setKey] = useState("");
  const [done, setDone] = useState(null);

  const activate = guarded(async () => {
    const result = await api("org/activate", "POST", { key });
    setDone(result);
    setKey("");
  });

  return (
    <details className="stack">
      <summary>Activate an organization key</summary>
      <textarea rows={4} placeholder="Paste your organization key" value={key} onChange={(e) => setKey(e.target.value)} />
      <button onClick={activate}>Activate</button>
      {done && (
        <div className="ok" role="status">
          {done.created ? "Organization created" : "Key renewed"}: {done.org.name} ({done.org.plan}).
          {done.admins.length > 0 && ` Sign in with ${done.admins.join(" or ")}.`}
        </div>
      )}
    </details>
  );
}
