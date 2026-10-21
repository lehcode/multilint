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
/**
 * A classified lint report. Only the first notice line matters: the script orders the failure
 * notice first, so it is the first line of the report when a run failed.
 */
export type Report = {
    readonly kind: "failed";
    readonly rel: string;
    readonly checks: string[];
} | {
    readonly kind: "error";
    readonly rel: string;
    readonly reason: string;
} | {
    readonly kind: "passed";
};
/**
 * Per-file record: the path the report named the file by, and the checks that were failing on
 * its last failure — null when the last report was a run that could not lint, so the recovery
 * line reports that instead of a list of checks.
 */
export type FailingState = Record<string, {
    readonly rel: string;
    readonly checks: string[] | null;
}>;
/**
 * Classify a multilint report by its first line. The formats are produced by lint_changed.py
 * (and by this plugin's own could-not-lint text), failure notice first:
 * - `multilint: <rel> failed <names>` — structured failure,
 * - `multilint found failing checks in <rel>.` — fallback failure,
 * - `multilint could not lint <rel>: <reason>. Treat the file as unverified.` — could not lint,
 * - anything else (skipped/unverified notice, config warning) — passed.
 */
export declare function classifyReport(report: string): Report;
/**
 * Decide the TUI line for one report and the state it leaves. `report` is null when the script
 * reported nothing (an unchanged file): no line, no state change.
 */
export declare function decideNotice(file: string, report: Report | null, state: FailingState): {
    readonly line: string | null;
    readonly state: FailingState;
};
/**
 * Read the failing-state file. A missing or corrupt file is the empty state: the recovery line
 * is a convenience, and a broken record is worth less than a fresh one.
 */
export declare function loadState(): FailingState;
/**
 * Write the state atomically (tmp file + rename). Failure is not fatal: the state only feeds
 * the recovery line, and a lost update costs at most one missing "clean (was: …)" line.
 */
export declare function saveState(state: FailingState): void;
/**
 * Classify one report, apply it to the persisted state, and save the state it leaves; returns
 * the TUI line to emit, or null when this run gets no notice.
 */
export declare function noticeFor(file: string, report: string): string | null;
