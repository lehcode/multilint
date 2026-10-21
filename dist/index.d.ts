/**
 * multilint plugin — lint-only entry for OpenCode V1 and V2.
 *
 * One default export serves both generations: V2 calls `setup(ctx)`, V1 calls `server(input)`.
 * Each adapter extracts the written file's path, asks src/lint.ts for the outcome, and delivers
 * the notice through its generation's model-visible channel by mutating the host's result in
 * place.
 *
 * V2 additionally emits the short one-line TUI notice (src/notice.ts) as a synthetic message:
 * `resume: false` admits it to the session without scheduling a model turn. V1 has no session
 * API, so its user-visible surface stays the notice in the tool output.
 *
 * The only runtime export is `default`: the V1 loader falls back to treating every named export as
 * a plugin function when it does not recognise the default.
 */
import type { Plugin } from "@opencode/plugin";
export type V1PluginInput = {
    readonly directory: string;
    readonly worktree?: string;
};
export type V1ToolAfterInput = {
    readonly tool: string;
    readonly sessionID: string;
    readonly callID: string;
    readonly args: unknown;
};
export type V1ToolAfterOutput = {
    title: string;
    output: string;
    metadata: unknown;
};
export type V1Hooks = {
    "tool.execute.after": (input: V1ToolAfterInput, output: V1ToolAfterOutput) => Promise<void>;
};
declare const plugin: {
    id: "multilint";
    setup(ctx: Plugin.Context): Promise<Plugin.Cleanup>;
    server(input: V1PluginInput, _options?: Record<string, unknown>): Promise<V1Hooks>;
};
export default plugin;
