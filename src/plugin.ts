/**
 * multilint plugin — lint-only entry for OpenCode V1 and V2.
 *
 * One default export serves both generations: V2 calls `setup(ctx)`, V1 calls `server(input)`.
 * Each adapter extracts the written file's path, asks src/docker-lint.ts for a notice, and delivers
 * it through its generation's model-visible channel by mutating the host's result in place.
 *
 * The only runtime export is `default`: the V1 loader falls back to treating every named export as
 * a plugin function when it does not recognise the default.
 */

import type { Plugin } from "@opencode/plugin";
import { isLintTool, lintNotice, toolPath } from "./docker-lint.js";

export type V1PluginInput = { readonly directory: string; readonly worktree?: string };
export type V1ToolAfterInput = {
  readonly tool: string;
  readonly sessionID: string;
  readonly callID: string;
  readonly args: unknown;
};
export type V1ToolAfterOutput = { title: string; output: string; metadata: unknown };
export type V1Hooks = {
  "tool.execute.after": (input: V1ToolAfterInput, output: V1ToolAfterOutput) => Promise<void>;
};

type MutableResult = { output?: unknown; content?: unknown; [key: string]: unknown };

/**
 * Append the notice to a V2 tool result so it reaches the model. The result is mutated in place
 * (the in-place push onto `content` is the path proven to reach the model) and only copied when it
 * is frozen. Returns the object the caller must assign back to `event.result`.
 */
function deliverV2(result: MutableResult | undefined, notice: string): MutableResult {
  const target: MutableResult = result && !Object.isFrozen(result) ? result : { ...result };
  const content = target.content;
  if (Array.isArray(content) && content.length > 0) {
    if (Object.isFrozen(content)) {
      target.content = [...content, { type: "text", text: notice }];
    } else {
      content.push({ type: "text", text: notice });
    }
  } else if (typeof content === "string" && content !== "") {
    target.content = content + "\n\n" + notice;
  } else {
    const output = target.output;
    const text = output === undefined || output === null ? "" : typeof output === "string" ? output : JSON.stringify(output);
    target.output = text + "\n\n" + notice;
  }
  return target;
}

const plugin = {
  id: "multilint" as const,

  async setup(ctx: Plugin.Context): Promise<Plugin.Cleanup> {
    const registration = await ctx.tool.hook("execute.after", async (event) => {
      try {
        if (event.status !== "completed" || !isLintTool(event.tool)) return;
        const filePath = toolPath(event.input);
        if (!filePath) return;
        const notice = await lintNotice(filePath, ctx.location.directory);
        if (!notice) return;
        // `event.result` is typed readonly-ish; the host reads whichever object ends up here.
        const mutable = event as unknown as { result: MutableResult };
        mutable.result = deliverV2(mutable.result, notice);
      } catch (error) {
        console.error("[multilint] V2 lint hook failed:", error);
      }
    });
    return () => registration.dispose();
  },

  async server(input: V1PluginInput, _options?: Record<string, unknown>): Promise<V1Hooks> {
    return {
      "tool.execute.after": async (hookInput, output) => {
        try {
          if (!isLintTool(hookInput.tool)) return;
          const filePath = toolPath(hookInput.args);
          if (!filePath) return;
          const notice = await lintNotice(filePath, input.directory);
          if (!notice) return;
          output.output = (output.output ?? "") + "\n\n" + notice;
        } catch (error) {
          console.error("[multilint] V1 lint hook failed:", error);
        }
      },
    };
  },
};

export default plugin;
