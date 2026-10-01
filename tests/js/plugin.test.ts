// Docker-free regression tests for the OpenCode lint plugin. A `docker` shim on PATH records its
// argv and prints a canned failing lint document, so the plugin runs end to end without a container.
import { afterAll, beforeAll, expect, test } from "bun:test";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import plugin from "../../src/plugin.ts";
import * as repoLocal from "../../.opencode/plugins/multilint.ts";
import * as pluginModule from "../../src/plugin.ts";

const CANNED = JSON.stringify({
  return_code: 1,
  checks: {
    shellcheck: {
      status: "failed",
      fix: "Fix the shell syntax",
      findings: [{ rule: "SC1073", file: "broken.sh", line: 2, message: "Couldn't parse this" }],
    },
  },
  summary: { rules_violated: ["SC1073"] },
});

let root: string;
let project: string;
let dockerLog: string;
const saved = { PATH: process.env.PATH, XDG_STATE_HOME: process.env.XDG_STATE_HOME };

beforeAll(() => {
  root = realpathSync(mkdtempSync(path.join(tmpdir(), "multilint-js-")));
  project = path.join(root, "project");
  mkdirSync(path.join(project, ".git"), { recursive: true });
  mkdirSync(path.join(project, "sub"), { recursive: true });
  dockerLog = path.join(root, "docker.log");
  const bin = path.join(root, "bin");
  mkdirSync(bin);
  const shim = path.join(bin, "docker");
  writeFileSync(shim, `#!/bin/sh\nprintf '%s\\n' "$*" >> '${dockerLog}'\ncat <<'EOF'\n${CANNED}\nEOF\nexit 1\n`);
  chmodSync(shim, 0o755);
  mkdirSync(path.join(root, "state"));
  process.env.PATH = `${bin}:${saved.PATH}`;
  process.env.XDG_STATE_HOME = path.join(root, "state");
});

afterAll(() => {
  process.env.PATH = saved.PATH;
  if (saved.XDG_STATE_HOME === undefined) delete process.env.XDG_STATE_HOME;
  else process.env.XDG_STATE_HOME = saved.XDG_STATE_HOME;
});

function invocations(): string[] {
  try {
    return readFileSync(dockerLog, "utf8").split("\n").filter(Boolean);
  } catch {
    return [];
  }
}

function mountSources(): string[] {
  return invocations().map((line) => /source=([^,]+),/.exec(line)?.[1] ?? "");
}

async function runV2(input: unknown, result: unknown, directory = project) {
  let hook: ((event: any) => Promise<void>) | undefined;
  const ctx: any = {
    location: { directory },
    tool: {
      hook: async (name: string, cb: (event: any) => Promise<void>) => {
        expect(name).toBe("execute.after");
        hook = cb;
        return { dispose: async () => {} };
      },
    },
  };
  await plugin.setup(ctx);
  const event: any = { tool: "write", status: "completed", input, result };
  await hook!(event);
  return event;
}

test("V2: notice is pushed onto non-empty content and the mount source is the project", async () => {
  const content = [{ type: "text", text: "Created file successfully: broken.sh" }];
  const event = await runV2({ path: "broken.sh" }, { content });
  expect(event.result.content).toHaveLength(2);
  expect(event.result.content[1].type).toBe("text");
  expect(event.result.content[1].text).toContain("<multilint>");
  expect(event.result.content[1].text).toContain("SC1073");
  expect(mountSources()).toEqual([project]);
  expect(mountSources()[0]).not.toBe(process.cwd());

  // Absolute path in a subdirectory: the mount source is the .git ancestor.
  await runV2({ filePath: path.join(project, "sub", "a.sh") }, { content: [{ type: "text", text: "ok" }] });
  expect(mountSources()[1]).toBe(project);

  // A relative path that climbs out of the project records no further docker invocation.
  const before = invocations().length;
  const escaped = await runV2({ path: "../outside.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(invocations()).toHaveLength(before);
  expect(escaped.result.content).toHaveLength(1);
});

test("V1 and module shape: default-only exports, one hook, notice appended to output", async () => {
  for (const mod of [pluginModule, repoLocal]) {
    expect(Object.keys(mod)).toEqual(["default"]);
    const def: any = (mod as any).default;
    expect(typeof def.id).toBe("string");
    expect(typeof def.setup).toBe("function");
    expect(typeof def.server).toBe("function");
  }
  const hooks = await plugin.server({ directory: project });
  expect(Object.keys(hooks)).toEqual(["tool.execute.after"]);
  const output = { title: "", output: "Wrote file", metadata: {} };
  await hooks["tool.execute.after"]({ tool: "write", sessionID: "s", callID: "c", args: { filePath: "broken.sh" } }, output);
  expect(output.output.startsWith("Wrote file\n\n<multilint>")).toBe(true);
  expect(output.output).toContain("SC1073");
});
