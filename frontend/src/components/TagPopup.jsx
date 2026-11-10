"use client";
import { useState } from "react";
import { api } from "../api";

// Floating box shown after the user selects text: collects tags and saves the highlight.
export default function TagPopup({ noteId, pending, onSaved, onCancel }) {
  const [tags, setTags] = useState("");

  const save = async () => {
    try {
      await api("highlights", "POST", { noteId, start: pending.start, end: pending.end, tags: tags.split(",") });
      window.getSelection().removeAllRanges();
      onSaved();
    } catch (e) {
      window.alert(e.message);
    }
  };

  return (
    <div id="popup" style={{ left: pending.left, top: pending.top }}>
      <div className="muted" id="popQuote">{pending.quote.slice(0, 80)}</div>
      <input
        id="popTags"
        autoFocus
        value={tags}
        placeholder="tags, comma separated (e.g. pricing, onboarding)"
        onChange={(e) => setTags(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && save()}
      />
      <div className="row">
        <button className="primary" id="popSave" onClick={save}>Highlight</button>
        <button id="popCancel" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
