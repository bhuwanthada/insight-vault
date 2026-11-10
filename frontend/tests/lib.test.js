import { describe, expect, it, vi } from "vitest";
import { api } from "../src/api";
import { buildSegments, offsetOf, selectionRange } from "../src/text";
import { fail, mockApi } from "./helpers";

describe("api", () => {
  it("sends GET without a body and returns JSON", async () => {
    const calls = mockApi({ "GET state": { ok: 1 } });
    expect(await api("state")).toEqual({ ok: 1 });
    expect(calls[0]).toEqual({ method: "GET", path: "state", body: undefined });
  });

  it("sends a JSON body on POST", async () => {
    const calls = mockApi({ "POST notes": (b) => ({ echoed: b }) });
    expect(await api("notes", "POST", { a: 1 })).toEqual({ echoed: { a: 1 } });
    expect(calls[0].body).toEqual({ a: 1 });
  });

  it("throws the server's error message", async () => {
    mockApi({ "GET x": fail("nope") });
    await expect(api("x")).rejects.toThrow("nope");
  });

  it("falls back to the status text when the error has no message", async () => {
    mockApi({ "GET x": fail("") });
    await expect(api("x")).rejects.toThrow("Bad Request");
  });

  it("surfaces unmocked routes as failures", async () => {
    mockApi({});
    await expect(api("whatever")).rejects.toThrow("unmocked request");
    vi.unstubAllGlobals();
  });
});

describe("buildSegments", () => {
  const text = "0123456789";
  it("returns one plain segment when nothing is highlighted", () => {
    expect(buildSegments(text, [])).toEqual([{ start: 0, text }]);
  });

  it("splits around highlights, in order, with trailing text", () => {
    const hs = [{ start: 6, end: 8 }, { start: 2, end: 4 }];
    const segs = buildSegments(text, hs);
    expect(segs.map((s) => [s.start, s.text, Boolean(s.highlight)])).toEqual([
      [0, "01", false], [2, "23", true], [4, "45", false], [6, "67", true], [8, "89", false],
    ]);
  });

  it("skips overlapping highlights and has no trailing segment at the end", () => {
    const segs = buildSegments(text, [{ start: 0, end: 5 }, { start: 3, end: 7 }, { start: 5, end: 10 }]);
    expect(segs.map((s) => [s.start, s.text])).toEqual([[0, "01234"], [5, "56789"]]);
  });
});

describe("offsetOf / selectionRange", () => {
  function dom() {
    document.body.innerHTML = '<div id="box"><span data-off="10">hello world</span><b id="plain">x</b></div>';
    return document.querySelector("span").firstChild;
  }

  it("adds the span offset to a text-node offset", () => {
    expect(offsetOf(dom(), 3)).toBe(13);
  });

  it("accepts an element node and walks up to the offset holder", () => {
    dom();
    expect(offsetOf(document.querySelector("span"), 0)).toBe(10);
  });

  it("returns null when no ancestor carries an offset", () => {
    dom();
    expect(offsetOf(document.querySelector("#plain"), 0)).toBeNull();
  });

  const range = (a, ao, b, bo) => ({ startContainer: a, startOffset: ao, endContainer: b, endOffset: bo });
  const full = "x".repeat(10) + "  hello  " + "y".repeat(10);

  it("trims whitespace around the selection", () => {
    const node = dom();
    expect(selectionRange(range(node, 0, node, 11), "x".repeat(10) + "hello world")).toEqual({ start: 10, end: 21 });
    expect(selectionRange(range(node, 0, node, 9), full)).toEqual({ start: 12, end: 17 });
  });

  it("handles reversed selections", () => {
    const node = dom();
    expect(selectionRange(range(node, 9, node, 0), full)).toEqual({ start: 12, end: 17 });
  });

  it("returns null for whitespace-only or out-of-text selections", () => {
    const node = dom();
    expect(selectionRange(range(node, 0, node, 2), full)).toBeNull();
    expect(selectionRange(range(document.querySelector("#plain"), 0, node, 2), full)).toBeNull();
    expect(selectionRange(range(node, 0, document.querySelector("#plain"), 0), full)).toBeNull();
  });
});
