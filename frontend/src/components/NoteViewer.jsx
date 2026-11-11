"use client";
import { useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";
import { buildSegments, selectionRange } from "../text";
import TagPopup from "./TagPopup";

// Shows one note with its highlights; selecting text opens the tag popup.
export default function NoteViewer({ note, highlights, analysis, reload, onDeleted }) {
  const [pending, setPending] = useState(null);

  const onMouseUp = () => {
    const selection = window.getSelection();
    if (!selection.rangeCount || selection.isCollapsed) return;
    const range = selection.getRangeAt(0);
    const found = selectionRange(range, note.text);
    if (!found) return;
    const rect = range.getBoundingClientRect();
    setPending({
      ...found,
      quote: note.text.slice(found.start, found.end),
      left: Math.min(rect.left, window.innerWidth - 280),
      top: Math.min(rect.bottom + 8, window.innerHeight - 140),
    });
  };

  const saved = () => {
    setPending(null);
    reload();
  };

  const removeNote = guarded(async () => {
    if (!window.confirm("Delete this note and its highlights?")) return;
    await api("notes/" + note.id, "DELETE");
    onDeleted();
  });

  const removeHighlight = guarded(async (id) => {
    await api("highlights/" + id, "DELETE");
    reload();
  });

  return (
    <>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <strong>{note.title}</strong>
            <div className="muted">{note.source} · select any text to highlight and tag it</div>
          </div>
          <button className="danger" onClick={removeNote}>Delete</button>
        </div>
        <hr />
        <div className="text" id="textbox" onMouseUp={onMouseUp}>
          {buildSegments(note.text, highlights).map((seg) =>
            seg.highlight ? (
              <mark key={seg.start} className="hl" title={seg.highlight.tags.join(", ") || "highlight"}>
                <span data-off={seg.start}>{seg.text}</span>
              </mark>
            ) : (
              <span key={seg.start} data-off={seg.start}>{seg.text}</span>
            ),
          )}
        </div>
        {pending && <TagPopup noteId={note.id} pending={pending} onSaved={saved} onCancel={() => setPending(null)} />}
      </div>

      <div className="stack">
        <div className="card">
          <h2>Auto summary</h2>
          {analysis.summary.length ? (
            analysis.summary.map((s) => <p key={s} style={{ margin: "0 0 8px" }}>{s}</p>)
          ) : (
            <div className="muted">Not enough text to summarise.</div>
          )}
          <div>{analysis.keywords.map((k) => <span key={k} className="tag">{k}</span>)}</div>
          <div className="muted">Sentiment: {analysis.sentiment} (+{analysis.positive} / -{analysis.negative})</div>
        </div>
        <div className="card">
          <h2>Highlights ({highlights.length})</h2>
          {highlights.length === 0 && <div className="muted">None yet.</div>}
          {highlights.map((h) => (
            <div key={h.id} className="quote">
              {h.text.length > 140 ? h.text.slice(0, 140) + "…" : h.text}
              <div>
                {h.tags.map((t) => <span key={t} className="tag">{t}</span>)}
                <button className="danger" style={{ padding: "0 6px", fontSize: 12 }} onClick={() => removeHighlight(h.id)}>remove</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
