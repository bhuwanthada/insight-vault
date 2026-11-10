"use client";
import { useState } from "react";
import { api } from "../api";

// Form for adding a note: paste text or load a transcript file.
export default function NoteForm({ onSaved }) {
  const [title, setTitle] = useState("");
  const [source, setSource] = useState("interview");
  const [text, setText] = useState("");

  const loadFile = async (e) => {
    const file = e.target.files[0];
    setText(await file.text());
    setTitle((current) => current || file.name.replace(/\.[^.]+$/, ""));
  };

  const save = async () => {
    try {
      onSaved(await api("notes", "POST", { title, source, text }));
    } catch (e) {
      window.alert(e.message);
    }
  };

  return (
    <div className="card stack">
      <h2>New note</h2>
      <input placeholder="Title (e.g. Interview with Priya, 12 Nov)" value={title} onChange={(e) => setTitle(e.target.value)} />
      <input placeholder="Source (interview, survey, support ticket...)" value={source} onChange={(e) => setSource(e.target.value)} />
      <textarea rows={14} placeholder="Paste a transcript or notes, or load a .txt/.md file below" value={text} onChange={(e) => setText(e.target.value)} />
      <input type="file" accept=".txt,.md,.vtt,.srt,.csv" onChange={loadFile} />
      <button className="primary" onClick={save}>Save note</button>
    </div>
  );
}
