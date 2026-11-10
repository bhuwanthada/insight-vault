// End-to-end demo: loads the sample transcript, highlights/tags quotes, builds insights, prints results.
const fs = require("node:fs");
const path = require("node:path");
const base = process.env.BASE || "http://localhost:3737/api/";
const call = async (p, m = "GET", b) => {
  const r = await fetch(base + p, { method: m, headers: { "Content-Type": "application/json" }, body: b ? JSON.stringify(b) : undefined });
  const t = await r.text();
  if (!r.ok) throw new Error(`${m} ${p} -> ${r.status} ${t}`);
  try { return JSON.parse(t); } catch { return t; }
};
(async () => {
  const text = fs.readFileSync(path.join(__dirname, "..", "samples", "interview-priya.txt"), "utf8");
  const note = await call("notes", "POST", { title: "Interview with Priya (Research Lead)", source: "interview", text });
  const quote = (needle, tags) => {
    const start = text.indexOf(needle);
    if (start < 0) throw new Error("quote not found: " + needle);
    return call("highlights", "POST", { noteId: note.id, start, end: start + needle.length, tags });
  };
  const hs = {
    mess: await quote("Finding anything from three months ago is painful.", ["pain", "findability"]),
    love: await quote("I love the highlighting and the way you can tag a quote", ["praise", "highlighting"]),
    price: await quote("It was expensive for limited user access, so only two of us got seats and the product managers were locked out.", ["pricing", "access"]),
    human: await quote("I became a human search engine.", ["access", "pain"]),
    trace: await quote("I always want to click back to the exact quote and the video moment to be sure.", ["ai", "traceability"]),
    tags: await quote("The tag taxonomy is hard to get right", ["tagging", "pain"]),
    eu: await quote("a tool that keeps data in the EU or on our own machines would be easier to approve", ["privacy", "data-residency"]),
  };
  await call("insights", "POST", { title: "Per-seat pricing locks stakeholders out of research", description: "Limited seats push researchers to act as human search engines for PMs.", highlightIds: [hs.price.id, hs.human.id] });
  await call("insights", "POST", { title: "Trust in AI depends on traceability to source", description: "Summaries save time but are only trusted when linked to the exact quote and clip.", highlightIds: [hs.trace.id] });
  await call("insights", "POST", { title: "Data residency affects tool approval", description: "Legal teams prefer EU-hosted or local storage.", highlightIds: [hs.eu.id] });

  const analysis = await call(`notes/${note.id}/analysis`);
  const search = await call("search?q=pricing");
  const state = await call("state");
  console.log("NOTE:", note.title, `(${text.length} chars)`);
  console.log("HIGHLIGHTS:", state.highlights.length, "| INSIGHTS:", state.insights.length);
  console.log("KEYWORDS:", analysis.keywords.join(", "));
  console.log("SENTIMENT:", analysis.sentiment, `(+${analysis.positive}/-${analysis.negative})`);
  console.log("SUMMARY:\n - " + analysis.summary.join("\n - "));
  console.log("SEARCH 'pricing':", search.length, "hits ->", search.map((s) => s.type).join(", "));
  // verify every stored highlight text still matches the transcript at its offsets
  const bad = state.highlights.filter((h) => text.slice(h.start, h.end) !== h.text);
  console.log("OFFSET INTEGRITY:", bad.length === 0 ? "OK" : "BROKEN " + bad.length);
  console.log("\n===== EXPORT (markdown) =====\n" + (await call("export")));
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
