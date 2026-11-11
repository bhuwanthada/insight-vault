// One-time setup: Python virtual environment + dependencies, frontend dependencies, and a .env file.
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const win = process.platform === "win32";
const venvPython = join(root, ".venv", win ? "Scripts/python.exe" : "bin/python");
const withLitellm = process.argv.includes("--litellm");

function run(command, args, options = {}) {
  console.log(`\n> ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit", shell: win && command === "npm", ...options });
  if (result.status !== 0) {
    console.error(`\nSetup failed while running: ${command} ${args.join(" ")}`);
    process.exit(result.status || 1);
  }
}

if (!existsSync(venvPython)) run(process.env.PYTHON || (win ? "python" : "python3"), ["-m", "venv", ".venv"]);
run(venvPython, ["-m", "pip", "install", "-r", "requirements-dev.txt"]);
if (withLitellm) run(venvPython, ["-m", "pip", "install", "-r", "requirements-litellm.txt"]);
run("npm", ["--prefix", "frontend", "install"]);

if (!existsSync(join(root, ".env"))) {
  copyFileSync(join(root, ".env.example"), join(root, ".env"));
  console.log("\nCreated .env from .env.example. Edit it to add your LLM key, sign-in providers and other settings.");
}
console.log("\nSetup complete. Start the app with: npm start");
