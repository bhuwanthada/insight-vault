// One-command launcher: starts the Python API and the Next.js UI, then opens the browser.
import { spawn, execSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const win = process.platform === "win32";
const venvPython = join(root, ".venv", win ? "Scripts/python.exe" : "bin/python");
const python = process.env.PYTHON || (existsSync(venvPython) ? venvPython : "python");
const apiPort = process.env.API_PORT || "5001";
const uiPort = process.env.UI_PORT || "3000";
const frontend = join(root, "frontend");

if (!existsSync(join(frontend, "node_modules"))) {
  console.error("Frontend dependencies are missing. Run: npm run setup");
  process.exit(1);
}

const env = { ...process.env, PORT: apiPort, BACKEND_URL: `http://127.0.0.1:${apiPort}` };
if (!existsSync(join(frontend, ".next", "BUILD_ID"))) {
  console.log("First run: building the UI (about a minute)...");
  execSync("npx next build", { cwd: frontend, stdio: "inherit", env });
}

const children = [
  spawn(python, ["-m", "backend"], { cwd: root, stdio: "inherit", env }),
  spawn("npx", ["next", "start", "-p", uiPort], { cwd: frontend, stdio: "inherit", env, shell: true }),
];

const stop = () => {
  for (const child of children) {
    if (win) spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"]);
    else child.kill();
  }
};
process.on("SIGINT", () => { stop(); process.exit(0); });
children.forEach((c) => c.on("exit", (code) => { if (code) { console.error("A process exited with code", code); stop(); process.exit(code); } }));

const url = `http://localhost:${uiPort}`;
console.log(`Insight Vault: ${url}  (API on port ${apiPort})`);
setTimeout(() => {
  const open = win ? `start "" "${url}"` : process.platform === "darwin" ? `open ${url}` : `xdg-open ${url}`;
  if (!process.env.NO_OPEN) spawn(open, { shell: true, stdio: "ignore" });
}, 3000);
