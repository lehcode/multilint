/**
 * Lint core for the OpenCode plugin: runs the package's own scripts/multilint.sh (synced from
 * core/) on the host, with the same PostToolUse payload Claude Code sends, and returns its notice.
 *
 * multilint.sh decides everything else: which suffixes are lintable, the repository root, the
 * search ceiling, change detection, and the notice text.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Slightly above lint_changed.py's own 120 s run timeout, so the script reports its timeout first.
const RUN_TIMEOUT_MS = 150_000;

const LINT_TOOLS = new Set(["write", "edit", "multiedit"]);

/** scripts/multilint.sh next to dist/ in the installed package (and next to src/ in the repo). */
export const MULTILINT_SH = fileURLToPath(new URL("../scripts/multilint.sh", import.meta.url));

/** True for the tools that write a file; nothing else is linted. */
export function isLintTool(name: unknown): boolean {
  return typeof name === "string" && LINT_TOOLS.has(name);
}

/** The file path a write or edit tool call targets, or undefined when the arguments carry none. */
export function toolPath(args: unknown): string | undefined {
  if (!args || typeof args !== "object") return undefined;
  const record = args as Record<string, unknown>;
  for (const key of ["path", "filePath", "file_path"]) {
    const value = record[key];
    if (typeof value === "string" && value !== "") return value;
  }
  return undefined;
}

function wrap(text: string): string {
  return `<multilint>\n${text}\n</multilint>`;
}

/** Run multilint.sh with `payload` on stdin; resolves with its stdout. Rejects on spawn failure or timeout. */
function runHook(payload: object, cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("bash", [MULTILINT_SH], { cwd, stdio: ["pipe", "pipe", "ignore"] });
    let stdout = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`multilint.sh did not finish within ${RUN_TIMEOUT_MS / 1000} s`));
    }, RUN_TIMEOUT_MS);
    child.stdout.on("data", (chunk: Buffer) => (stdout += chunk.toString()));
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(stdout);
    });
    child.stdin.on("error", () => {}); // EPIPE when the script exits early; the close handler reports.
    child.stdin.end(JSON.stringify(payload));
  });
}

/**
 * Lints one file. Returns the "<multilint>...</multilint>" notice, or null when there is nothing to
 * report (unsupported file, clean result, or a relative path outside `directory`). A run that fails
 * returns a "could not lint" notice, never null: silence would read like a clean file.
 */
export async function lintNotice(filePath: string, directory: string | undefined): Promise<string | null> {
  if (typeof filePath !== "string" || filePath === "") return null;
  let absolute: string;
  if (path.isAbsolute(filePath)) {
    absolute = filePath;
  } else if (typeof directory === "string" && path.isAbsolute(directory)) {
    absolute = path.resolve(directory, filePath);
    // A relative path that climbs out of the project directory is not an edit of this project.
    if (path.relative(directory, absolute).startsWith("..")) return null;
  } else {
    return null;
  }

  const cwd = path.dirname(absolute);
  const payload = { tool_name: "Write", cwd, tool_input: { file_path: absolute } };
  let stdout: string;
  try {
    stdout = await runHook(payload, cwd);
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return wrap(`multilint could not lint ${absolute}: ${reason}. Treat the file as unverified.`);
  }
  if (stdout.trim() === "") return null;
  try {
    const context = JSON.parse(stdout)?.hookSpecificOutput?.additionalContext;
    return typeof context === "string" && context !== "" ? wrap(context) : null;
  } catch {
    return wrap(`multilint could not lint ${absolute}: unreadable output from multilint.sh. Treat the file as unverified.`);
  }
}
