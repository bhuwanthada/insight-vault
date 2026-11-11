import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/components/App";
import Login from "../src/components/Login";
import SettingsTab from "../src/components/SettingsTab";
import Workspace from "../src/components/Workspace";
import { guarded } from "../src/guard";
import { NEXT_THEME, applyTheme, saveTheme, storedTheme } from "../src/theme";
import { fail, fakeBackend, hl, mockApi, note } from "./helpers";

let alertSpy;
beforeEach(() => {
  alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
  window.localStorage.clear();
  window.history.pushState({}, "", "/");
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.removeProperty("--accent");
});

const user = (role = "admin", name = "Dana") => ({ email: "dana@corp.com", name, role, provider: "google" });
const signedIn = (role, config = {}) => ({
  me: { authenticated: true, user: user(role), mode: "sso" },
  config: { ...fakeBackend().db.config, authMode: "sso", ...config },
});

function start(seed) {
  const backend = fakeBackend(seed);
  mockApi(backend.route);
  render(<App />);
  return { ...backend, user: userEvent.setup() };
}

describe("guarded actions", () => {
  it("passes results through and alerts on errors", async () => {
    expect(await guarded(async (x) => x * 2)(4)).toBe(8);
    expect(await guarded(async () => { throw new Error("nope"); })()).toBeUndefined();
    expect(alertSpy).toHaveBeenCalledWith("nope");
  });
});

describe("theme helpers", () => {
  it("cycles system, light, dark and applies the attribute", () => {
    expect(NEXT_THEME).toEqual({ system: "light", light: "dark", dark: "system" });
    applyTheme("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    applyTheme("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("remembers the choice and survives blocked storage", () => {
    saveTheme("dark");
    expect(storedTheme()).toBe("dark");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(storedTheme()).toBeNull();
    expect(() => saveTheme("light")).not.toThrow();
  });
});

describe("App bootstrap", () => {
  it("applies branding from the public configuration", async () => {
    start({ config: { ...fakeBackend().db.config, appName: "Acme Insights", accentColor: "#112233", defaultTheme: "dark" } });
    expect(await screen.findByRole("heading", { name: "Acme Insights" })).toBeInTheDocument();
    expect(document.title).toBe("Acme Insights");
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#112233");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("lets the user cycle the theme and remembers it", async () => {
    const { user: u } = start();
    const button = await screen.findByRole("button", { name: /Switch theme/ });
    expect(button).toHaveTextContent("Theme: system");
    await u.click(button);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    await u.click(screen.getByRole("button", { name: /Switch theme/ }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem("iv-theme")).toBe("dark");
  });

  it("prefers the theme stored in the browser", async () => {
    window.localStorage.setItem("iv-theme", "light");
    start({ config: { ...fakeBackend().db.config, defaultTheme: "dark" } });
    await screen.findByRole("button", { name: /Switch theme/ });
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("explains when the API cannot be reached", async () => {
    mockApi({});
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the API");
  });

  it("shows the login screen when nobody is signed in", async () => {
    start({ me: { authenticated: false, user: null, mode: "sso" }, config: { ...fakeBackend().db.config, authMode: "sso" },
      providers: { mode: "sso", providers: [{ id: "google", label: "Google" }, { id: "linkedin", label: "LinkedIn" }] } });
    const link = await screen.findByRole("link", { name: "Sign in with Google" });
    expect(link).toHaveAttribute("href", "/api/auth/login/google");
    expect(screen.getByRole("link", { name: "Sign in with LinkedIn" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Login", () => {
  const config = { appName: "Acme", authMode: "sso" };
  const withProviders = (info) => mockApi({ "GET auth/providers": info });

  it.each([
    ["denied", /not allowed to use this workspace/],
    ["failed", /Sign-in failed/],
    ["weird", /Sign-in failed/],
  ])("shows a friendly message for login_error=%s", async (code, expected) => {
    window.history.pushState({}, "", `/?login_error=${code}`);
    withProviders({ mode: "sso", providers: [{ id: "google", label: "Google" }] });
    render(<Login config={config} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(expected);
  });

  it("explains API-key mode and the no-provider case", async () => {
    withProviders({ mode: "apikey", providers: [] });
    const { unmount } = render(<Login config={config} />);
    expect(await screen.findByText(/API-key mode/)).toBeInTheDocument();
    unmount();
    withProviders({ mode: "sso", providers: [] });
    render(<Login config={config} />);
    expect(await screen.findByText(/No sign-in method is available/)).toBeInTheDocument();
  });

  it("falls back gracefully when the provider list cannot be loaded", async () => {
    mockApi({ "GET auth/providers": fail("down") });
    render(<Login config={config} />);
    expect(await screen.findByText(/No sign-in method is available/)).toBeInTheDocument();
  });
});

describe("Workspace", () => {
  it("hides features that are switched off and shows Settings only to admins", async () => {
    start({ config: { ...fakeBackend().db.config, features: ["notes", "search"] } });
    await screen.findByRole("button", { name: "Search" });
    expect(screen.queryByRole("button", { name: "Insights" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Support report" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Settings" })).toBeInTheDocument();
  });

  it("hides Settings and the user chip for non-admins in no-login mode", async () => {
    start({ ...signedIn("viewer", { authMode: "none" }) });
    await screen.findByRole("button", { name: "Notes" });
    expect(screen.queryByRole("button", { name: "Settings" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Sign out/)).not.toBeInTheDocument();
  });

  it("shows who is signed in and signs out", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload, search: "" });
    const { user: u } = start(signedIn("editor"));
    expect(await screen.findByText(/Dana \(editor\)/)).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(reload).toHaveBeenCalled());
  });

  it("alerts when sign-out fails", async () => {
    const { user: u } = start({ ...signedIn("editor"), logoutFails: true });
    await u.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("logout failed"));
  });

  it("hides AI suggestions when that feature is off", async () => {
    const config = { ...fakeBackend().db.config, features: ["notes", "insights"] };
    const { user: u } = start({ config });
    await u.click(await screen.findByRole("button", { name: "Insights" }));
    await screen.findByText("Insights (0)");
    expect(screen.queryByRole("button", { name: "Suggest insights" })).not.toBeInTheDocument();
  });

  it("hides approve and reject from people who cannot decide", async () => {
    const backend = fakeBackend(signedIn("editor"));
    const docs = [{ id: "d1", title: "R", status: "pending_approval", markdown: "x", createdAt: "2026-06-15T12:00:00Z", events: [{ detail: "queued" }] }];
    mockApi((m, p, b) => (p === "reports" ? docs : p.startsWith("report?") ? { summary: "s", totalTickets: 0, categories: [], lowConfidence: 0 } : backend.route(m, p, b)));
    render(<App />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Support report" }));
    await screen.findByText("Pending approval");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Reviewer name/)).not.toBeInTheDocument(); // signed-in users are identified by their account
  });

  it("sends the decision without a typed reviewer when signed in", async () => {
    const backend = fakeBackend(signedIn("approver"));
    const sent = [];
    const docs = [{ id: "d1", title: "R", status: "pending_approval", markdown: "x", createdAt: "2026-06-15T12:00:00Z", events: [{ detail: "queued" }] }];
    mockApi((m, p, b) => {
      if (p === "reports/d1/decision") { sent.push(b); return {}; }
      return p === "reports" ? docs : p.startsWith("report?") ? { summary: "s", totalTickets: 0, categories: [], lowConfidence: 0 } : backend.route(m, p, b);
    });
    render(<App />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Support report" }));
    await u.click(await screen.findByRole("button", { name: "Approve" }));
    expect(sent).toEqual([{ decision: "approve" }]);
  });

  it("uses the configured report windows and default", async () => {
    const config = { ...fakeBackend().db.config, windows: [3, 10], defaultDays: 10, locale: "de-DE" };
    const backend = fakeBackend({ config });
    const calls = mockApi((m, p, b) => (p === "reports" ? [] : p.startsWith("report?") ? { summary: "s", totalTickets: 0, categories: [], lowConfidence: 0 } : backend.route(m, p, b)));
    render(<App />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Support report" }));
    expect(await screen.findByRole("combobox", { name: "Time window" })).toHaveValue("10");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Last 3 days", "Last 10 days"]);
    expect(calls.some((c) => c.path === "report?days=10")).toBe(true);
  });
});

describe("Settings tab", () => {
  const llm = {
    enabled: true, provider: "openrouter", model: "vendor/model", fallbacks: ["litellm:backup"], piiMasking: true,
    circuits: { "openrouter:vendor/model": true, "litellm:backup": false }, usage: { date: "d", tokens: 120, calls: 3, cap: 1000 },
  };
  const audit = [{ at: "2026-06-15T12:00:00Z", actor: "dana@corp.com", action: "POST /api/notes", detail: "201" }];
  const settings = { llm_provider: "openrouter", features: ["notes", "search"], extra: [], port: 5001, cookie_secure: false, llm_api_key: "set", data_file: "" };

  async function openSettings(seed) {
    const ctx = start(seed);
    await ctx.user.click(await screen.findByRole("button", { name: "Settings" }));
    return ctx;
  }

  it("shows the LLM connection, usage, activity and masked settings", async () => {
    await openSettings({ llm, audit, settings });
    expect(await screen.findByText("vendor/model")).toBeInTheDocument();
    expect(screen.getByText("litellm:backup")).toBeInTheDocument();
    expect(screen.getByText("120 / 1000 (3 calls)")).toBeInTheDocument();
    expect(screen.getByText("openrouter:vendor/model")).toBeInTheDocument(); // the open circuit
    expect(screen.getByText("on")).toBeInTheDocument();
    expect(screen.getByText("POST /api/notes")).toBeInTheDocument();
    const all = within(screen.getByText(/All active settings/).closest("details"));
    expect(all.getByText("notes, search")).toBeInTheDocument();
    expect(all.getByText("set")).toBeInTheDocument();
    expect(all.getByText("false")).toBeInTheDocument();
    expect(all.getAllByText("empty")).toHaveLength(2); // empty list and empty string
  });

  it("shows sensible text when nothing is configured", async () => {
    await openSettings({});
    expect(await screen.findByText("not configured")).toBeInTheDocument();
    expect(screen.getByText("0 / unlimited (0 calls)")).toBeInTheDocument();
    expect(screen.getAllByText("none").length).toBeGreaterThanOrEqual(2); // fallbacks and circuits
    expect(screen.getByText("off")).toBeInTheDocument();
    expect(screen.getByText("Nothing recorded yet.")).toBeInTheDocument();
  });

  it("tests the connection and reports success or failure", async () => {
    const { user: u, db } = await openSettings({});
    await u.click(await screen.findByRole("button", { name: "Test LLM connection" }));
    expect(await screen.findByText("Connected to litellm:m in 12 ms")).toBeInTheDocument();
    db.llmTest = { ok: false, error: "401 unauthorized" };
    await u.click(screen.getByRole("button", { name: "Test LLM connection" }));
    expect(await screen.findByText("Connection failed: 401 unauthorized")).toBeInTheDocument();
  });

  it("alerts when the settings cannot be loaded", async () => {
    const backend = fakeBackend();
    mockApi((m, p, b) => (p === "config" ? fail("forbidden") : backend.route(m, p, b)));
    render(<SettingsTab locale="en-US" />);
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("forbidden"));
  });
});

describe("Workspace renders note data for signed-in users", () => {
  it("still opens notes from the list", async () => {
    const { user: u } = start({ ...signedIn("editor"), notes: [note], highlights: [hl("h1", 0, 5)] });
    await u.click(await screen.findByText("Interview 1"));
    expect(await screen.findByText("Auto summary")).toBeInTheDocument();
  });

  it("is rendered directly with the props it needs", async () => {
    const backend = fakeBackend();
    mockApi(backend.route);
    render(<Workspace config={backend.db.config} user={user("admin")} theme="system" onToggleTheme={() => {}} />);
    expect(await screen.findByRole("heading", { name: "Insight Vault" })).toBeInTheDocument();
  });
});
