import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.js"],
    include: ["tests/**/*.test.jsx", "tests/**/*.test.js"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{js,jsx}", "app/**/*.{js,jsx}"],
      reporter: ["text", "html"],
      thresholds: { statements: 100, branches: 100, functions: 100, lines: 100 },
    },
  },
});
