// Shared helpers for the end-to-end suites: start/stop the API, the UI and a browser.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, execSync } = require("node:child_process");

const ROOT = path.join(__dirname, "..");
const win = process.platform === "win32";
const PYTHON = process.env.PYTHON || path.join(ROOT, ".venv", win ? "Scripts/python.exe" : "bin/python");
const BROWSERS = [
  process.env.BROWSER_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].filter((p) => p && fs.existsSync(p));
const NO_ENV_FILE = path.join(os.tmpdir(), "insight-vault-no.env"); // points the API away from the developer's real .env

function kill(child) {
  if (!child || child.exitCode !== null) return;
  if (win) execSync(`taskkill /pid ${child.pid} /T /F`, { stdio: "ignore" });
  else child.kill();
}

async function waitFor(url, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("timed out waiting for " + url);
}

function startApi(env) {
  return spawn(PYTHON, ["-m", "backend"], { cwd: ROOT, env: { ...process.env, ENV_FILE: NO_ENV_FILE, ...env }, stdio: "ignore" });
}

function startUi(port, backendUrl) {
  return spawn("npx", ["next", "dev", "-p", String(port)], {
    cwd: path.join(ROOT, "frontend"), env: { ...process.env, BACKEND_URL: backendUrl }, shell: true, stdio: "ignore",
  });
}

function startFakeIdp(port) {
  return spawn(PYTHON, [path.join(__dirname, "fake_idp.py"), String(port)], { cwd: ROOT, stdio: "ignore" });
}

module.exports = { ROOT, PYTHON, BROWSERS, NO_ENV_FILE, kill, waitFor, startApi, startUi, startFakeIdp };
