"use client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";

const TEXT_FIELDS = [
  ["app_name", "Application name"], ["accent_color", "Accent color (#rrggbb)"], ["output_language", "Language for summaries"],
  ["llm_provider", "LLM provider (litellm, openrouter, groq, openai_compatible, none)"], ["llm_model", "LLM model"], ["llm_base_url", "LLM base URL (optional)"],
];
const LONG_FIELDS = [["rules_yaml", "Issue categories and fix patterns (YAML)"], ["report_template_text", "Report layout (Markdown with $title, $summary, ...)"]];

// An organization administrator's own settings: look and feel, LLM choice, rules, report layout and the LLM key.
export default function OrgSettings() {
  const [data, setData] = useState(null);
  const [form, setForm] = useState({});
  const [apiKey, setApiKey] = useState("");
  const [saved, setSaved] = useState(false);

  const load = async () => {
    const loaded = await api("org/settings");
    setData(loaded);
    setForm({ ...loaded.overrides }); // only what the organization set itself: defaults are shown as hints, never frozen
  };
  useEffect(() => {
    guarded(load)();
  }, []);

  const change = (name) => (e) => {
    setSaved(false);
    setForm({ ...form, [name]: e.target.value });
  };

  const save = guarded(async () => {
    const overrides = { ...data.overrides };
    for (const [name] of [...TEXT_FIELDS, ...LONG_FIELDS]) {
      const value = String(form[name] ?? "").trim();
      if (value) overrides[name] = value;
      else delete overrides[name];
    }
    await api("org/settings", "PUT", overrides);
    if (apiKey.trim()) await api("org/secrets", "PUT", { llm_api_key: apiKey });
    setApiKey("");
    await load();
    setSaved(true);
  });

  const removeKey = guarded(async () => {
    await api("org/secrets", "PUT", { llm_api_key: "" });
    await load();
  });

  if (!data) return null;
  return (
    <div className="card stack" id="org-settings">
      <h2>Organization settings</h2>
      {TEXT_FIELDS.map(([name, label]) => (
        <label key={name}>{label}<input value={form[name] ?? ""} placeholder={String(data.effective[name] ?? "")} onChange={change(name)} /></label>
      ))}
      {LONG_FIELDS.map(([name, label]) => (
        <label key={name}>{label}<textarea rows={5} value={form[name] ?? ""} onChange={change(name)} /></label>
      ))}
      <label>
        LLM API key (stored encrypted, never shown again)
        <input type="password" placeholder={data.hasLlmKey ? "a key is set; type to replace it" : "not set"} value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
      </label>
      <div className="row">
        <button className="primary" onClick={save}>Save settings</button>
        {data.hasLlmKey && <button className="danger" onClick={removeKey}>Remove LLM key</button>}
        {saved && <span className="ok" role="status">Saved</span>}
      </div>
    </div>
  );
}
