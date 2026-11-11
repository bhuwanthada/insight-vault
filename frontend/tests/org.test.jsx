import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/components/App";
import Activate from "../src/components/Activate";
import CsvImport from "../src/components/CsvImport";
import IntegrationsTab from "../src/components/IntegrationsTab";
import Login from "../src/components/Login";
import Members from "../src/components/Members";
import OrgPanel from "../src/components/OrgPanel";
import OrgSettings from "../src/components/OrgSettings";
import { fakeBackend, mockApi } from "./helpers";

let alertSpy;
beforeEach(() => {
  alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
  window.localStorage.clear();
  window.history.pushState({}, "", "/");
});

function mount(ui, seed) {
  const backend = fakeBackend(seed);
  const calls = mockApi(backend.route);
  render(ui);
  return { ...backend, calls, user: userEvent.setup() };
}

describe("OrgPanel", () => {
  it("shows the plan, features, validity and every limit with its usage", async () => {
    const org = {
      id: "acme", name: "Acme Inc", plan: "growth", status: "active", deploymentMode: "multi_tenant", features: ["notes", "support"],
      quotas: { tickets: 500, seats: 5, connectors: 2, llm_tokens_per_day: 10000 },
      usage: { tickets: 120, seats: 3, tokensToday: 40, connectors: 1 }, expiresAt: "2027-06-15T00:00:00+00:00", readOnly: false,
    };
    mount(<OrgPanel locale="en-US" />, { org });
    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    expect(screen.getByText("growth (SaaS)")).toBeInTheDocument();
    expect(screen.getByText("notes, support")).toBeInTheDocument();
    expect(screen.getByText("6/15/2027")).toBeInTheDocument();
    expect(screen.getByText("120 / 500")).toBeInTheDocument();
    expect(screen.getByText("3 / 5")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("40 / 10000")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows unlimited quotas, no seat row for a single company, and no expiry", async () => {
    mount(<OrgPanel locale="en-US" />);
    expect(await screen.findByText("self-hosted (single company)")).toBeInTheDocument();
    expect(screen.getByText("no expiry")).toBeInTheDocument();
    expect(screen.getByText("3 / unlimited")).toBeInTheDocument();
    expect(screen.queryByText("Seats")).not.toBeInTheDocument();
  });

  it("warns when the license has expired", async () => {
    mount(<OrgPanel locale="en-US" />, { org: { ...fakeBackend().db.org, readOnly: true } });
    expect(await screen.findByRole("alert")).toHaveTextContent("read-only until a renewal key is activated");
  });

  it("alerts when the organization cannot be loaded", async () => {
    mockApi({});
    render(<OrgPanel locale="en-US" />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining("unmocked")));
  });
});

describe("Members", () => {
  it("lists people, adds one and removes one", async () => {
    const { user, db } = mount(<Members />);
    expect(await screen.findByText("boss@acme.com")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("colleague@company.com"), "new@acme.com");
    await user.selectOptions(screen.getByRole("combobox", { name: "Role" }), "editor");
    await user.click(screen.getByRole("button", { name: "Add person" }));
    expect(await screen.findByText("new@acme.com")).toBeInTheDocument();
    expect(db.members[1]).toEqual({ email: "new@acme.com", role: "editor" });
    expect(screen.getByPlaceholderText("colleague@company.com")).toHaveValue("");
    await user.click(screen.getAllByRole("button", { name: "Remove" })[1]);
    await waitFor(() => expect(screen.queryByText("new@acme.com")).not.toBeInTheDocument());
  });

  it("shows the server's reason when adding fails", async () => {
    const { user } = mount(<Members />, { failMember: true });
    await user.type(await screen.findByPlaceholderText("colleague@company.com"), "x@acme.com");
    await user.click(screen.getByRole("button", { name: "Add person" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("seat limit reached"));
  });
});

describe("OrgSettings", () => {
  it("edits settings, stores a new LLM key separately and reports success", async () => {
    const { user, calls, db } = mount(<OrgSettings />);
    const name = await screen.findByLabelText("Application name");
    expect(name).toHaveValue("Acme");
    await user.clear(name);
    await user.type(name, "Acme Vault");
    await user.type(screen.getByLabelText("LLM provider (litellm, openrouter, groq, openai_compatible, none)"), "groq");
    await user.type(screen.getByLabelText("LLM model"), "my-model");
    await user.click(screen.getByLabelText("Issue categories and fix patterns (YAML)"));
    await user.paste("categories: {A: [x]}");
    await user.type(screen.getByLabelText(/LLM API key/), "gsk-secret");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    const saved = calls.find((c) => c.method === "PUT" && c.path === "org/settings").body;
    expect(saved).toMatchObject({ app_name: "Acme Vault", llm_provider: "groq", llm_model: "my-model", rules_yaml: "categories: {A: [x]}" })
    expect(saved).not.toHaveProperty("llm_base_url"); // empty fields are not stored as overrides
    expect(calls.find((c) => c.path === "org/secrets").body).toEqual({ llm_api_key: "gsk-secret" });
    expect(db.orgSettings.hasLlmKey).toBe(true);
    expect(await screen.findByPlaceholderText("a key is set; type to replace it")).toHaveValue("");
  });

  it("shows platform defaults as hints and never saves them as the organization's own choices", async () => {
    const { user, calls } = mount(<OrgSettings />);
    const color = await screen.findByLabelText("Accent color (#rrggbb)");
    expect(color).toHaveValue("");
    expect(color).toHaveAttribute("placeholder", "#4f46e5"); // the default is visible but not stored
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByRole("status");
    expect(calls.find((c) => c.method === "PUT" && c.path === "org/settings").body).toEqual({ app_name: "Acme" });
  });

  it("does not touch the key when none is typed, and clears settings that were emptied", async () => {
    const { user, calls } = mount(<OrgSettings />);
    await user.clear(await screen.findByLabelText("Application name"));
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByRole("status");
    expect(calls.find((c) => c.method === "PUT" && c.path === "org/settings").body).toEqual({});
    expect(calls.some((c) => c.path === "org/secrets")).toBe(false);
  });

  it("hides the saved notice again after the next edit", async () => {
    const { user } = mount(<OrgSettings />);
    await user.click(await screen.findByRole("button", { name: "Save settings" }));
    await screen.findByRole("status");
    await user.type(screen.getByLabelText("LLM model"), "x");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("can remove a stored LLM key", async () => {
    const { user, db } = mount(<OrgSettings />, { orgSettings: { ...fakeBackend().db.orgSettings, hasLlmKey: true } });
    await user.click(await screen.findByRole("button", { name: "Remove LLM key" }));
    await waitFor(() => expect(db.orgSettings.hasLlmKey).toBe(false));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Remove LLM key" })).not.toBeInTheDocument());
  });

  it("shows nothing and alerts when the settings cannot be loaded; rejected saves are reported", async () => {
    mockApi({});
    const { container } = render(<OrgSettings />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("reports a rejected save", async () => {
    const { user } = mount(<OrgSettings />, { failOrgSettings: true });
    await user.click(await screen.findByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("settings rejected"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("falls back to empty values for settings the server did not report", async () => {
    mount(<OrgSettings />, { orgSettings: { overrides: {}, effective: {}, hasLlmKey: false, editable: [] } });
    expect(await screen.findByLabelText("Application name")).toHaveValue("");
  });
});

describe("IntegrationsTab", () => {
  const fillZendesk = async (user) => {
    await user.type(screen.getByLabelText(/^Subdomain/), "acme");
    await user.type(screen.getByLabelText(/^E-mail/), "ops@acme.com");
    await user.type(screen.getByLabelText(/^API token/), "tok-123");
  };

  it("explains when no database is configured", async () => {
    mount(<IntegrationsTab locale="en-US" />, { noDatabase: true });
    expect(await screen.findByRole("alert")).toHaveTextContent("Integrations need a database");
  });

  it("starts empty and offers every supported tool with its own fields", async () => {
    const { user } = mount(<IntegrationsTab locale="en-US" />);
    expect(await screen.findByText(/Nothing connected yet/)).toBeInTheDocument();
    expect(screen.getAllByRole("option").map((o) => o.textContent).slice(0, 2)).toEqual(["Zendesk", "Jira Service Management"]);
    expect(screen.getByLabelText(/^API token/)).toHaveAttribute("type", "password");
    await user.selectOptions(screen.getByRole("combobox", { name: "Tool" }), "jira");
    expect(screen.getByLabelText(/^Site/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Subdomain/)).not.toBeInTheDocument(); // fields change with the tool
  });

  it("connects a tool with credentials, mapping and schedule", async () => {
    const { user, db } = mount(<IntegrationsTab locale="en-US" />);
    await screen.findByText(/Nothing connected yet/);
    await fillZendesk(user);
    await user.type(screen.getByLabelText("Name"), "Support desk");
    await user.type(screen.getByLabelText(/Where the fix is written/), "900");
    await user.clear(screen.getByLabelText("Sync every (minutes)"));
    await user.type(screen.getByLabelText("Sync every (minutes)"), "30");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Support desk")).toBeInTheDocument();
    expect(db.lastConnectorBody).toEqual({
      kind: "zendesk", name: "Support desk", config: { subdomain: "acme", email: "ops@acme.com" }, secrets: { api_token: "tok-123" },
      mapping: { resolution_field: "900" }, intervalMinutes: 30,
    });
    expect(screen.getByLabelText(/^Subdomain/)).toHaveValue(""); // the form is cleared and the secret is not kept in the page
    expect(screen.getByText("Not synced yet")).toBeInTheDocument();
  });

  it("reports a rejected connection", async () => {
    const { user } = mount(<IntegrationsTab locale="en-US" />, { failConnector: true });
    await screen.findByText(/Nothing connected yet/);
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("subdomain may only contain letters, digits and hyphens"));
  });

  const existing = [
    { id: "c1", kind: "zendesk", name: "Desk", interval_minutes: 30, enabled: true, last_sync_at: "2026-06-15T12:00:00Z", last_status: "ok", last_count: 7, last_error: null },
    { id: "c2", kind: "jira", name: "Jira", interval_minutes: 60, enabled: false, last_sync_at: "2026-06-14T12:00:00Z", last_status: "error", last_count: 0, last_error: "authentication failed" },
  ];

  it("shows each connector's status, including paused ones and errors", async () => {
    mount(<IntegrationsTab locale="en-US" />, { connectors: existing });
    expect(await screen.findByText("zendesk, every 30 min")).toBeInTheDocument();
    expect(screen.getByText("jira, every 60 min, paused")).toBeInTheDocument();
    expect(screen.getByText(/ok, 7 tickets/)).toBeInTheDocument();
    expect(screen.getByText(/error, 0 tickets \(authentication failed\)/)).toBeInTheDocument();
  });

  it("tests a connection and reports success or failure", async () => {
    const { user, db } = mount(<IntegrationsTab locale="en-US" />, { connectors: existing });
    await user.click((await screen.findAllByRole("button", { name: "Test" }))[0]);
    expect(await screen.findByText("OK: Connected to acme.zendesk.com")).toBeInTheDocument();
    db.testResult = { ok: false, error: "authentication failed (HTTP 401)" };
    await user.click(screen.getAllByRole("button", { name: "Test" })[0]);
    expect(await screen.findByText("Failed: authentication failed (HTTP 401)")).toBeInTheDocument();
  });

  it("syncs now and describes the outcome", async () => {
    const { user, db } = mount(<IntegrationsTab locale="en-US" />, { connectors: existing });
    await user.click((await screen.findAllByRole("button", { name: "Sync now" }))[0]);
    expect(await screen.findByText("Synced 5 tickets")).toBeInTheDocument();
    db.syncResult = { status: "ok", fetched: 50, more: true };
    await user.click(screen.getAllByRole("button", { name: "Sync now" })[0]);
    expect(await screen.findByText("Synced 50 tickets (more to come)")).toBeInTheDocument();
    db.syncResult = { status: "error", error: "plan limit reached: tickets (limit 500)", fetched: 0, more: false };
    await user.click(screen.getAllByRole("button", { name: "Sync now" })[0]);
    expect(await screen.findByText("Sync failed: plan limit reached: tickets (limit 500)")).toBeInTheDocument();
  });

  it("disconnects a tool only after confirmation", async () => {
    const { user, db } = mount(<IntegrationsTab locale="en-US" />, { connectors: existing });
    vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await user.click((await screen.findAllByRole("button", { name: "Disconnect" }))[0]);
    expect(db.connectors).toHaveLength(2);
    await user.click(screen.getAllByRole("button", { name: "Disconnect" })[0]);
    await waitFor(() => expect(db.connectors.map((c) => c.id)).toEqual(["c2"]));
    expect(await screen.findByText("Jira")).toBeInTheDocument();
  });
});

describe("CsvImport", () => {
  const file = (text = "subject,created,resolved\nx,1,2") => new File([text], "tickets.csv", { type: "text/csv" });

  it("imports a spreadsheet and refreshes the report", async () => {
    const onImported = vi.fn();
    const { user, calls } = mount(<CsvImport onImported={onImported} />);
    await user.upload(screen.getByLabelText("CSV file"), file());
    expect(await screen.findByRole("status")).toHaveTextContent("Imported 2 tickets.");
    expect(calls.find((c) => c.path === "tickets/import").body).toEqual({ csv: "subject,created,resolved\nx,1,2" });
    expect(onImported).toHaveBeenCalledTimes(1);
  });

  it("lists the rows it had to skip", async () => {
    const { user } = mount(<CsvImport onImported={() => {}} />, { importResult: { imported: 1, skipped: 2, skippedRows: [{ row: 3, reason: "missing subject" }, { row: 5, reason: "unreadable date" }] } });
    await user.upload(screen.getByLabelText("CSV file"), file());
    expect(await screen.findByText(/Imported 1 tickets, skipped 2/)).toBeInTheDocument();
    expect(screen.getByText("Row 3: missing subject")).toBeInTheDocument();
    expect(screen.getByText("Row 5: unreadable date")).toBeInTheDocument();
  });

  it("explains an unusable file", async () => {
    const onImported = vi.fn();
    const { user } = mount(<CsvImport onImported={onImported} />, { importFails: true });
    await user.upload(screen.getByLabelText("CSV file"), file("nonsense"));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("could not find a column for: subject"));
    expect(onImported).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("Activate", () => {
  it("creates an organization and says who can sign in", async () => {
    const { user, calls } = mount(<Activate />);
    await user.click(screen.getByText("Activate an organization key"));
    await user.type(screen.getByPlaceholderText("Paste your organization key"), "a.b.c");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Organization created: Acme (growth). Sign in with boss@acme.com.");
    expect(calls.find((c) => c.path === "org/activate").body).toEqual({ key: "a.b.c" });
    expect(screen.getByPlaceholderText("Paste your organization key")).toHaveValue(""); // the key is not left on screen
  });

  it("says when a key was renewed, and when no administrators are listed", async () => {
    const { user } = mount(<Activate />, { activateResult: { created: false, org: { name: "Acme", plan: "growth" }, admins: [] } });
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Key renewed: Acme (growth).");
    expect(screen.getByRole("status")).not.toHaveTextContent("Sign in with");
  });

  it("reports an invalid key", async () => {
    const { user } = mount(<Activate />, { activateResult: "fail" });
    await user.click(screen.getByRole("button", { name: "Activate" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("invalid key: bad_signature"));
  });
});

describe("in the application", () => {
  const admin = { id: "a@b.c", email: "a@b.c", name: "Dana", role: "admin", provider: "google", org: "acme", orgs: ["acme"] };
  const saas = { appName: "Acme", features: ["notes", "insights", "search", "support", "ai_suggestions", "integrations"], windows: [7, 30], defaultDays: 30,
                 locale: "en-US", authMode: "sso", deploymentMode: "multi_tenant", accentColor: "#4f46e5", defaultTheme: "system" };
  const start = (seed) => {
    const backend = fakeBackend(seed);
    mockApi(backend.route);
    render(<App />);
    return { ...backend, user: userEvent.setup() };
  };

  it("gives administrators Integrations, and SaaS administrators the organization, settings and people", async () => {
    const { user } = start({ config: saas, me: { authenticated: true, user: admin, mode: "sso" } });
    await user.click(await screen.findByRole("button", { name: "Integrations" }));
    expect(await screen.findByText("Connected tools")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Organization" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Organization settings" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "People" })).toBeInTheDocument();
  });

  it("keeps the SaaS-only panels away from single-company installations", async () => {
    const { user } = start({ config: { ...saas, deploymentMode: "single_org", authMode: "none" } });
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Organization" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "People" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Organization settings" })).not.toBeInTheDocument();
  });

  it("hides Integrations when the plan does not include it", async () => {
    start({ config: { ...saas, features: ["notes", "support"] }, me: { authenticated: true, user: admin, mode: "sso" } });
    await screen.findByRole("button", { name: "Settings" });
    expect(screen.queryByRole("button", { name: "Integrations" })).not.toBeInTheDocument();
    expect(screen.queryByText("Import tickets from a spreadsheet (CSV)")).not.toBeInTheDocument();
  });

  it("offers the CSV import to editors but not to viewers", async () => {
    const editor = start({ config: saas, me: { authenticated: true, user: { ...admin, role: "editor" }, mode: "sso" } });
    await editor.user.click(await screen.findByRole("button", { name: "Support report" }));
    expect(await screen.findByText("Import tickets from a spreadsheet (CSV)")).toBeInTheDocument();
  });

  it("does not offer the CSV import to viewers", async () => {
    const viewer = start({ config: saas, me: { authenticated: true, user: { ...admin, role: "viewer" }, mode: "sso" } });
    await viewer.user.click(await screen.findByRole("button", { name: "Support report" }));
    await screen.findByText("Support resolution report");
    expect(screen.queryByText("Import tickets from a spreadsheet (CSV)")).not.toBeInTheDocument();
  });

  it("lets people who belong to several organizations switch between them", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload, search: "" });
    const { user } = start({ config: saas, me: { authenticated: true, user: { ...admin, orgs: ["acme", "beta"] }, mode: "sso" } });
    const picker = await screen.findByRole("combobox", { name: "Organization" });
    expect(picker).toHaveValue("acme");
    await user.selectOptions(picker, "beta");
    await waitFor(() => expect(reload).toHaveBeenCalled());
  });

  it("shows no organization picker for people in a single organization", async () => {
    start({ config: saas, me: { authenticated: true, user: admin, mode: "sso" } });
    await screen.findByRole("button", { name: "Settings" });
    expect(screen.queryByRole("combobox", { name: "Organization" })).not.toBeInTheDocument();
  });

  it("offers key activation on the SaaS sign-in screen only", async () => {
    window.history.pushState({}, "", "/");
    mockApi({ "GET auth/providers": { mode: "sso", providers: [{ id: "google", label: "Google" }] } });
    const { unmount } = render(<Login config={{ appName: "Acme", authMode: "sso", deploymentMode: "multi_tenant" }} />);
    expect(await screen.findByText("Activate an organization key")).toBeInTheDocument();
    unmount();
    render(<Login config={{ appName: "Acme", authMode: "sso", deploymentMode: "single_org" }} />);
    await screen.findByRole("link", { name: "Sign in with Google" });
    expect(screen.queryByText("Activate an organization key")).not.toBeInTheDocument();
  });
});
