import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Proxy /api/* to the Python backend so the browser only talks to one origin.
const backend = process.env.BACKEND_URL || "http://127.0.0.1:5001";
const here = dirname(fileURLToPath(import.meta.url));

export default {
  // The repo has a lockfile at the root (for e2e tooling) and one here; pin this folder as the app root.
  turbopack: { root: here },
  outputFileTracingRoot: here,
  allowedDevOrigins: ["127.0.0.1"], // dev only: lets the UI hydrate when opened via 127.0.0.1
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
