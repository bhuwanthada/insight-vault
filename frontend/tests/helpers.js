import { vi } from "vitest";

// Replace fetch with fake routes. `routes` is either a table keyed "GET state" / "POST highlights"
// (value = data, or function(body)), or a single function (method, path, body) -> data | undefined.
// Return fail("msg") to simulate an API error. Values may be promises.
export function mockApi(routes) {
  const calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, opts = {}) => {
      const method = opts.method || "GET";
      const path = String(url).replace(/^\/api\//, "");
      const body = opts.body ? JSON.parse(opts.body) : undefined;
      calls.push({ method, path, body });
      const handler = typeof routes === "function" ? routes : routes[`${method} ${path}`];
      let out = typeof routes === "function" ? routes(method, path, body) : handler;
      if (typeof out === "function") out = out(body);
      out = await out;
      if (out === undefined) throw new Error(`unmocked request: ${method} ${path}`);
      if (out && out.__error !== undefined) {
        return { ok: false, status: 400, statusText: "Bad Request", json: async () => (out.__error ? { error: out.__error } : {}) };
      }
      return { ok: true, status: 200, json: async () => out };
    }),
  );
  return calls;
}

export const fail = (message) => ({ __error: message });

export const TEXT = "Alpha beta gamma delta. Second sentence here.";
export const note = { id: "n1", title: "Interview 1", source: "interview", text: TEXT };
export const analysis = { keywords: ["alpha", "beta"], summary: ["Alpha beta gamma delta."], sentiment: "neutral", positive: 0, negative: 0 };
export const hl = (id, start, end, tags = [], text = TEXT) => ({ id, noteId: "n1", start, end, text: text.slice(start, end), tags, createdAt: "t" });

// A tiny in-memory stand-in for the Python API, enough to drive the whole UI.
export function fakeBackend(seed = {}) {
  const db = {
    notes: [], highlights: [], insights: [], analysis, suggest: { method: "rules", suggestions: [] }, failNoteLoad: false,
    config: {
      appName: "Insight Vault", accentColor: "#4f46e5", defaultTheme: "system",
      features: ["notes", "insights", "search", "support", "ai_suggestions"], windows: [7, 14, 30, 60, 90],
      defaultDays: 30, locale: "en-US", authMode: "none",
    },
    me: { authenticated: true, user: { email: "local", name: "Local user", role: "admin", provider: "none" }, mode: "none" },
    providers: { mode: "none", providers: [] },
    llm: { enabled: false, provider: "none", model: null, fallbacks: [], circuits: {}, piiMasking: false, usage: { date: "2026-01-01", tokens: 0, calls: 0, cap: 0 } },
    settings: { llm_provider: "none", features: ["notes"], port: 5001, cookie_secure: false, llm_api_key: "not set", data_file: "" },
    audit: [], llmTest: { ok: true, provider: "litellm", model: "m", latencyMs: 12 },
    org: {
      id: "default", name: "Insight Vault", plan: "self-hosted", status: "active", deploymentMode: "single_org",
      features: ["notes", "insights"], quotas: {}, usage: { tickets: 3, seats: null, tokensToday: 12, connectors: 0 }, expiresAt: null, readOnly: false,
    },
    orgSettings: {
      overrides: { app_name: "Acme" }, hasLlmKey: false, editable: [],
      effective: { app_name: "Acme", accent_color: "#4f46e5", output_language: "English", llm_provider: "none", llm_model: "", llm_base_url: "" },
    },
    members: [{ email: "boss@acme.com", role: "admin" }],
    kinds: [
      { kind: "zendesk", label: "Zendesk", config: [{ name: "subdomain", label: "Subdomain", required: true }, { name: "email", label: "E-mail", required: true }],
        secrets: [{ name: "api_token", label: "API token", required: true }] },
      { kind: "jira", label: "Jira Service Management", config: [{ name: "site", label: "Site", required: false }], secrets: [{ name: "api_token", label: "Token", required: false }] },
    ],
    connectors: [], noDatabase: false,
    testResult: { ok: true, detail: "Connected to acme.zendesk.com" }, syncResult: { status: "ok", error: null, fetched: 5, more: false },
    importResult: { imported: 2, skipped: 0, skippedRows: [] }, activateResult: { created: true, org: { name: "Acme", plan: "growth" }, admins: ["boss@acme.com"] },
    ...seed,
  };
  let counter = 0;
  const route = (method, path, body) => {
    const key = `${method} ${path}`;
    let m;
    if (key === "GET llm/status") return db.llm;
    if (key === "GET config/public") return db.config;
    if (key === "GET auth/me") return db.me;
    if (key === "GET auth/providers") return db.providers;
    if (key === "POST auth/logout") return db.logoutFails ? fail("logout failed") : { ok: true };
    if (key === "GET config") return db.settings;
    if (key === "GET audit?limit=50") return db.audit;
    if (key === "POST llm/test") return db.llmTest;
    if (key === "GET state") return { notes: db.notes.map(({ text, ...rest }) => rest), highlights: db.highlights, insights: db.insights };
    if (key === "POST notes") {
      if (!body.title || !body.text) return fail("title and text are required");
      const created = { id: `new${++counter}`, title: body.title, source: body.source, text: body.text };
      db.notes.push(created);
      return created;
    }
    if ((m = path.match(/^notes\/([^/]+)\/analysis$/)) && method === "GET") return db.analysis;
    if ((m = path.match(/^notes\/([^/]+)$/))) {
      const found = db.notes.find((n) => n.id === m[1]);
      if (method === "GET") return found && !db.failNoteLoad ? found : fail("note not found");
      db.notes = db.notes.filter((n) => n.id !== m[1]);
      db.highlights = db.highlights.filter((h) => h.noteId !== m[1]);
      return { ok: true };
    }
    if (key === "POST highlights") {
      const source = db.notes.find((n) => n.id === body.noteId);
      if (db.failHighlight) return fail("invalid range");
      const created = { id: `h${++counter}`, noteId: body.noteId, start: body.start, end: body.end, text: source.text.slice(body.start, body.end), tags: body.tags.map((t) => t.trim().toLowerCase()).filter(Boolean) };
      db.highlights.push(created);
      return created;
    }
    if ((m = path.match(/^highlights\/([^/]+)$/))) {
      db.highlights = db.highlights.filter((h) => h.id !== m[1]);
      return { ok: true };
    }
    if (key === "POST insights/suggest") return db.suggest;
    if (key === "POST insights") {
      if (!body.title || body.title === "FAIL") return fail("title is required");
      const created = { id: `i${++counter}`, ...body };
      db.insights.push(created);
      return created;
    }
    if ((m = path.match(/^insights\/([^/]+)$/))) {
      db.insights = db.insights.filter((i) => i.id !== m[1]);
      return { ok: true };
    }
    if (path.startsWith("search?q=")) {
      const q = decodeURIComponent(path.slice(9));
      return db.notes.filter((n) => n.text.toLowerCase().includes(q)).map((n) => ({ type: "note", noteId: n.id, title: n.title, snippet: n.text.slice(0, 40) }));
    }
    if (key === "GET org") return db.org;
    if (key === "GET org/settings") return db.orgSettings;
    if (key === "PUT org/settings") {
      if (db.failOrgSettings) return fail("settings rejected");
      db.orgSettings = { ...db.orgSettings, overrides: body };
      return { ok: true };
    }
    if (key === "PUT org/secrets") {
      db.orgSettings = { ...db.orgSettings, hasLlmKey: Boolean(body.llm_api_key) };
      return { llm_api_key: body.llm_api_key ? "set" : "not set" };
    }
    if (key === "GET org/members") return db.members;
    if (key === "POST org/members") {
      if (db.failMember) return fail("seat limit reached");
      db.members = [...db.members, body];
      return body;
    }
    if ((m = path.match(/^org\/members\/(.+)$/)) && method === "DELETE") {
      db.members = db.members.filter((x) => x.email !== decodeURIComponent(m[1]));
      return { ok: true };
    }
    if (key === "POST org/switch") return { ok: true, org: body.org };
    if (key === "POST org/activate") return db.activateResult === "fail" ? fail("invalid key: bad_signature") : db.activateResult;
    if (key === "GET connectors") return db.noDatabase ? fail("Integrations need a database: set DATABASE_URL") : { kinds: db.kinds, connectors: db.connectors };
    if (key === "POST connectors") {
      if (db.failConnector) return fail("subdomain may only contain letters, digits and hyphens");
      const created = { id: `c${++counter}`, kind: body.kind, name: body.name || body.kind, config: body.config, interval_minutes: body.intervalMinutes, enabled: true,
                         last_sync_at: null, last_status: null, last_error: null, last_count: 0 };
      db.connectors = [...db.connectors, created];
      db.lastConnectorBody = body;
      return created;
    }
    if ((m = path.match(/^connectors\/([^/]+)\/(test|sync)$/))) return m[2] === "test" ? db.testResult : db.syncResult;
    if ((m = path.match(/^connectors\/([^/]+)$/)) && method === "DELETE") {
      db.connectors = db.connectors.filter((c) => c.id !== m[1]);
      return { ok: true };
    }
    if (key === "POST tickets/import") return db.importFails ? fail("could not find a column for: subject") : db.importResult;
    return undefined;
  };
  return { db, route };
}

// Select [start, end) inside a text node, like a mouse drag would.
export function selectText(textNode, start, end) {
  const range = document.createRange();
  range.setStart(textNode, start);
  range.setEnd(textNode, end);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  return range;
}
