#!/usr/bin/env node
/*
 * Stop-hook cost guardrail for the hiit-pipeline project.
 * Only acts when the session's cwd is under this project. Ceiling is
 * $0.50 for automated runs (HIIT_AUTOMATED_RUN=1) and $15 otherwise
 * (interactive setup/dev sessions). Logs + warns on stderr when the
 * session's actual cost (from ccusage) meets or exceeds the ceiling.
 * This is a visibility layer only -- it never blocks the session.
 */
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const PROJECT_ROOT = "C:\\Users\\gurfi\\hiit-pipeline";
const LOG_FILE = path.join(PROJECT_ROOT, "cost_guardrail.log");

function readStdin() {
  let raw;
  try {
    raw = fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
  // Strip a UTF-8 BOM some Windows shells prepend when piping JSON.
  return raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw;
}

function isUnderProject(cwd) {
  if (!cwd) return false;
  const norm = path.resolve(cwd).toLowerCase();
  const root = path.resolve(PROJECT_ROOT).toLowerCase();
  return norm === root || norm.startsWith(root + path.sep);
}

function appendLog(line) {
  try {
    fs.mkdirSync(PROJECT_ROOT, { recursive: true });
    fs.appendFileSync(LOG_FILE, line + "\n");
  } catch {
    // best-effort logging only
  }
}

function main() {
  const raw = readStdin();
  let input = {};
  try {
    input = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  const cwd = input.cwd || input.workspace_dir || process.cwd();
  if (!isUnderProject(cwd)) {
    process.exit(0);
  }

  const sessionId = input.session_id;
  if (!sessionId) {
    process.exit(0);
  }

  const isAutomatedRun = process.env.HIIT_AUTOMATED_RUN === "1";
  const ceiling = isAutomatedRun ? 0.5 : 15;

  let cost;
  try {
    const out = execFileSync(
      "ccusage",
      ["session", "--id", sessionId, "--json"],
      { encoding: "utf8", timeout: 15000, shell: true }
    );
    const parsed = JSON.parse(out);
    cost = parsed.totalCost;
  } catch (e) {
    appendLog(
      `${new Date().toISOString()} ERROR could not resolve cost for session=${sessionId} cwd=${cwd}: ${e.message}`
    );
    process.exit(0);
  }

  if (typeof cost !== "number") {
    process.exit(0);
  }

  if (cost >= ceiling) {
    const label = isAutomatedRun ? "AUTOMATED RUN" : "SETUP SESSION";
    const msg = `[hiit-pipeline cost guardrail] ${label} cost $${cost.toFixed(
      2
    )} has reached/exceeded the $${ceiling.toFixed(2)} ceiling (session ${sessionId}).`;
    process.stderr.write(msg + "\n");
    appendLog(
      `${new Date().toISOString()} WARNING session=${sessionId} cost=${cost.toFixed(
        4
      )} ceiling=${ceiling.toFixed(2)} automated=${isAutomatedRun} cwd=${cwd}`
    );
  }

  process.exit(0);
}

main();
