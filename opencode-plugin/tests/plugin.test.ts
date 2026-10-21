// End-to-end tests for the OpenCode plugin: the adapters drive the real scripts/multilint.sh, which
// lints in an ephemeral Docker container. Tests that need a lint result skip when docker or the image
// is absent; MULTILINT_TEST_IMAGE selects a local build instead of the pinned release image.
import { afterAll, beforeAll, expect, test } from "bun:test";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import plugin from "../src/index.ts";
import * as pluginModule from "../src/index.ts";
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

type SyntheticInput = { sessionID: string; text: string; description: string; resume: boolean };

async function runV2(input: unknown, result: unknown, directory = project) {
  let hook: ((event: any) => Promise<void>) | undefined;
  const notices: SyntheticInput[] = [];
  const ctx: any = {
    location: { directory },
    tool: {
      hook: async (name: string, cb: (event: any) => Promise<void>) => {
        expect(name).toBe("execute.after");
        hook = cb;
        return { dispose: async () => {} };
      },
    },
    // Records the short TUI notice; the real ctx.session.synthetic sends it into the session
    // (resume: false = no model turn).
    session: {
      synthetic: async (synthetic: SyntheticInput) => {
        notices.push(synthetic);
      },
    },
  };
  await plugin.setup(ctx);
  const event: any = { tool: "write", status: "completed", sessionID: "test-session", input, result };
  await hook!(event);
  return { event, notices };
}

withImage("V2: a defective file's findings are pushed onto the tool result", async () => {
  const { event } = await runV2({ path: "broken.sh" }, { content: [{ type: "text", text: "Created broken.sh" }] });
  expect(event.result.content).toHaveLength(2);
  expect(event.result.content[1].text).toStartWith("<multilint>");
  expect(event.result.content[1].text).toContain("shellcheck");

  const { event: nested } = await runV2({ filePath: path.join(project, "sub", "a.sh") }, { content: [{ type: "text", text: "ok" }] });
  expect(nested.result.content[1].text).toContain("shellcheck");
});

withImage("V2: a relative path outside the project is not linted", async () => {
  const { event } = await runV2({ path: "../outside.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(event.result.content).toHaveLength(1);
  // The same file named by its absolute path is linted, which proves the guard above did the work.
  const { event: absolute } = await runV2({ path: path.join(root, "outside.sh") }, { content: [{ type: "text", text: "ok" }] });
  expect(absolute.result.content[1].text).toContain("shellcheck");
});

withImage("V2: a file whose name starts with '..' is inside the project", async () => {
  const { event } = await runV2({ path: "..dotted.sh" }, { content: [{ type: "text", text: "ok" }] });
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
    const { event } = await runV2({ path: "crash.sh" }, { content: [{ type: "text", text: "ok" }] });
    expect(event.result.content[1].text).toContain("multilint.sh exited with status 3");
  } finally {
    process.env.PATH = saved.PATH;
  }
});

test("a run that cannot start is reported, never silent", async () => {
  writeFileSync(path.join(project, "nobash.sh"), DEFECTIVE_SH);
  process.env.PATH = path.join(root, "empty-bin");
  try {
    const { event } = await runV2({ path: "nobash.sh" }, { content: [{ type: "text", text: "ok" }] });
    expect(event.result.content[1].text).toContain("multilint could not lint");
    expect(event.result.content[1].text).toContain("unverified");
  } finally {
    process.env.PATH = saved.PATH;
  }
});

withImage("V2: the TUI notice — silent on clean, one line per failed edit, then the recovery line", async () => {
  const cleanSh = "#!/bin/bash\nset -eu\nif [ \"$1\" = ok ]; then\n    echo ok\nfi\n";
  writeFileSync(path.join(project, "notice.sh"), cleanSh);

  // A clean file that never failed: the report is delivered, the TUI stays quiet.
  const { event: clean, notices: quiet } = await runV2({ path: "notice.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(clean.result.content).toHaveLength(2);
  expect(quiet).toEqual([]);

  // A defect: the report is delivered and exactly one failure line is emitted.
  writeFileSync(path.join(project, "notice.sh"), DEFECTIVE_SH);
  const { event: failed, notices } = await runV2({ path: "notice.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(failed.result.content[1].text).toContain("failed");
  expect(notices).toHaveLength(1);
  expect(notices[0].sessionID).toBe("test-session");
  expect(notices[0].description).toBe(notices[0].text);
  expect(notices[0].resume).toBe(false);
  const was = notices[0].text.slice("multilint: notice.sh failed ".length);

  // The fix: exactly one recovery line naming the checks that failed.
  writeFileSync(path.join(project, "notice.sh"), cleanSh);
  const { notices: recovered } = await runV2({ path: "notice.sh" }, { content: [{ type: "text", text: "ok" }] });
  expect(recovered).toEqual([
    {
      sessionID: "test-session",
      text: `multilint: notice.sh clean (was: ${was})`,
      description: `multilint: notice.sh clean (was: ${was})`,
      resume: false,
    },
  ]);
});
