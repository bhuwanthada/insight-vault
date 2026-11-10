import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/components/App";
import { TEXT, analysis, fail, fakeBackend, hl, mockApi, note, selectText } from "./helpers";

let alertSpy;
beforeEach(() => {
  alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
});

function start(seed) {
  const backend = fakeBackend(seed);
  const calls = mockApi(backend.route);
  render(<App />);
  return { ...backend, calls, user: userEvent.setup() };
}
const openNote = async (user, title = "Interview 1") => {
  await user.click(await screen.findByText(title));
  await screen.findByText("Auto summary");
  return document.querySelector("#textbox span");
};

describe("header and notes list", () => {
  it("shows the empty state, LLM-off badge and export link", async () => {
    start();
    expect(await screen.findByText("No notes yet.")).toBeInTheDocument();
    expect(await screen.findByText("LLM off: rules only")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export insights (.md)" })).toHaveAttribute("href", "/api/export");
  });

  it("shows the configured model", async () => {
    start({ llm: { enabled: true, model: "openrouter/x/y" } });
    expect(await screen.findByText("LLM: openrouter/x/y")).toBeInTheDocument();
  });

  it("hides the badge when the status call fails", async () => {
    mockApi((method, path) => (path === "llm/status" ? fail("down") : { notes: [], highlights: [], insights: [] }));
    render(<App />);
    await screen.findByText("No notes yet.");
    expect(screen.queryByText(/LLM/)).not.toBeInTheDocument();
  });

  it("lists notes with their highlight counts", async () => {
    start({ notes: [note], highlights: [hl("h1", 0, 5), hl("h2", 6, 10)] });
    expect(await screen.findByText("2 ✦")).toBeInTheDocument();
  });
});

describe("adding notes", () => {
  it("validates, saves a pasted note and shows the analysis", async () => {
    const { user } = start();
    await user.click(await screen.findByRole("button", { name: "Save note" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("title and text are required"));

    await user.type(screen.getByPlaceholderText(/^Title/), "My interview");
    await user.type(screen.getByPlaceholderText(/^Paste a transcript/), TEXT);
    await user.click(screen.getByRole("button", { name: "Save note" }));

    expect(await screen.findByText("Auto summary")).toBeInTheDocument();
    expect(screen.getAllByText("My interview").length).toBeGreaterThan(0);
    expect(screen.getByText("Sentiment: neutral (+0 / -0)")).toBeInTheDocument();
    expect(screen.getByText("Highlights (0)")).toBeInTheDocument();
    expect(screen.getByText("None yet.")).toBeInTheDocument();
    expect(screen.getByText("alpha")).toBeInTheDocument();
  });

  it("loads a transcript file and defaults the title to the file name", async () => {
    const { user } = start();
    await screen.findByText("New note");
    const input = document.querySelector('input[type="file"]');
    await user.upload(input, new File(["from a file"], "call-1.txt", { type: "text/plain" }));
    await waitFor(() => expect(screen.getByPlaceholderText(/^Paste a transcript/)).toHaveValue("from a file"));
    expect(screen.getByPlaceholderText(/^Title/)).toHaveValue("call-1");

    await user.upload(input, new File(["second"], "other.txt", { type: "text/plain" }));
    await waitFor(() => expect(screen.getByPlaceholderText(/^Paste a transcript/)).toHaveValue("second"));
    expect(screen.getByPlaceholderText(/^Title/)).toHaveValue("call-1"); // an existing title is kept
  });

  it("lets you change the source and returns to the form with + Add note", async () => {
    const { user } = start({ notes: [note] });
    await openNote(user);
    await user.click(screen.getByRole("button", { name: "+ Add note" }));
    const source = await screen.findByPlaceholderText(/^Source/);
    await user.clear(source);
    await user.type(source, "survey");
    expect(source).toHaveValue("survey");
  });

  it("falls back to the form when a note can no longer be loaded", async () => {
    const { user } = start({ notes: [note], failNoteLoad: true });
    await user.click(await screen.findByText("Interview 1"));
    expect(await screen.findByText("New note")).toBeInTheDocument();
  });
});

describe("highlighting", () => {
  async function ready(seed = {}) {
    const ctx = start({ notes: [note], ...seed });
    const span = await openNote(ctx.user);
    return { ...ctx, span, box: document.querySelector("#textbox") };
  }

  it("selects text, tags it and shows the new highlight", async () => {
    const { user, span, box, calls } = await ready();
    selectText(span.firstChild, 6, 10);
    fireEvent.mouseUp(box);
    expect(await screen.findByText("beta", { selector: "#popQuote" })).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/^tags, comma/), "Pricing, UX{Enter}");

    await waitFor(() => expect(document.querySelector("mark.hl")).toHaveTextContent("beta"));
    expect(calls.find((c) => c.path === "highlights").body).toEqual({ noteId: "n1", start: 6, end: 10, tags: ["Pricing", " UX"] });
    expect(screen.queryByText("Highlight", { selector: "button" })).not.toBeInTheDocument();
    expect(screen.getByText("Highlights (1)")).toBeInTheDocument();
    expect(screen.getAllByText("pricing").length).toBeGreaterThan(0);
  });

  it("saves with the button and cancels without saving", async () => {
    const { user, span, box } = await ready();
    selectText(span.firstChild, 0, 5);
    fireEvent.mouseUp(box);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByPlaceholderText(/^tags, comma/)).not.toBeInTheDocument();

    selectText(span.firstChild, 0, 5);
    fireEvent.mouseUp(box);
    await user.click(await screen.findByRole("button", { name: "Highlight" }));
    await waitFor(() => expect(document.querySelectorAll("mark.hl")).toHaveLength(1));
  });

  it("ignores empty, collapsed and whitespace-only selections", async () => {
    const { span, box } = await ready();
    window.getSelection().removeAllRanges();
    fireEvent.mouseUp(box);
    selectText(span.firstChild, 3, 3);
    fireEvent.mouseUp(box);
    selectText(span.firstChild, 5, 6); // the space between "Alpha" and "beta"
    fireEvent.mouseUp(box);
    expect(screen.queryByPlaceholderText(/^tags, comma/)).not.toBeInTheDocument();
  });

  it("alerts when the server rejects the highlight", async () => {
    const { span, box, db, user } = await ready();
    db.failHighlight = true;
    selectText(span.firstChild, 6, 10);
    fireEvent.mouseUp(box);
    await user.click(await screen.findByRole("button", { name: "Highlight" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("invalid range"));
    expect(screen.getByPlaceholderText(/^tags, comma/)).toBeInTheDocument(); // popup stays open
  });

  it("renders existing highlights, truncates long quotes and skips overlaps", async () => {
    const long = { ...note, text: "word ".repeat(60) };
    const highlights = [
      { ...hl("h1", 0, 4, ["x", "y"], long.text), noteId: "n1" },
      { ...hl("h2", 2, 8, [], long.text), noteId: "n1" }, // overlaps h1, not drawn
      { ...hl("h3", 10, 250, [], long.text), noteId: "n1" },
    ];
    const { user } = start({ notes: [long], highlights });
    await user.click(await screen.findByText("Interview 1"));
    await screen.findByText("Auto summary");
    const marks = document.querySelectorAll("mark.hl");
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveAttribute("title", "x, y");
    expect(marks[1]).toHaveAttribute("title", "highlight");
    expect(screen.getAllByText(/…$/).length).toBe(1);
  });

  it("removes a highlight", async () => {
    const { user } = start({ notes: [note], highlights: [hl("h1", 0, 5, ["a"])] });
    await openNote(user);
    await user.click(await screen.findByRole("button", { name: "remove" }));
    await waitFor(() => expect(document.querySelectorAll("mark.hl")).toHaveLength(0));
    expect(screen.getByText("None yet.")).toBeInTheDocument();
  });

  it("shows a placeholder when there is not enough text to summarise", async () => {
    const { user } = start({ notes: [note], analysis: { ...analysis, summary: [] } });
    await openNote(user);
    expect(await screen.findByText("Not enough text to summarise.")).toBeInTheDocument();
  });

  it("deletes a note only after confirmation", async () => {
    const { user, db } = start({ notes: [note] });
    await openNote(user);
    vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(db.notes).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("New note")).toBeInTheDocument();
    expect(db.notes).toHaveLength(0);
  });
});

describe("insights tab", () => {
  const seed = () => ({
    notes: [note],
    highlights: [hl("h1", 0, 5, ["pricing", "ux"]), hl("h2", 6, 10, ["pricing"])],
  });
  const goInsights = async (user) => user.click(await screen.findByRole("button", { name: "Insights" }));

  it("shows empty states", async () => {
    const { user } = start();
    await goInsights(user);
    expect(await screen.findByText("Tag some highlights first.")).toBeInTheDocument();
    expect(screen.getByText("No highlights match.")).toBeInTheDocument();
    expect(screen.getByText("No insights yet.")).toBeInTheDocument();
  });

  it("filters by tag, builds an insight from picked quotes and opens the source note", async () => {
    const { user, calls } = start(seed());
    await goInsights(user);
    await screen.findByText("Evidence (2), tick quotes to build an insight");

    await user.click(screen.getByText("ux", { selector: ".tag" }));
    expect(screen.getByText("Evidence (1), tick quotes to build an insight")).toBeInTheDocument();
    await user.click(screen.getByText("ux", { selector: ".tag" })); // toggle off
    expect(screen.getByText("Evidence (2), tick quotes to build an insight")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Create insight from selected" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("title is required"));

    const boxes = screen.getAllByRole("checkbox");
    await user.click(boxes[0]);
    await user.click(boxes[1]);
    await user.click(boxes[1]); // untick
    await user.type(screen.getByPlaceholderText(/^Insight title/), "Pricing hurts");
    await user.type(screen.getByPlaceholderText(/^What does the evidence/), "Because.");
    await user.click(screen.getByRole("button", { name: "Create insight from selected" }));

    expect(await screen.findByText("Insights (1)")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "insights" && c.method === "POST" && c.body.title === "Pricing hurts").body.highlightIds).toEqual(["h1"]);
    expect(screen.getByPlaceholderText(/^Insight title/)).toHaveValue("");

    await user.click(screen.getByText("↗ Interview 1"));
    expect(await screen.findByText("Auto summary")).toBeInTheDocument();
  });

  it("deletes an insight", async () => {
    const { user } = start({ ...seed(), insights: [{ id: "i1", title: "Keep?", description: "d", highlightIds: ["h1"] }] });
    await goInsights(user);
    await user.click(await screen.findByRole("button", { name: "Delete insight" }));
    expect(await screen.findByText("No insights yet.")).toBeInTheDocument();
  });

  it("suggests insights from the LLM and accepts one", async () => {
    const suggest = {
      method: "llm", model: "m/x",
      suggestions: [{ title: "Theme A", description: "About A", highlightIds: ["h1", "h2"], confidence: 0.8 }],
    };
    const { user, db } = start({ ...seed(), suggest });
    await goInsights(user);
    await user.click(await screen.findByRole("button", { name: "Suggest insights" }));
    expect(await screen.findByText("Generated by m/x")).toBeInTheDocument();
    expect(screen.getByText("80% confidence, 2 quote(s)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept" }));
    expect(await screen.findByText("No suggestions yet. Tag more highlights.")).toBeInTheDocument();
    expect(db.insights.map((i) => i.title)).toEqual(["Theme A"]);
  });

  it("explains rule-based fallbacks", async () => {
    const { user, db } = start(seed());
    db.suggest = { method: "rules", fallbackReason: "LLM circuit is open", suggestions: [] };
    await goInsights(user);
    await user.click(await screen.findByRole("button", { name: "Suggest insights" }));
    expect(await screen.findByText("Rule-based suggestions (LLM circuit is open)")).toBeInTheDocument();

    db.suggest = { method: "rules", suggestions: [] };
    await user.click(screen.getByRole("button", { name: "Suggest insights" }));
    expect(await screen.findByText("Rule-based suggestions (no recurring tags yet)")).toBeInTheDocument();
  });

  it("alerts when suggesting or accepting fails", async () => {
    const bad = { method: "rules", suggestions: [{ title: "FAIL", description: "", highlightIds: ["h1"], confidence: 0.4 }] };
    const { user } = start({ ...seed(), suggest: bad });
    await goInsights(user);
    await user.click(await screen.findByRole("button", { name: "Suggest insights" }));
    await user.click(await screen.findByRole("button", { name: "Accept" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("title is required"));
    expect(screen.getByText("FAIL")).toBeInTheDocument(); // still listed

    const original = global.fetch;
    vi.stubGlobal("fetch", vi.fn(async (url) => (String(url).endsWith("insights/suggest")
      ? { ok: false, status: 500, statusText: "x", json: async () => ({ error: "llm exploded" }) }
      : original(url))));
    await user.click(screen.getByRole("button", { name: "Suggest insights" }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("llm exploded"));
  });
});

describe("search tab", () => {
  const goSearch = async (user) => user.click(await screen.findByRole("button", { name: "Search" }));

  it("searches as you type, opens a result, and clears", async () => {
    const { user, calls } = start({ notes: [note] });
    await goSearch(user);
    const box = await screen.findByPlaceholderText(/^Search notes/);
    await user.type(box, "beta");
    expect(await screen.findByText("note: Interview 1")).toBeInTheDocument();

    await user.clear(box);
    await waitFor(() => expect(screen.queryByText("note: Interview 1")).not.toBeInTheDocument());
    expect(calls.filter((c) => c.path.startsWith("search")).every((c) => c.path !== "search?q=")).toBe(true);

    await user.type(box, "zzz");
    expect(await screen.findByText("No results.")).toBeInTheDocument();

    await user.clear(box);
    await user.type(box, "alpha");
    await user.click(await screen.findByText("note: Interview 1"));
    expect(await screen.findByText("Auto summary")).toBeInTheDocument();
  });

  it("ignores a slow, outdated response", async () => {
    let releaseSlow;
    const slow = new Promise((resolve) => { releaseSlow = resolve; });
    const fresh = [{ type: "note", noteId: "n1", title: "Fresh", snippet: "s" }];
    mockApi((method, path) => {
      if (path === "llm/status") return { enabled: false, model: null };
      if (path === "state") return { notes: [], highlights: [], insights: [] };
      if (path === "search?q=a") return slow;
      if (path === "search?q=ab") return fresh;
      return undefined;
    });
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Search" }));
    const box = await screen.findByPlaceholderText(/^Search notes/);
    await user.type(box, "a");
    await user.type(box, "b");
    expect(await screen.findByText("note: Fresh")).toBeInTheDocument();
    releaseSlow([{ type: "note", noteId: "n1", title: "Stale", snippet: "s" }]);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("note: Stale")).not.toBeInTheDocument();
    expect(within(document.body).getByText("note: Fresh")).toBeInTheDocument();
  });
});
