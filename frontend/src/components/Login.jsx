"use client";
import { useEffect, useState } from "react";
import { api } from "../api";

const MESSAGES = {
  denied: "Your account is not allowed to use this workspace. Ask an administrator to add your e-mail address.",
  failed: "Sign-in failed. Please try again.",
};

// Shown when the server requires authentication and nobody is signed in.
export default function Login({ config }) {
  const [info, setInfo] = useState(null);
  const error = new URLSearchParams(window.location.search).get("login_error");

  useEffect(() => {
    api("auth/providers").then(setInfo).catch(() => setInfo({ mode: config.authMode, providers: [] }));
  }, [config.authMode]);

  return (
    <main className="login">
      <div className="card stack">
        <h1>{config.appName}</h1>
        <p className="muted">Sign in to continue.</p>
        {error && <div role="alert" className="alert">{MESSAGES[error] || MESSAGES.failed}</div>}
        {info && info.mode === "apikey" && (
          <div className="muted">This server is in API-key mode. Use the API with an Authorization: Bearer key; the browser app needs single sign-on.</div>
        )}
        {info && info.mode === "sso" && info.providers.length === 0 && <div className="muted">No sign-in method is available. Contact an administrator.</div>}
        {info && info.providers.map((p) => (
          <a key={p.id} className="btn primary-link" href={`/api/auth/login/${p.id}`}>Sign in with {p.label}</a>
        ))}
      </div>
    </main>
  );
}
