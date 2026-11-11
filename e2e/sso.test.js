// End-to-end single sign-on: the real app (Authlib OIDC, sessions, roles) against a local fake provider.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright-core");
const stack = require("./stack");

const IDP_PORT = 5811;
const API_PORT = 5802;
const UI_PORT = 3802;
const IDP = `http://127.0.0.1:${IDP_PORT}`;
const API = `http://127.0.0.1:${API_PORT}`;
const UI = `http://127.0.0.1:${UI_PORT}`;
const SHOTS = path.join(__dirname, "screenshots");
const DATA = path.join(os.tmpdir(), `iv-sso-${process.pid}.json`);

let idp, api, ui, browser, page, context;
const shot = (name) => page.screenshot({ path: path.join(SHOTS, name + ".png"), fullPage: true });
const chooseUser = (email, name, verified = true) =>
  fetch(`${IDP}/_login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, name, email_verified: verified }) });

async function signInAs(email, name, verified = true) {
  await chooseUser(email, name, verified);
  await page.goto(UI);
  await page.getByRole("link", { name: "Sign in with Test IdP" }).click();
}
async function signOut() {
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("link", { name: "Sign in with Test IdP" }).waitFor();
}

test.before(async () => {
  assert.ok(stack.BROWSERS.length, "No Chrome/Edge found. Set BROWSER_PATH to a Chromium-based browser.");
  fs.mkdirSync(SHOTS, { recursive: true });
  idp = stack.startFakeIdp(IDP_PORT);
  api = stack.startApi({
    PORT: String(API_PORT), DATA_FILE: DATA, AUTH_MODE: "sso", SECRET_KEY: "e2e-secret-".repeat(4), PUBLIC_URL: UI,
    OIDC_NAME: "Test IdP", OIDC_METADATA_URL: `${IDP}/.well-known/openid-configuration`, OIDC_CLIENT_ID: "e2e-client",
    OIDC_CLIENT_SECRET: "e2e-client-secret", ALLOWED_DOMAINS: "corp.example", ADMIN_EMAILS: "boss@corp.example",
    APPROVER_EMAILS: "lead@corp.example", EDITOR_EMAILS: "ed@corp.example",
    AUTHLIB_INSECURE_TRANSPORT: "1", // the local fake provider speaks http; real providers must use https
  });
  ui = stack.startUi(UI_PORT, API);
  await stack.waitFor(`${IDP}/health`, 30000);
  await stack.waitFor(`${API}/api/health`, 30000);
  await stack.waitFor(UI, 120000);
  browser = await chromium.launch({ executablePath: stack.BROWSERS[0] });
  context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  page = await context.newPage();
  page.setDefaultTimeout(30000);
});
test.after(async () => {
  await browser?.close();
  [ui, api, idp].forEach(stack.kill);
  fs.rmSync(DATA, { force: true });
});

test("1. nobody signed in: API refuses and the UI shows the login screen", async () => {
  assert.equal((await fetch(`${API}/api/state`)).status, 401);
  assert.equal((await fetch(`${API}/api/health`)).status, 200);
  await page.goto(UI, { timeout: 120000 });
  await page.getByRole("link", { name: "Sign in with Test IdP" }).waitFor();
  await shot("20-sso-login");
});

test("2. an account outside the allowed domains is denied", async () => {
  await signInAs("stranger@evil.example", "Stranger");
  await page.locator(".alert").waitFor();
  assert.match(await page.locator(".alert").innerText(), /not allowed to use this workspace/);
  assert.equal(await page.getByRole("button", { name: "Notes" }).count(), 0, "no workspace for a denied user");
  await shot("21-sso-denied");
});

test("3. an unverified e-mail is denied even on an allowed domain", async () => {
  await signInAs("ed@corp.example", "Ed Editor", false);
  await page.locator(".alert").waitFor();
  assert.match(await page.locator(".alert").innerText(), /not allowed/);
});

test("4. an editor signs in, gets a secure session cookie and can add notes", async () => {
  await signInAs("ed@corp.example", "Ed Editor");
  await page.getByText(/Ed Editor \(editor\)/).waitFor();
  const cookie = (await context.cookies()).find((c) => c.name === "session");
  assert.ok(cookie, "session cookie set");
  assert.equal(cookie.httpOnly, true);
  assert.equal(cookie.sameSite, "Lax");
  assert.equal(await page.getByRole("button", { name: "Settings" }).count(), 0, "editors do not see Settings");
  await page.fill('input[placeholder^="Title"]', "SSO note");
  await page.fill("textarea", "Written by a signed-in editor.");
  await page.getByRole("button", { name: "Save note" }).click();
  await page.locator("#textbox").waitFor();
  await shot("22-sso-editor");
});

test("5. an editor cannot approve what they created", async () => {
  await page.getByRole("button", { name: "Support report" }).click();
  await page.getByRole("button", { name: "Load sample tickets" }).click();
  await page.locator("table tbody tr").first().waitFor();
  assert.equal(await page.getByPlaceholder(/Reviewer name/).count(), 0, "signed-in users are identified by account, not typed names");
  await page.getByRole("button", { name: "Generate document" }).click();
  await page.getByRole("button", { name: "Send for approval (mock)" }).click();
  await page.getByText("Pending approval", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Approve" }).count(), 0, "editors have no approve button");
  await signOut();
});

test("6. an approver approves it, and the audit trail names the real people", async () => {
  await signInAs("lead@corp.example", "Lena Lead");
  await page.getByText(/Lena Lead \(approver\)/).waitFor();
  await page.getByRole("button", { name: "Support report" }).click();
  await page.getByRole("button", { name: "Approve" }).click();
  await page.getByText("Approved", { exact: true }).waitFor();
  await shot("23-sso-approved");
  await signOut();
});

test("7. an admin sees settings and the activity log; secrets stay hidden", async () => {
  await signInAs("boss@corp.example", "Bo Boss");
  await page.getByText(/Bo Boss \(admin\)/).waitFor();
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("heading", { name: "LLM connection" }).waitFor();
  await page.getByRole("cell", { name: "login", exact: true }).first().waitFor();
  const text = await page.locator("main").innerText();
  assert.match(text, /lead@corp\.example/);
  assert.doesNotMatch(text, /e2e-client-secret|e2e-secret-/);
  await page.getByText(/All active settings/).click();
  assert.match(await page.locator("details").innerText(), /oidc_client_secret\s+set/i);
  await shot("24-sso-admin-settings");
  const approvals = await page.evaluate(async () => (await (await fetch("/api/reports")).json()).map((r) => [r.status, r.createdBy]));
  assert.deepEqual(approvals, [["approved", "ed@corp.example"]]);
});

test("8. signing out ends the session", async () => {
  await signOut();
  assert.equal((await page.evaluate(async () => (await fetch("/api/state")).status)), 401);
});
