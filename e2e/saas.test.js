// End-to-end SaaS: real signed organization keys, two organizations, sign-in through a fake identity provider,
// data isolation, plan and people management, CSV import, and the operator suspending an organization.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright-core");
const stack = require("./stack");

const IDP_PORT = 5812;
const API_PORT = 5803;
const UI_PORT = 3803;
const IDP = `http://127.0.0.1:${IDP_PORT}`;
const API = `http://127.0.0.1:${API_PORT}`;
const UI = `http://127.0.0.1:${UI_PORT}`;
const SHOTS = path.join(__dirname, "screenshots");
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), "iv-saas-"));
const PLATFORM_KEY = "operator-key-1234567890";

const admin = (...args) => execFileSync(stack.PYTHON, ["-m", "backend.admin", ...args], { cwd: stack.ROOT, env: { ...process.env, ENV_FILE: stack.NO_ENV_FILE } }).toString().trim();
let idp, api, ui, browser, page, keyA, keyB;
const shot = (name) => page.screenshot({ path: path.join(SHOTS, name + ".png"), fullPage: true });
const chooseUser = (email, name) =>
  fetch(`${IDP}/_login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, name, email_verified: true }) });
async function signInAs(email, name) {
  await chooseUser(email, name);
  await page.goto(UI);
  await page.getByRole("link", { name: "Sign in with Test IdP" }).click();
}
async function signOut() {
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("link", { name: "Sign in with Test IdP" }).waitFor();
}
async function activate(key) {
  await page.getByText("Activate an organization key").click();
  await page.getByPlaceholder("Paste your organization key").fill(key);
  await page.getByRole("button", { name: "Activate" }).click();
}

test.before(async () => {
  assert.ok(stack.BROWSERS.length, "No Chrome/Edge found. Set BROWSER_PATH to a Chromium-based browser.");
  fs.mkdirSync(SHOTS, { recursive: true });
  // the vendor's signing keys, and two organization keys with different plans
  admin("keygen", "--out", WORK);
  const mint = (org, name, plan, ...extra) =>
    admin("mint", "--private-key", path.join(WORK, "org-key-private.pem"), "--org", org, "--name", name, "--plan", plan, "--role", "editor",
      "--admin", `boss@${org}.example`, "--quota", "tickets=1000", "--quota", "seats=3", ...extra);
  keyA = mint("orga", "Org A", "growth");
  keyB = mint("orgb", "Org B", "starter", "--features", "notes,insights,search,support");
  idp = stack.startFakeIdp(IDP_PORT);
  api = stack.startApi({
    PORT: String(API_PORT), DEPLOYMENT_MODE: "multi_tenant", DATABASE_URL: `sqlite:///${path.join(WORK, "saas.db").replace(/\\/g, "/")}`,
    SECRETS_KEY: admin("secrets-key"), ORG_KEY_PUBLIC_KEY_FILE: path.join(WORK, "org-key-public.pem"), PLATFORM_ADMIN_KEYS: `ops:${PLATFORM_KEY}`,
    AUTH_MODE: "sso", SECRET_KEY: "e2e-saas-secret-".repeat(3), PUBLIC_URL: UI, OIDC_NAME: "Test IdP",
    OIDC_METADATA_URL: `${IDP}/.well-known/openid-configuration`, OIDC_CLIENT_ID: "e2e-client", OIDC_CLIENT_SECRET: "e2e-client-secret",
    AUTHLIB_INSECURE_TRANSPORT: "1",
  });
  ui = stack.startUi(UI_PORT, API);
  await stack.waitFor(`${IDP}/health`, 30000);
  await stack.waitFor(`${API}/api/health`, 30000);
  await stack.waitFor(UI, 120000);
  browser = await chromium.launch({ executablePath: stack.BROWSERS[0] });
  page = await (await browser.newContext({ viewport: { width: 1280, height: 800 } })).newPage();
  page.setDefaultTimeout(30000);
});
test.after(async () => {
  await browser?.close();
  [ui, api, idp].forEach(stack.kill);
  fs.rmSync(WORK, { recursive: true, force: true });
});

test("1. nobody can see anything until they sign in, and the sign-in page offers key activation", async () => {
  assert.equal((await fetch(`${API}/api/state`)).status, 401);
  await page.goto(UI, { timeout: 120000 });
  await page.getByRole("link", { name: "Sign in with Test IdP" }).waitFor();
  await page.getByText("Activate an organization key").waitFor();
  await shot("30-saas-login");
});

test("2. pasting a signed key creates an organization, and a forged key is refused", async () => {
  await activate(keyA);
  await page.getByRole("status").getByText(/Organization created: Org A \(growth\)\. Sign in with boss@orga\.example/).waitFor();
  const dialog = page.waitForEvent("dialog");
  await page.getByPlaceholder("Paste your organization key").fill(keyB.slice(0, -4) + "AAAA");
  await page.getByRole("button", { name: "Activate" }).click();
  assert.match((await dialog).message(), /invalid key/);
  await (await dialog).dismiss();
  await page.getByPlaceholder("Paste your organization key").fill(keyB);
  await page.getByRole("button", { name: "Activate" }).click();
  await page.getByRole("status").getByText(/Organization created: Org B \(starter\)/).waitFor();
  await shot("31-saas-activated");
});

test("3. each organization's admin signs in to their own organization", async () => {
  await signInAs("boss@orga.example", "Boss A");
  await page.getByRole("heading", { name: "Org A" }).waitFor();
  await page.getByText(/Boss A \(admin\)/).waitFor();
  await page.fill('input[placeholder^="Title"]', "Org A confidential note");
  await page.fill("textarea", "Only Org A may ever read this sentence.");
  await page.getByRole("button", { name: "Save note" }).click();
  await page.locator("#textbox").waitFor();
  await shot("32-saas-org-a");
});

test("4. the plan, limits and people are visible and manageable by the organization admin", async () => {
  await page.getByRole("button", { name: "Settings" }).click();
  await page.locator("#org-panel").getByText("growth (SaaS)").waitFor();
  assert.match(await page.locator("#org-panel").innerText(), /Seats\s+1 \/ 3/);
  assert.match(await page.locator("#org-panel").innerText(), /Tickets\s+0 \/ 1000/);
  await page.getByPlaceholder("colleague@company.com").fill("analyst@orga.example");
  await page.getByRole("combobox", { name: "Role" }).selectOption("editor");
  await page.getByRole("button", { name: "Add person" }).click();
  await page.locator("#members").getByText("analyst@orga.example").waitFor();
  await page.locator("#org-settings").getByLabel("Application name").fill("Org A Insights");
  await page.getByRole("button", { name: "Save settings" }).click();
  await page.locator("#org-settings").getByRole("status").waitFor();
  await page.reload();
  await page.getByRole("heading", { name: "Org A Insights" }).waitFor();  // the organization's own branding
  await shot("33-saas-settings");
});

test("5. importing a spreadsheet fills the support report", async () => {
  const csv = path.join(WORK, "tickets.csv");
  const day = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 19).replace("T", " ");
  fs.writeFileSync(csv, `Ticket ID,Title,Resolution,Assignee,Tools,Created,Resolved\n` +
    `1,Cannot log in,Reset the password,Asha,admin console,${day(3)},${day(2)}\n2,Dashboard is slow,Restarted the service,Ravi,grafana,${day(5)},${day(4)}\n`);
  await page.getByRole("button", { name: "Support report" }).click();
  await page.setInputFiles('input[aria-label="CSV file"]', csv);
  await page.locator("#csv-import").getByRole("status").getByText(/Imported 2 tickets/).waitFor();
  await page.locator("table tbody tr").first().waitFor();
  assert.match(await page.locator("table").innerText(), /Reset password|Restarted service/);
  await shot("34-saas-csv-import");
});

test("6. the connectors page offers all five ticketing tools", async () => {
  await page.getByRole("button", { name: "Integrations" }).click();
  await page.getByRole("heading", { name: "Connected tools" }).waitFor();
  const tools = await page.getByRole("combobox", { name: "Tool" }).locator("option").allTextContents();
  assert.deepEqual(tools, ["Zendesk", "Freshdesk", "Jira Service Management", "ServiceNow", "Salesforce Service Cloud"]);
  await page.getByLabel(/^Zendesk subdomain/).fill("not a valid host");
  await page.getByLabel(/^E-mail of the agent/).fill("ops@orga.example");
  await page.getByLabel(/^API token/).fill("token-value");
  const dialog = page.waitForEvent("dialog");
  await page.getByRole("button", { name: "Connect" }).click();
  assert.match((await dialog).message(), /letters, digits and hyphens/);  // unsafe hosts are refused before anything is stored
  await (await dialog).dismiss();
  await shot("35-saas-integrations");
});

test("7. another organization sees none of it", async () => {
  await signOut();
  await signInAs("boss@orgb.example", "Boss B");
  await page.getByRole("heading", { name: "Org B" }).waitFor();
  await page.getByText("No notes yet.").waitFor();
  const seen = await page.evaluate(async () => JSON.stringify([await (await fetch("/api/state")).json(), await (await fetch("/api/tickets")).json(), await (await fetch("/api/audit")).json()]));
  assert.doesNotMatch(seen, /Org A|confidential|Reset the password|boss@orga/);
  assert.equal(await page.getByRole("button", { name: "Integrations" }).count(), 0, "Org B's plan has no integrations");
  assert.equal((await page.evaluate(async () => (await fetch("/api/connectors")).status)), 403);
  await shot("36-saas-org-b");
});

test("8. the platform operator manages organizations without seeing their data", async () => {
  const headers = { "X-Platform-Key": PLATFORM_KEY };
  assert.equal((await fetch(`${API}/api/platform/orgs`)).status, 401);
  const orgs = await (await fetch(`${API}/api/platform/orgs`, { headers })).json();
  assert.deepEqual(orgs.map((o) => [o.id, o.plan, o.status]), [["orga", "growth", "active"], ["orgb", "starter", "active"]]);
  assert.equal(orgs[0].members, 2);
  const suspended = await fetch(`${API}/api/platform/orgs/orga/status`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ status: "suspended" }) });
  assert.equal(suspended.status, 200);
});

test("9. a suspended organization is locked out immediately", async () => {
  await signOut();
  await signInAs("boss@orga.example", "Boss A");
  await page.getByRole("link", { name: "Sign in with Test IdP" }).waitFor();  // back at sign-in: no active membership
  assert.equal(await page.getByRole("heading", { name: /Org A/ }).count(), 0);
  assert.match(await page.locator(".alert").innerText(), /not allowed/);
  await shot("37-saas-suspended");
});
