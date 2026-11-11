// Run a command with the project's Python (the virtual environment if it exists): node scripts/py.mjs -m pytest
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const venvPython = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.PYTHON || (existsSync(venvPython) ? venvPython : "python");
process.exit(spawnSync(python, process.argv.slice(2), { cwd: root, stdio: "inherit" }).status ?? 1);
