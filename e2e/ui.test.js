// End-to-end: starts the real Python API and Next.js UI, drives them in Chrome/Edge, saves screenshots.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright-core");
const stack = require("./stack");

const SHOTS = path.join(__dirname, "screenshots");
const SAMPLE = path.join(stack.ROOT, "samples", "interview-priya.txt");
const API_PORT = 5801;
const UI_PORT = 3801;
const API = `http://127.0.0.1:${API_PORT}`;
const UI = `http://127.0.0.1:${UI_PORT}`;
const DATA = path.join(os.tmpdir(), `iv-e2e-${process.pid}.json`);

let backend, frontend, browser, page;
const shot = (name) => page.screenshot({ path: path.join(SHOTS, name + ".png"), fullPage: true });

test.before(async () => {
  assert.ok(stack.BROWSERS.length, "No Chrome/Edge found. Set BROWSER_PATH to a Chromium-based browser.");
  fs.mkdirSync(SHOTS, { recursive: true });
  backend = stack.startApi({ PORT: String(API_PORT), DATA_FILE: DATA, LLM_MODEL: "", AUTH_MODE: "none" });
  frontend = stack.startUi(UI_PORT, API);
  await stack.waitFor(`${API}/api/health`, 30000);
  await stack.waitFor(UI, 120000);
  browser = await chromium.launch({ executablePath: stack.BROWSERS[0] });
  page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.setDefaultTimeout(30000);
  page.on("pageerror", (e) => { throw new Error("Page JS error: " + e.message); });
});
test.after(async () => {
  await browser?.close();
  stack.kill(frontend);
  stack.kill(backend);
  fs.rmSync(DATA, { force: true });
});

// Select an exact phrase the way a mouse drag would: native Range + mouseup.
async function selectPhrase(phrase) {
  const ok = await page.evaluate((p) => {
    for (const s of document.querySelectorAll("#textbox span[data-off]")) {
      const i = s.textContent.indexOf(p);
      if (i >= 0) {
        const r = document.createRange();
        r.setStart(s.firstChild, i);
        r.setEnd(s.firstChild, i + p.length);
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
        document.querySelector("#textbox").dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        return true;
      }
    }
    return false;
  }, phrase);
  assert.ok(ok, "phrase not found in an unhighlighted span: " + phrase);
  await page.locator("#popup").waitFor({ state: "visible" });
}
async function highlight(phrase, tags) {
  await selectPhrase(phrase);
  await page.fill("#popTags", tags);
  await page.click("#popSave");
  await page.waitForFunction((p) => [...document.querySelectorAll("mark.hl")].some((m) => m.innerText === p), phrase);
}
const api = async (p) => (await fetch(`${API}/api/${p}`)).json();

test("1. empty state, LLM badge", async () => {
  await page.goto(UI, { timeout: 120000 });
  await page.getByText("No notes yet.").waitFor();
  assert.equal(await page.title(), "Insight Vault");
  assert.match(await page.locator("#llm-badge").innerText(), /LLM off/);
  await shot("01-empty-state");
});

test("2. validation: empty note shows an error", async () => {
  let msg = "";
  page.once("dialog", (d) => { msg = d.message(); d.dismiss(); });
  await page.getByRole("button", { name: "Save note" }).click();
  await page.waitForTimeout(500);
  assert.match(msg, /title and text are required/);
});

test("3. load a transcript from a file and save it", async () => {
  await page.setInputFiles('input[type="file"]', SAMPLE);
  await page.waitForFunction(() => document.querySelector("textarea").value.includes("Interviewer: Thanks for joining"));
  assert.equal(await page.locator('input[placeholder^="Title"]').inputValue(), "interview-priya");
  await shot("02-transcript-loaded-in-form");
  await page.locator('input[placeholder^="Title"]').fill("Interview with Priya");
  await page.getByRole("button", { name: "Save note" }).click();
  await page.locator("#textbox").waitFor();
});

test("4. auto summary panel", async () => {
  await page.getByText("Auto summary").waitFor();
  assert.match(await page.getByText(/Sentiment:/).innerText(), /Sentiment: (positive|negative|neutral)/);
  assert.ok((await page.locator(".card .tag").count()) >= 3, "keywords shown");
  await shot("03-note-with-summary");
});

test("5. highlight by double-clicking a word, then tag it", async () => {
  const pt = await page.evaluate(() => {
    const s = [...document.querySelectorAll("#textbox span[data-off]")].find((x) => x.textContent.includes("painful"));
    const i = s.textContent.indexOf("painful");
    const r = document.createRange();
    r.setStart(s.firstChild, i);
    r.setEnd(s.firstChild, i + 7);
    const b = r.getBoundingClientRect();
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
  });
  await page.mouse.dblclick(pt.x, pt.y);
  await page.locator("#popup").waitFor({ state: "visible" });
  assert.equal((await page.locator("#popQuote").innerText()).trim(), "painful");
  await shot("04-tag-popup");
  await page.fill("#popTags", "Pain, Findability");
  await page.press("#popTags", "Enter");
  await page.locator("mark.hl").first().waitFor();
  assert.equal(await page.locator("mark.hl").first().innerText(), "painful");
  await shot("05-first-highlight");
});

test("6. highlight multi-word phrases (including after existing highlights)", async () => {
  await highlight("It was expensive for limited user access, so only two of us got seats and the product managers were locked out.", "pricing, access");
  await highlight("I became a human search engine.", "access, pain");
  await highlight("I always want to click back to the exact quote and the video moment to be sure.", "ai, traceability");
  await highlight("a tool that keeps data in the EU or on our own machines would be easier to approve", "privacy");
  assert.equal(await page.locator("mark.hl").count(), 5);
  await page.getByText("Highlights (5)").waitFor();
  await shot("06-five-highlights");
});

test("7. highlight offsets are stored exactly", async () => {
  const st = await api("state");
  const note = await api("notes/" + st.notes[0].id);
  assert.equal(st.highlights.length, 5);
  for (const h of st.highlights) assert.equal(note.text.slice(h.start, h.end), h.text);
});

test("8. cancel leaves no highlight", async () => {
  await selectPhrase("Make it simple");
  await page.click("#popCancel");
  assert.equal(await page.locator("#popup").count(), 0);
  assert.equal(await page.locator("mark.hl").count(), 5);
});

test("9. insights tab: filter by tag, build insight from evidence", async () => {
  await page.getByRole("button", { name: "Insights", exact: true }).click();
  await page.getByText("Filter by tag").waitFor();
  await page.locator(".tag", { hasText: /^access$/ }).click();
  assert.equal(await page.locator("label.quote").count(), 2, "tag filter narrows to 2 quotes");
  await shot("07-insights-filtered");
  await page.locator("label.quote input").nth(0).check();
  await page.locator("label.quote input").nth(1).check();
  await page.fill('input[placeholder^="Insight title"]', "Per-seat pricing locks PMs out");
  await page.fill("textarea", "Researchers become a human search engine.");
  await page.getByRole("button", { name: "Create insight from selected" }).click();
  await page.getByText("Insights (1)").waitFor();
  await shot("08-insight-created");
});

test("10. insight validation: title required", async () => {
  let msg = "";
  page.once("dialog", (d) => { msg = d.message(); d.dismiss(); });
  await page.getByRole("button", { name: "Create insight from selected" }).click();
  await page.waitForTimeout(500);
  assert.match(msg, /title is required/);
});

test("11. AI-suggested insights fall back to rules when no LLM is configured", async () => {
  await page.getByRole("button", { name: "Suggest insights" }).click();
  await page.locator("#suggest-method").waitFor();
  assert.match(await page.locator("#suggest-method").innerText(), /Rule-based suggestions/);
  await page.getByText("Recurring theme: pain").waitFor();
  await shot("09-ai-suggestions");
  await page.getByRole("button", { name: "Accept" }).first().click();
  await page.getByText("Insights (2)").waitFor();
});

test("12. clicking an insight quote opens the source note", async () => {
  await page.locator(".quote", { hasText: "↗" }).first().click();
  await page.locator("#textbox").waitFor();
  assert.ok(await page.getByText("Interview with Priya").first().isVisible());
});

test("13. search finds notes and quotes, and a result opens the note", async () => {
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.fill('input[placeholder^="Search"]', "pricing");
  await page.locator(".quote").first().waitFor();
  assert.ok((await page.locator(".quote").count()) >= 2, "note + highlight hits");
  await shot("10-search");
  await page.fill('input[placeholder^="Search"]', "zzzznotfound");
  await page.getByText("No results.").waitFor();
  await page.fill('input[placeholder^="Search"]', "locked");
  await page.locator(".quote").first().click();
  await page.locator("#textbox").waitFor();
});

test("14. export downloads markdown with the insight and its quotes", async () => {
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name: "Export insights (.md)" }).click()]);
  assert.equal(dl.suggestedFilename(), "insights.md");
  const md = fs.readFileSync(await dl.path(), "utf8");
  assert.match(md, /## Per-seat pricing locks PMs out/);
  assert.match(md, /> I became a human search engine\./);
});

test("15. data persists after reload", async () => {
  await page.reload();
  await page.locator(".item", { hasText: "Interview with Priya" }).click();
  await page.locator("mark.hl").first().waitFor();
  assert.equal(await page.locator("mark.hl").count(), 5);
});

test("16. support report: sample tickets, X-day dropdown, issue + fix breakdown", async () => {
  await page.getByRole("button", { name: "Support report" }).click();
  await page.getByText(/No resolved tickets in this window/).waitFor();
  await page.getByRole("button", { name: "Load sample tickets" }).click();
  await page.locator("table tbody tr").first().waitFor();
  const total = async () => Number((await page.locator("#report-summary").innerText()).match(/^(\d+) tickets/)[1]);
  const select = page.getByRole("combobox", { name: "Time window" });
  const counts = {};
  for (const days of [7, 30, 90]) {
    await select.selectOption(String(days));
    await page.getByText(new RegExp(`last ${days} days`)).first().waitFor();
    counts[days] = await total();
  }
  assert.ok(counts[7] < counts[30] && counts[30] < counts[90], JSON.stringify(counts));
  await select.selectOption("30");
  await page.getByText("Reset password / credentials").first().waitFor();
  assert.match(await page.locator("table").innerText(), /Login & access/);
  await shot("11-support-report");
});

test("17. document generation and mock approval workflow", async () => {
  await page.getByRole("button", { name: "Generate document" }).click();
  await page.getByText("Draft", { exact: true }).waitFor();
  await page.getByRole("button", { name: "View" }).click();
  assert.match(await page.locator("pre.doc").innerText(), /Executive summary/);
  await shot("12-document-draft");
  await page.getByRole("button", { name: "Send for approval (mock)" }).click();
  await page.getByText("Pending approval", { exact: true }).waitFor();
  await page.fill('input[placeholder^="Reviewer name"]', "Dana (Support Lead)");
  await page.getByRole("button", { name: "Approve" }).click();
  await page.getByText("Approved", { exact: true }).waitFor();
  await shot("13-document-approved");
  const docs = await api("reports");
  assert.equal(docs.length, 1);
  assert.equal(docs[0].status, "approved");
  assert.deepEqual(docs[0].events.map((e) => e.type), ["created", "approval_requested", "approved"]);
  assert.match(docs[0].events[1].detail, /MOCK/);
});

test("18. settings tab: LLM connection, usage and masked settings", async () => {
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("heading", { name: "LLM connection" }).waitFor();
  await page.getByText("Fallbacks", { exact: true }).waitFor(); // the status table appears once the data has loaded
  assert.match(await page.locator("main").innerText(), /Provider\s+none/);
  await page.getByRole("button", { name: "Test LLM connection" }).click();
  await page.locator("#llm-test-result").waitFor();
  assert.match(await page.locator("#llm-test-result").innerText(), /Connection failed: no LLM configured/);
  await page.getByText(/All active settings/).click();
  assert.match(await page.locator("details").innerText(), /LLM_MAX_CONCURRENCY\s+4/i);
  await shot("17-settings");
  const config = await (await fetch(`${API}/api/config`)).json();
  assert.equal(config.LLM_API_KEY, "not set"); // secrets are reported as set / not set, never as values
  assert.equal(config.AUTH_MODE, "none");
});

test("19. theme toggle switches and remembers the choice", async () => {
  const button = page.getByRole("button", { name: /Switch theme/ });
  await button.click();
  assert.equal(await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "light");
  await button.click();
  assert.equal(await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "dark");
  await page.reload();
  await page.getByRole("button", { name: /Switch theme/ }).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "dark");
  await page.getByRole("button", { name: /Switch theme/ }).click(); // back to system
  assert.equal(await page.evaluate(() => document.documentElement.hasAttribute("data-theme")), false);
});

test("20. untrusted text is rendered as text, not HTML (XSS)", async () => {
  let fired = false;
  const onDialog = (d) => { fired = true; d.dismiss(); };
  page.on("dialog", onDialog);
  await page.getByRole("button", { name: "Notes", exact: true }).click();
  await page.getByRole("button", { name: "+ Add note" }).click();
  await page.fill('input[placeholder^="Title"]', "<img src=x onerror=alert(1)>");
  await page.fill("textarea", "<script>alert(2)</script> <b>bold?</b> hello world from a hostile transcript");
  await page.getByRole("button", { name: "Save note" }).click();
  await page.locator("#textbox").waitFor();
  await page.waitForTimeout(500);
  assert.equal(await page.locator("#textbox b").count(), 0);
  assert.match(await page.locator("#textbox").innerText(), /<script>alert\(2\)<\/script>/);
  assert.equal(fired, false, "no script executed");
  page.off("dialog", onDialog);
  await shot("14-xss-safe");
});

test("21. remove highlight, delete insights and note (cascade)", async () => {
  await page.locator(".item", { hasText: "Interview with Priya" }).click();
  await page.locator("mark.hl").first().waitFor();
  await page.locator(".quote").filter({ hasText: "a tool that keeps data" }).getByRole("button", { name: "remove" }).click();
  await page.waitForFunction(() => document.querySelectorAll("mark.hl").length === 4);
  await page.getByRole("button", { name: "Insights", exact: true }).click();
  while (await page.getByRole("button", { name: "Delete insight" }).count()) {
    await page.getByRole("button", { name: "Delete insight" }).first().click();
    await page.waitForTimeout(300);
  }
  await page.getByText("No insights yet.").waitFor();
  await page.getByRole("button", { name: "Notes", exact: true }).click();
  await page.locator(".item", { hasText: "Interview with Priya" }).click();
  await page.locator("mark.hl").first().waitFor();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("main").innerText.includes("Interview with Priya"));
  assert.equal((await api("state")).highlights.length, 0, "highlights cascade-deleted with note");
});

test("22. dark mode contrast and mobile layout", async () => {
  await page.emulateMedia({ colorScheme: "dark" });
  const colors = await page.evaluate(() => ({
    link: getComputedStyle(document.querySelector("header a.btn")).color,
    body: getComputedStyle(document.body).color,
  }));
  assert.equal(colors.link, colors.body, "Export button text must follow the theme");
  await shot("15-dark-mode");
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 390, height: 800 });
  await shot("16-mobile");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, "no horizontal scroll on mobile");
});
