"use client";
import { useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";

// Bring tickets in from any tool that can export a spreadsheet. Columns are found by their usual names.
export default function CsvImport({ onImported }) {
  const [result, setResult] = useState(null);

  const choose = guarded(async (e) => {
    const file = e.target.files[0];
    const outcome = await api("tickets/import", "POST", { csv: await file.text() });
    setResult(outcome);
    onImported();
  });

  return (
    <div className="card stack" id="csv-import">
      <h2>Import tickets from a spreadsheet (CSV)</h2>
      <div className="muted">Needs a subject, a created date and a resolved date. Resolution, agent and tools columns are used when present.</div>
      <input type="file" accept=".csv,.tsv,.txt" aria-label="CSV file" onChange={choose} />
      {result && (
        <div role="status">
          Imported {result.imported} tickets{result.skipped ? `, skipped ${result.skipped}` : ""}.
          {result.skippedRows.map((r) => <div key={r.row} className="muted">Row {r.row}: {r.reason}</div>)}
        </div>
      )}
    </div>
  );
}
