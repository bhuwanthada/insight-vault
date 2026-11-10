import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// jsdom has no layout engine and no Range geometry; give it stable fakes.
Range.prototype.getBoundingClientRect = () => ({ left: 100, bottom: 200, top: 180, right: 160, width: 60, height: 20 });

// Older jsdom builds lack Blob#text; the app reads uploaded files with it.
if (!Blob.prototype.text) {
  Blob.prototype.text = function text() {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.readAsText(this);
    });
  };
}
