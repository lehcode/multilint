/**
 * The short TUI notice for multilint: classifies the report a lint run produced and decides,
 * against a small state file, whether a one-line notice belongs on the user's screen.
 *
 * Every run that fails, and every run that could not lint, gets its line. A run that passes
 * after the file last failed gets the recovery line — the "changes applied" confirmation. A
 * clean file that never failed is silent: there is nothing to report.
 *
 * The state file records, per file, the failing checks of its last failure, so the recovery
 * line can name them. It lives in the same XDG state directory as the script's changes.db.
 */

import os from "node:os";
import path from "node:path";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";

/**
 * A classified lint report. Only the first notice line matters: the script orders the failure
 * notice first, so it is the first line of the report when a run failed.
 */
export type Report =
  | { readonly kind: "failed"; readonly rel: string; readonly checks: string[] }
  | { readonly kind: "error"; readonly rel: string; readonly reason: string }
  | { readonly kind: "passed" };

/**
 * Per-file record: the path the report named the file by, and the checks that were failing on
 * its last failure — null when the last report was a run that could not lint, so the recovery
 * line reports that instead of a list of checks.
 */
export type FailingState = Record<string, { readonly rel: string; readonly checks: string[] | null }>;

/**
 * Classify a multilint report by its first line. The formats are produced by lint_changed.py
 * (and by this plugin's own could-not-lint text), failure notice first:
 * - `multilint: <rel> failed <names>` — structured failure,
 * - `multilint found failing checks in <rel>.` — fallback failure,
 * - `multilint could not lint <rel>: <reason>. Treat the file as unverified.` — could not lint,
 * - anything else (skipped/unverified notice, config warning) — passed.
 */
export function classifyReport(report: string): Report {
  const line = report.split("\n", 1)[0];
  const failed = line.match(/^multilint: (.+) failed (.+)$/);
  if (failed) return { kind: "failed", rel: failed[1], checks: failed[2].split(", ").filter((n) => n !== "") };
  const fallback = line.match(/^multilint found failing checks in (.+)\.$/);
  if (fallback) return { kind: "failed", rel: fallback[1], checks: [] };
  const error = line.match(/^multilint could not lint (.+)/);
  if (error) {
    const rest = error[1].replace(/\. Treat the file as unverified\.$/, "");
    const sep = rest.indexOf(": ");
    return sep > 0
      ? { kind: "error", rel: rest.slice(0, sep), reason: rest.slice(sep + 2) }
      : { kind: "error", rel: rest, reason: "lint could not run" };
  }
  return { kind: "passed" };
}

/**
 * Decide the TUI line for one report and the state it leaves. `report` is null when the script
 * reported nothing (an unchanged file): no line, no state change.
 */
export function decideNotice(
  file: string,
  report: Report | null,
  state: FailingState,
): { readonly line: string | null; readonly state: FailingState } {
  if (report === null) return { line: null, state };
  const next: FailingState = { ...state };
  switch (report.kind) {
    case "failed": {
      next[file] = { rel: report.rel, checks: report.checks };
      const checks = report.checks.length > 0 ? ` failed ${report.checks.join(", ")}` : " failed";
      return { line: `multilint: ${report.rel}${checks}`, state: next };
    }
    case "error": {
      next[file] = { rel: report.rel, checks: null };
      return { line: `multilint: could not lint ${report.rel}: ${report.reason}`, state: next };
    }
    case "passed": {
      const prev = next[file];
      if (!prev) return { line: null, state };
      delete next[file];
      // null = a could-not-lint run, [] = a failure with no check names, otherwise the names.
      const was =
        prev.checks === null
          ? "could not lint"
          : prev.checks.length > 0
            ? prev.checks.join(", ")
            : "failed checks";
      return { line: `multilint: ${prev.rel} clean (was: ${was})`, state: next };
    }
  }
}

/**
 * The state directory, exactly as lint_changed.py computes it: XDG_STATE_HOME when it is an
 * absolute path (the XDG spec invalidates relative values, so it is ignored), otherwise
 * ~/.local/state, plus "multilint".
 */
function stateDir(): string {
  const xdg = process.env.XDG_STATE_HOME;
  const base = xdg && path.isAbsolute(xdg) ? xdg : path.join(os.homedir(), ".local", "state");
  return path.join(base, "multilint");
}

function stateFile(): string {
  return path.join(stateDir(), "failing.json");
}

/**
 * Read the failing-state file. A missing or corrupt file is the empty state: the recovery line
 * is a convenience, and a broken record is worth less than a fresh one.
 */
export function loadState(): FailingState {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(stateFile(), "utf8"));
  } catch {
    return {};
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return {};
  const state: FailingState = {};
  for (const [file, entry] of Object.entries(raw as Record<string, unknown>)) {
    if (!entry || typeof entry !== "object") continue;
    const record = entry as { rel?: unknown; checks?: unknown };
    if (typeof record.rel !== "string" || record.rel === "") continue;
    const checks = Array.isArray(record.checks)
      ? record.checks.filter((c): c is string => typeof c === "string")
      : record.checks === null
        ? null
        : undefined;
    if (checks === undefined) continue;
    state[file] = { rel: record.rel, checks };
  }
  return state;
}

/**
 * Write the state atomically (tmp file + rename). Failure is not fatal: the state only feeds
 * the recovery line, and a lost update costs at most one missing "clean (was: …)" line.
 */
export function saveState(state: FailingState): void {
  try {
    const dir = stateDir();
    mkdirSync(dir, { recursive: true });
    const file = stateFile();
    writeFileSync(file + ".tmp", JSON.stringify(state, null, 2));
    renameSync(file + ".tmp", file);
  } catch (error) {
    console.error("[multilint] could not save failing state:", error);
  }
}

/**
 * Classify one report, apply it to the persisted state, and save the state it leaves; returns
 * the TUI line to emit, or null when this run gets no notice.
 */
export function noticeFor(file: string, report: string): string | null {
  const state = loadState();
  const { line, state: next } = decideNotice(file, classifyReport(report), state);
  saveState(next);
  return line;
}
