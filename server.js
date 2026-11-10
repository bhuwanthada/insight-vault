"use strict";
// Insight Vault: a local-first research repository (zero dependencies).
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { exec } = require("node:child_process");

const PORT = Number(process.env.PORT) || 3737;
const DATA_FILE = process.env.DATA_FILE || path.join(__dirname, "data", "db.json");
const PUBLIC_DIR = path.join(__dirname, "public");

const uid = () => crypto.randomBytes(6).toString("hex");

// ---------- storage ----------
function load() {
  try {
    return JSON.parse(fs.readFileSync(DATA_FILE, "utf8"));
  } catch {
    return { notes: [], highlights: [], insights: [] };
  }
}
function save(db) {
  fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
  const tmp = DATA_FILE + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(db, null, 2));
  fs.renameSync(tmp, DATA_FILE);
}

// ---------- local "AI": extractive summary, keywords, sentiment ----------
const STOP = new Set(
  "a an the and or but if then so of to in on at by for with from as is are was were be been it its this that these those i you he she we they my your our their me us them not no yes do does did have has had just very really can could would should will about into than too also like get got one".split(" ")
);
const POS = "love great easy helpful good happy clear fast useful intuitive enjoy nice smooth".split(" ");
const NEG = "hate slow confusing frustrating hard difficult bug broken expensive annoying painful bad stuck problem issue complicated".split(" ");

function words(text) {
  return (text.toLowerCase().match(/[a-z']{3,}/g) || []).filter((w) => !STOP.has(w));
}
function analyze(text) {
  const freq = {};
  for (const w of words(text)) freq[w] = (freq[w] || 0) + 1;
  const keywords = Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([w]) => w);
  const sentences = (text.match(/[^.!?\n]+[.!?]?/g) || []).map((s) => s.trim()).filter((s) => s.length > 25);
  const scored = sentences.map((s, i) => ({
    s, i, score: words(s).reduce((n, w) => n + (freq[w] || 0), 0) / Math.sqrt(words(s).length || 1),
  }));
  const summary = scored.sort((a, b) => b.score - a.score).slice(0, 3).sort((a, b) => a.i - b.i).map((x) => x.s);
  const toks = text.toLowerCase().match(/[a-z']+/g) || [];
  const pos = toks.filter((t) => POS.includes(t)).length;
  const neg = toks.filter((t) => NEG.includes(t)).length;
  const sentiment = pos === neg ? "neutral" : pos > neg ? "positive" : "negative";
  return { keywords, summary, sentiment, positive: pos, negative: neg };
}

// ---------- helpers ----------
function send(res, code, body, type = "application/json") {
  res.writeHead(code, { "Content-Type": type + "; charset=utf-8" });
  res.end(typeof body === "string" ? body : JSON.stringify(body));
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > 10e6) { reject(new Error("Body too large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}); }
      catch { reject(new Error("Invalid JSON")); }
    });
  });
}
const cleanTags = (t) => [...new Set((t || []).map((x) => String(x).trim().toLowerCase()).filter(Boolean))];

function exportMarkdown(db) {
  const hl = (id) => db.highlights.find((h) => h.id === id);
  const noteTitle = (id) => (db.notes.find((n) => n.id === id) || {}).title || "unknown";
  let md = "# Insight Vault export\n\n";
  for (const ins of db.insights) {
    md += `## ${ins.title}\n\n${ins.description || ""}\n\n`;
    for (const id of ins.highlightIds) {
      const h = hl(id);
      if (h) md += `> ${h.text.replace(/\n+/g, " ")}\n> - *${noteTitle(h.noteId)}* [${h.tags.join(", ")}]\n\n`;
    }
  }
  return md;
}

// ---------- API ----------
async function api(req, res, url) {
  const db = load();
  const parts = url.pathname.split("/").filter(Boolean).slice(1); // after "api"
  const [res1, id, sub] = parts;
  const m = req.method;

  if (res1 === "state" && m === "GET") {
    return send(res, 200, { ...db, notes: db.notes.map(({ text, ...n }) => ({ ...n, length: text.length })) });
  }
  if (res1 === "notes") {
    if (m === "POST" && !id) {
      const b = await readBody(req);
      if (!b.title || !b.text) return send(res, 400, { error: "title and text are required" });
      const note = { id: uid(), title: String(b.title).slice(0, 200), source: b.source || "interview", text: String(b.text), createdAt: new Date().toISOString() };
      db.notes.push(note); save(db);
      return send(res, 201, note);
    }
    const note = db.notes.find((n) => n.id === id);
    if (!note) return send(res, 404, { error: "note not found" });
    if (m === "GET" && !sub) return send(res, 200, note);
    if (m === "GET" && sub === "analysis") return send(res, 200, analyze(note.text));
    if (m === "DELETE") {
      const gone = new Set(db.highlights.filter((h) => h.noteId === id).map((h) => h.id));
      db.notes = db.notes.filter((n) => n.id !== id);
      db.highlights = db.highlights.filter((h) => h.noteId !== id);
      db.insights.forEach((i) => (i.highlightIds = i.highlightIds.filter((x) => !gone.has(x))));
      save(db);
      return send(res, 200, { ok: true });
    }
  }
  if (res1 === "highlights") {
    if (m === "POST") {
      const b = await readBody(req);
      const note = db.notes.find((n) => n.id === b.noteId);
      if (!note) return send(res, 404, { error: "note not found" });
      const { start, end } = b;
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > note.text.length)
        return send(res, 400, { error: "invalid range" });
      const h = { id: uid(), noteId: note.id, start, end, text: note.text.slice(start, end), tags: cleanTags(b.tags), createdAt: new Date().toISOString() };
      db.highlights.push(h); save(db);
      return send(res, 201, h);
    }
    const h = db.highlights.find((x) => x.id === id);
    if (!h) return send(res, 404, { error: "highlight not found" });
    if (m === "PATCH") {
      const b = await readBody(req);
      h.tags = cleanTags(b.tags); save(db);
      return send(res, 200, h);
    }
    if (m === "DELETE") {
      db.highlights = db.highlights.filter((x) => x.id !== id);
      db.insights.forEach((i) => (i.highlightIds = i.highlightIds.filter((x) => x !== id)));
      save(db);
      return send(res, 200, { ok: true });
    }
  }
  if (res1 === "insights") {
    if (m === "POST") {
      const b = await readBody(req);
      if (!b.title) return send(res, 400, { error: "title is required" });
      const ins = { id: uid(), title: String(b.title), description: b.description || "", highlightIds: (b.highlightIds || []).filter((x) => db.highlights.some((h) => h.id === x)), createdAt: new Date().toISOString() };
      db.insights.push(ins); save(db);
      return send(res, 201, ins);
    }
    const ins = db.insights.find((x) => x.id === id);
    if (!ins) return send(res, 404, { error: "insight not found" });
    if (m === "DELETE") { db.insights = db.insights.filter((x) => x.id !== id); save(db); return send(res, 200, { ok: true }); }
  }
  if (res1 === "search" && m === "GET") {
    const q = (url.searchParams.get("q") || "").toLowerCase().trim();
    if (!q) return send(res, 200, []);
    const out = [];
    for (const n of db.notes) {
      const i = n.text.toLowerCase().indexOf(q);
      if (i >= 0 || n.title.toLowerCase().includes(q))
        out.push({ type: "note", noteId: n.id, title: n.title, snippet: n.text.slice(Math.max(0, i - 40), i + 100) });
    }
    for (const h of db.highlights)
      if (h.text.toLowerCase().includes(q) || h.tags.some((t) => t.includes(q)))
        out.push({ type: "highlight", noteId: h.noteId, title: h.tags.join(", ") || "highlight", snippet: h.text });
    return send(res, 200, out);
  }
  if (res1 === "export" && m === "GET") return send(res, 200, exportMarkdown(db), "text/markdown");
  return send(res, 404, { error: "not found" });
}

// ---------- static + server ----------
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    if (url.pathname.startsWith("/api/")) return await api(req, res, url);
    const rel = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname).replace(/^\/+/, "");
    const file = path.resolve(PUBLIC_DIR, rel);
    if (!file.startsWith(PUBLIC_DIR + path.sep)) return send(res, 403, "Forbidden", "text/plain");
    fs.readFile(file, (err, buf) => {
      if (err) return send(res, 404, "Not found", "text/plain");
      res.writeHead(200, { "Content-Type": (MIME[path.extname(file)] || "application/octet-stream") + "; charset=utf-8" });
      res.end(buf);
    });
  } catch (e) {
    send(res, 500, { error: e.message });
  }
});

if (require.main === module) {
  server.listen(PORT, "127.0.0.1", () => {
    const addr = `http://localhost:${PORT}`;
    console.log(`Insight Vault running at ${addr}  (data: ${DATA_FILE})`);
    if (!process.env.NO_OPEN && process.platform === "win32") exec(`start "" "${addr}"`);
  });
}
module.exports = { server, analyze };
