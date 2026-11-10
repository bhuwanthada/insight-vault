import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RootLayout, { metadata } from "../app/layout";
import Page from "../app/page";
import SupportTab from "../src/components/SupportTab";
import { fail, fakeBackend, mockApi } from "./helpers";

let alertSpy;
beforeEach(() => {
  alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
});

const cat = (over = {}) => ({
  category: "Login & access", count: 4, share: 0.19, avgResolutionHours: 2.1, avgConfidence: 1,
  topActions: [{ name: "Reset password", count: 3 }, { name: "Cleared cache", count: 1 }],
  topTools: [{ name: "admin console", count: 4 }], exampleTickets: ["S-001"], ...over,
});
const report = (over = {}) => ({
  days: 30, totalTickets: 4, lowConfidence: 0, summary: "4 tickets were resolved in the last 30 days.",
  categories: [cat()], topActions: [], topTools: [], byAgent: [], ...over,
});
const doc = (over = {}) => ({
  id: "d1", title: "Support report", status: "draft", markdown: "# Report body",
  events: [{ at: "t", type: "created", detail: "Draft generated (rules summary)" }], ...over,
});

// Mutable fake for the support endpoints.
function supportBackend(init = {}) {
  const s = { report: report(), docs: [], reports: {}, ...init };
  const route = (method, path, body) => {
    if (path.startsWith("report?days=")) return s.reportFails ? fail("report failed") : s.reports[path] ?? s.report;
    if (path === "reports" && method === "GET") return s.docsFail ? fail("docs failed") : s.docs;
    if (path === "tickets/sample") return s.sampleFails ? fail("sample failed") : { loaded: 30 };
    if (path === "reports" && method === "POST") {
      if (s.generateFails) return fail("generate failed");
      const created = doc({ id: `d${s.docs.length + 1}` });
      s.docs = [...s.docs, created];
      return created;
    }
    const m = path.match(/^reports\/([^/]+)\/(submit|decision)$/);
    if (m) {
      if (s.actionFails) return fail("action failed");
      s.docs = s.docs.map((d) => (d.id !== m[1] ? d : {
        ...d,
        status: m[2] === "submit" ? "pending_approval" : body.decision === "approve" ? "approved" : "rejected",
        events: [...d.events, { at: "t", type: "x", detail: m[2] === "submit" ? "MOCK: queued" : `${body.reviewer}: done` }],
      }));
      return {};
    }
    return undefined;
  };
  return { s, route };
}

function setup(init) {
  const backend = supportBackend(init);
  const calls = mockApi(backend.route);
  render(<SupportTab />);
  return { ...backend, calls, user: userEvent.setup() };
}

describe("report", () => {
  it("shows what was diagnosed and how it was fixed for the default 30-day window", async () => {
    setup({ report: report({ lowConfidence: 2 }) });
    expect(await screen.findByText("4 tickets were resolved in the last 30 days.")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "How it was fixed" })).toBeInTheDocument();
    expect(screen.getByText("Login & access")).toBeInTheDocument();
    expect(screen.getByText("Reset password (3), Cleared cache (1)")).toBeInTheDocument();
    expect(screen.getByText("admin console (4)")).toBeInTheDocument();
    expect(document.querySelector(".bar > div")).toHaveStyle({ width: "19%" });
    expect(screen.getByText(/2 ticket\(s\) could not be diagnosed with confidence/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Time window" })).toHaveValue("30");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Last 7 days", "Last 14 days", "Last 30 days", "Last 60 days", "Last 90 days"]);
  });

  it("re-queries when the time window changes", async () => {
    const { user, calls } = setup({ reports: { "report?days=7": report({ days: 7, summary: "Only 1 ticket in 7 days." }) } });
    await screen.findByText("4 tickets were resolved in the last 30 days.");
    await user.selectOptions(screen.getByRole("combobox", { name: "Time window" }), "7");
    expect(await screen.findByText("Only 1 ticket in 7 days.")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "report?days=7")).toBe(true);
  });

  it("handles an empty window and categories without recorded fixes", async () => {
    const { s, user } = setup({ report: report({ totalTickets: 0, categories: [], summary: "No tickets were resolved in the last 30 days." }) });
    expect(await screen.findByText(/No resolved tickets in this window/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be diagnosed/)).not.toBeInTheDocument();

    s.report = report({ categories: [cat({ topActions: [], topTools: [] })] });
    await user.click(screen.getByRole("button", { name: "Load sample tickets" }));
    expect(await screen.findAllByText("none recorded")).toHaveLength(2);
  });

  it("loads the sample tickets and refreshes", async () => {
    const { user, calls } = setup();
    await screen.findByText("Login & access");
    await user.click(screen.getByRole("button", { name: "Load sample tickets" }));
    await waitFor(() => expect(calls.filter((c) => c.path === "report?days=30").length).toBe(2));
    expect(calls.some((c) => c.path === "tickets/sample" && c.method === "POST")).toBe(true);
  });

  it("alerts when the report, documents, sample load or generation fail", async () => {
    const { s, user } = setup({ reportFails: true, docsFail: true, sampleFails: true, generateFails: true });
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("report failed"));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("docs failed"));
    await user.click(screen.getByRole("button", { name: "Load sample tickets" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("sample failed"));
    await user.click(screen.getByRole("button", { name: "Generate document" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("generate failed"));
    expect(s.docs).toEqual([]);
  });
});

describe("documents and mock approval", () => {
  it("generates a document, sends it for approval and approves it", async () => {
    const { user, calls } = setup();
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Generate document" }));
    expect(await screen.findByText("Draft")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "reports" && c.method === "POST").body).toEqual({ days: 30 });

    await user.click(screen.getByRole("button", { name: "View" }));
    expect(screen.getByText("# Report body")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText("# Report body")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send for approval (mock)" }));
    expect(await screen.findByText("Pending approval")).toBeInTheDocument();
    expect(screen.getByText("MOCK: queued")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(/^Reviewer name/), "Dana");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/decision")).body).toEqual({ decision: "approve", reviewer: "Dana" });
    expect(screen.queryByRole("button", { name: /Send for approval|Approve|Reject/ })).not.toBeInTheDocument();
  });

  it("supports rejection followed by resubmission", async () => {
    const { user } = setup({ docs: [doc({ status: "pending_approval" })] });
    await user.type(await screen.findByPlaceholderText(/^Reviewer name/), "Dana");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("Rejected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send for approval (mock)" }));
    expect(await screen.findByText("Pending approval")).toBeInTheDocument();
  });

  it("alerts when an approval action fails", async () => {
    const { user } = setup({ docs: [doc()], actionFails: true });
    await user.click(await screen.findByRole("button", { name: "Send for approval (mock)" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("action failed"));
    expect(screen.getByText("Draft")).toBeInTheDocument();
  });
});

describe("app shell", () => {
  it("is reachable from the Support report tab", async () => {
    const backend = fakeBackend();
    const support = supportBackend();
    mockApi((m, p, b) => backend.route(m, p, b) ?? support.route(m, p, b));
    const { default: App } = await import("../src/components/App");
    render(<App />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Support report" }));
    expect(await screen.findByText("Support resolution report")).toBeInTheDocument();
  });

  it("renders the page and the root layout", async () => {
    const backend = fakeBackend();
    mockApi(backend.route);
    render(<Page />);
    expect(await screen.findByRole("heading", { name: "Insight Vault" })).toBeInTheDocument();
    const html = renderToStaticMarkup(<RootLayout><p>child</p></RootLayout>);
    expect(html).toContain('<html lang="en">');
    expect(html).toContain("<p>child</p>");
    expect(metadata.title).toBe("Insight Vault");
  });
});
