"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";
import InsightsTab from "./InsightsTab";
import NoteForm from "./NoteForm";
import NoteViewer from "./NoteViewer";
import SearchTab from "./SearchTab";
import SettingsTab from "./SettingsTab";
import SupportTab from "./SupportTab";

// [tab id, label, feature flag that enables it]
const TABS = [["notes", "Notes", "notes"], ["insights", "Insights", "insights"], ["search", "Search", "search"], ["support", "Support report", "support"]];
const APPROVER_ROLES = ["approver", "admin"];

export default function Workspace({ config, user, theme, onToggleTheme }) {
  const tabs = TABS.filter(([, , feature]) => config.features.includes(feature)).map(([id, label]) => [id, label]);
  if (user.role === "admin") tabs.push(["settings", "Settings"]);

  const [tab, setTab] = useState("notes");
  const [data, setData] = useState({ notes: [], highlights: [], insights: [] });
  const [noteId, setNoteId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [llm, setLlm] = useState(null);

  useEffect(() => {
    api("llm/status").then(setLlm).catch(() => setLlm(null));
  }, []);

  const load = useCallback(async (id) => {
    setData(await api("state"));
    if (!id) return setDetail(null);
    try {
      const [note, analysis] = await Promise.all([api("notes/" + id), api(`notes/${id}/analysis`)]);
      setDetail({ note, analysis });
    } catch {
      setNoteId(null);
      setDetail(null);
    }
  }, []);

  useEffect(() => {
    load(noteId);
  }, [noteId, load]);

  const reload = () => load(noteId);
  const openNote = (id) => {
    setTab("notes");
    setNoteId(id);
  };
  const signOut = guarded(async () => {
    await api("auth/logout", "POST", {});
    window.location.reload();
  });

  return (
    <>
      <header>
        <h1>{config.appName}</h1>
        <nav>
          {tabs.map(([key, label]) => (
            <button key={key} className={"tab" + (tab === key ? " active" : "")} onClick={() => setTab(key)}>{label}</button>
          ))}
        </nav>
        <span className="muted" style={{ marginLeft: "auto" }}>Your data stays on this machine.</span>
        {llm && <span className="badge" id="llm-badge">{llm.enabled ? `LLM: ${llm.provider}:${llm.model}` : "LLM off: rules only"}</span>}
        <button aria-label={`Switch theme (now ${theme})`} onClick={onToggleTheme}>Theme: {theme}</button>
        {config.authMode !== "none" && (
          <span className="badge" id="user-chip">{user.name} ({user.role}) <button className="link" onClick={signOut}>Sign out</button></span>
        )}
        <a className="btn" href="/api/export" download="insights.md">Export insights (.md)</a>
      </header>
      <main className={tab === "notes" ? "notes" : ""}>
        {tab === "notes" && (
          <>
            <div className="card">
              <h2>Research notes</h2>
              <button className="primary" style={{ width: "100%", marginBottom: 8 }} onClick={() => setNoteId(null)}>+ Add note</button>
              {data.notes.length === 0 && <div className="muted">No notes yet.</div>}
              {data.notes.map((n) => (
                <div key={n.id} className={"item" + (n.id === noteId ? " active" : "")} onClick={() => setNoteId(n.id)}>
                  <span>{n.title}</span>
                  <span className="muted">{data.highlights.filter((h) => h.noteId === n.id).length} ✦</span>
                </div>
              ))}
            </div>
            {noteId === null && <NoteForm onSaved={(note) => setNoteId(note.id)} />}
            {noteId !== null && detail && (
              <NoteViewer
                note={detail.note}
                analysis={detail.analysis}
                highlights={data.highlights.filter((h) => h.noteId === noteId)}
                reload={reload}
                onDeleted={() => setNoteId(null)}
              />
            )}
          </>
        )}
        {tab === "insights" && <InsightsTab data={data} reload={reload} openNote={openNote} ai={config.features.includes("ai_suggestions")} />}
        {tab === "search" && <SearchTab openNote={openNote} />}
        {tab === "support" && (
          <SupportTab
            windows={config.windows}
            defaultDays={config.defaultDays}
            locale={config.locale}
            canDecide={APPROVER_ROLES.includes(user.role)}
            askReviewer={config.authMode === "none"}
          />
        )}
        {tab === "settings" && <SettingsTab locale={config.locale} />}
      </main>
    </>
  );
}
