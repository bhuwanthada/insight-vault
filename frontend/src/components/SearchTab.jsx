"use client";
import { useRef, useState } from "react";
import { api } from "../api";

// Search notes, quotes and tags. Only the latest request may update the results.
export default function SearchTab({ openNote }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const latest = useRef(0);

  const onChange = async (e) => {
    const q = e.target.value;
    const ticket = ++latest.current;
    setQuery(q);
    const found = q ? await api("search?q=" + encodeURIComponent(q)) : [];
    if (ticket === latest.current) setResults(found);
  };

  return (
    <div className="card stack">
      <h2>Search</h2>
      <input placeholder="Search notes, quotes and tags..." value={query} onChange={onChange} />
      <div className="stack">
        {query && results.length === 0 && <div className="muted">No results.</div>}
        {results.map((r, i) => (
          <div key={i} className="quote clickable" onClick={() => openNote(r.noteId)}>
            <strong>{r.type + ": " + r.title}</strong>
            <div>{r.snippet}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
