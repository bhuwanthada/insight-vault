import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Proxy /api/* to the Python backend so the browser only talks to one origin.
const backend = process.env.BACKEND_URL || "http://127.0.0.1:5001";
const here = dirname(fileURLToPath(import.meta.url));
const production = process.env.NODE_ENV === "production";

// Hardening headers for every page. The content-security policy is production only:
// the development server needs inline scripts and eval for hot reloading.
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  ...(production
    ? [{
        key: "Content-Security-Policy",
        value: "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
      }]
    : []),
];

export default {
  // The repo has a lockfile at the root (for e2e tooling) and one here; pin this folder as the app root.
  turbopack: { root: here },
  outputFileTracingRoot: here,
  allowedDevOrigins: ["127.0.0.1"], // dev only: lets the UI hydrate when opened via 127.0.0.1
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
