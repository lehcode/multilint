// End-to-end tests for the OpenCode plugin: the adapters drive the real scripts/multilint.sh, which
// lints in an ephemeral Docker container. Tests that need a lint result skip when docker or the image
// is absent; MULTILINT_TEST_IMAGE selects a local build instead of the pinned release image.
import { afterAll, beforeAll, expect, test } from "bun:test";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import plugin from "../src/plugin.ts";
import * as pluginModule from "../src/plugin.ts";
import { MULTILINT_SH } from "../src/lint.ts";

const DEFECTIVE_SH = "#!/bin/bash\necho $1\nif [ $x = 1 ]; then echo; fi\n";
const SCRIPTS = path.dirname(MULTILINT_SH);
const IMAGE =
  process.env.MULTILINT_TEST_IMAGE ||
  execFileSync("python3", ["-c", "import sys; sys.path.insert(0, sys.argv[1]); import lint_changed; print(lint_changed.DEFAULT_IMAGE)", SCRIPTS])
    .toString()
    .trim();
// Never pulled: a missing image skips the container tests instead of downloading ~500 MB.
const withImage = spawnSync("docker", ["image", "inspect", IMAGE], { stdio: "ignore" }).status === 0 ? test : test.skip;

let root: string;
let project: string;
const saved = { PATH: process.env.PATH, XDG_STATE_HOME: process.env.XDG_STATE_HOME };

beforeAll(() => {
  root = realpathSync(mkdtempSync(path.join(tmpdir(), "multilint-js-")));
  project = path.join(root, "project");
  mkdirSync(path.join(project, "sub"), { recursive: true });
  execFileSync("git", ["init", "-q"], { cwd: project });
  writeFileSync(path.join(project, "broken.sh"), DEFECTIVE_SH);
  writeFileSync(path.join(project, "sub", "a.sh"), DEFECTIVE_SH);
  // Defective and inside the search ceiling, so only the plugin's path guard keeps it unlinted.
  writeFileSync(path.join(root, "outside.sh"), DEFECTIVE_SH);
  writeFileSync(path.join(project, "..dotted.sh"), DEFECTIVE_SH);
  process.env.XDG_STATE_HOME = path.join(root, "state");
  // The hook never searches above its ceiling ($HOME by default); the temp dir is outside $HOME.
  const lintChanged = path.join(SCRIPTS, "lint_changed.py");
  execFileSync("python3", [lintChanged, "--set", "image", IMAGE]);
  execFileSync("python3", [lintChanged, "--set", "search_ceiling", root]);
});

afterAll(() => {
  process.env.PATH = saved.PATH;
  if (saved.XDG_STATE_HOME === undefined) delete process.env.XDG_STATE_HOME;
  else process.env.XDG_STATE_HOME = saved.XDG_STATE_HOME;
  rmSync(root, { recursive: true, force: true });
});

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

withImage("V2: a defective file's findings are pushed onto the tool result", async () => {
  const event = await runV2({ path: "broken.sh" }, { content: [{ type: "text", text: "Created broken.sh" }] });
  expect(event.result.content).toHaveLength(2);
  expect(event.result.content[1].text).toStartWith("<multilint>");
  expect(event.result.content[1].text).toContain("shellcheck");

  const nested = await runV2({ filePath: path.join(project, "sub", "a.sh") }, { content: [{ type: "text", text: "ok" }] });
  expect(nested.result.content[1].text).toContain("shellcheck");
});

withImage("V2: a relative path outside the project is not linted", async () => {
  const event = await runV2({ path: "../outside.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(event.result.content).toHaveLength(1);
  // The same file named by its absolute path is linted, which proves the guard above did the work.
  const absolute = await runV2({ path: path.join(root, "outside.sh") }, { content: [{ type: "text", text: "ok" }] });
  expect(absolute.result.content[1].text).toContain("shellcheck");
});

withImage("V2: a file whose name starts with '..' is inside the project", async () => {
  const event = await runV2({ path: "..dotted.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(event.result.content[1].text).toContain("shellcheck");
});

withImage("V1 and module shape: default-only exports, one hook, notice appended to output", async () => {
  expect(Object.keys(pluginModule)).toEqual(["default"]);
  const hooks = await plugin.server({ directory: project });
  expect(Object.keys(hooks)).toEqual(["tool.execute.after"]);
  writeFileSync(path.join(project, "v1.sh"), DEFECTIVE_SH);
  const output = { title: "", output: "Wrote file", metadata: {} };
  await hooks["tool.execute.after"]({ tool: "write", sessionID: "s", callID: "c", args: { filePath: "v1.sh" } }, output);
  expect(output.output.startsWith("Wrote file\n\n<multilint>")).toBe(true);
  expect(output.output).toContain("shellcheck");
});

test("a crashed run is reported, never silent", async () => {
  writeFileSync(path.join(project, "crash.sh"), DEFECTIVE_SH);
  const bin = path.join(root, "crash-bin");
  mkdirSync(bin, { recursive: true });
  // A python3 that dies: multilint.sh execs it, so the script exits non-zero with no output.
  writeFileSync(path.join(bin, "python3"), "#!/bin/sh\nexit 3\n", { mode: 0o755 });
  process.env.PATH = `${bin}:${saved.PATH}`;
  try {
    const event = await runV2({ path: "crash.sh" }, { content: [{ type: "text", text: "ok" }] });
    expect(event.result.content[1].text).toContain("multilint.sh exited with status 3");
  } finally {
    process.env.PATH = saved.PATH;
  }
});

test("a run that cannot start is reported, never silent", async () => {
  writeFileSync(path.join(project, "nobash.sh"), DEFECTIVE_SH);
  process.env.PATH = path.join(root, "empty-bin");
  try {
    const event = await runV2({ path: "nobash.sh" }, { content: [{ type: "text", text: "ok" }] });
    expect(event.result.content[1].text).toContain("multilint could not lint");
    expect(event.result.content[1].text).toContain("unverified");
  } finally {
    process.env.PATH = saved.PATH;
  }
});
