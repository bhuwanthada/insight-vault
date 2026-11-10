const test = require("node:test");
const assert = require("node:assert");
const os = require("node:os");
const path = require("node:path");
process.env.DATA_FILE = path.join(os.tmpdir(), `iv-test-${process.pid}.json`);
const { server } = require("../server.js");

let base;
test.before(() => new Promise((r) => server.listen(0, "127.0.0.1", () => { base = `http://127.0.0.1:${server.address().port}/api/`; r(); })));
test.after(() => server.close());
const call = async (p, m = "GET", b) => {
  const r = await fetch(base + p, { method: m, headers: { "Content-Type": "application/json" }, body: b ? JSON.stringify(b) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
};

test("note -> highlight -> insight -> delete cascade", async () => {
  const text = "I love the search but onboarding is confusing and slow. Pricing feels expensive for small teams.";
  const note = (await call("notes", "POST", { title: "Interview 1", text })).body;
  const hl = await call("highlights", "POST", { noteId: note.id, start: 22, end: 52, tags: ["Onboarding", " onboarding ", "ux"] });
  assert.equal(hl.status, 201);
  assert.deepEqual(hl.body.tags, ["onboarding", "ux"]);
  assert.equal(hl.body.text, text.slice(22, 52));
  const ins = (await call("insights", "POST", { title: "Onboarding is a pain", highlightIds: [hl.body.id, "bogus"] })).body;
  assert.deepEqual(ins.highlightIds, [hl.body.id]);
  assert.ok((await call("search?q=onboarding")).body.length >= 2);
  const a = (await call(`notes/${note.id}/analysis`)).body;
  assert.ok(a.summary.length > 0 && a.keywords.length > 0);
  await call(`notes/${note.id}`, "DELETE");
  const st = (await call("state")).body;
  assert.equal(st.highlights.length, 0);
  assert.deepEqual(st.insights[0].highlightIds, []);
});

test("validation", async () => {
  assert.equal((await call("notes", "POST", { title: "x" })).status, 400);
  const note = (await call("notes", "POST", { title: "t", text: "hello world" })).body;
  assert.equal((await call("highlights", "POST", { noteId: note.id, start: 5, end: 99 })).status, 400);
  assert.equal((await call("nope")).status, 404);
});
