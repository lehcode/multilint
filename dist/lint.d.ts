/**
 * Lint core for the OpenCode plugin: runs the package's own scripts/multilint.sh (synced from
 * core/) on the host, with the same PostToolUse payload Claude Code sends, and returns its notice.
 *
 * multilint.sh decides everything else: which suffixes are lintable, the repository root, the
 * search ceiling, change detection, and the notice text.
 */
/** scripts/multilint.sh next to dist/ in the installed package (and next to src/ in the repo). */
export declare const MULTILINT_SH: string;
/** True for the tools that write a file; nothing else is linted. */
export declare function isLintTool(name: unknown): boolean;
/** The file path a write or edit tool call targets, or undefined when the arguments carry none. */
export declare function toolPath(args: unknown): string | undefined;
/**
 * One lint run's output: the "<multilint>...</multilint>" notice for the tool result, the raw
 * report text behind it (src/notice.ts classifies it into the short TUI notice), and the
 * absolute path of the file the run linted.
 */
export interface LintOutcome {
    readonly notice: string;
    readonly report: string;
    readonly file: string;
}
/**
 * Lints one file. Returns null when there is nothing to report (unsupported file, no changes,
 * or a relative path outside `directory`). A run that fails returns a "could not lint" outcome,
 * never null: silence would read like a clean file.
 */
export declare function lintNotice(filePath: string, directory: string | undefined): Promise<LintOutcome | null>;
