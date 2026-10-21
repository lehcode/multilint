// Unit tests for the TUI notice decision: pure classification and state logic, no docker and no
// real state file. The state file itself round-trips through XDG_STATE_HOME, which the e2e suite
// already points at a temp root.
import { expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { classifyReport, decideNotice, loadState, saveState } from "../src/notice.ts";

const FILE = "/home/dev/project/error-mocks/shell-script.sh";
const OTHER = "/home/dev/project/other.sh";
const REL = "error-mocks/shell-script.sh";
const failReport = `multilint: ${REL} failed bashate, shfmt

bashate: FAILED
  E003: line 7
shfmt: FAILED

multilint could not run these checks, so ${REL} is unverified for them: gitleaks`;

test("classify: a structured failure header names the file and its failing checks", () => {
  expect(classifyReport(failReport)).toEqual({ kind: "failed", rel: REL, checks: ["bashate", "shfmt"] });
});

test("classify: a single-check failure still classifies", () => {
  const report = `multilint: ${REL} failed shfmt

multilint could not run these checks, so ${REL} is unverified for them: gitleaks`;
  expect(classifyReport(report)).toEqual({ kind: "failed", rel: REL, checks: ["shfmt"] });
});

test("classify: a fallback failure has no check names", () => {
  const report = `multilint found failing checks in ${REL}.

<findings>`;
  expect(classifyReport(report)).toEqual({ kind: "failed", rel: REL, checks: [] });
});

test("classify: a could-not-lint report keeps the reason, drops the model-facing tail", () => {
  const report = `multilint could not lint ${REL}: docker daemon not running. Treat the file as unverified.`;
  expect(classifyReport(report)).toEqual({ kind: "error", rel: REL, reason: "docker daemon not running" });
});

test("classify: a plugin-side could-not-lint report uses the absolute path", () => {
  const report = `multilint could not lint ${FILE}: multilint.sh exited with status 3. Treat the file as unverified.`;
  expect(classifyReport(report)).toEqual({ kind: "error", rel: FILE, reason: "multilint.sh exited with status 3" });
});

test("classify: a skipped-only report is a pass", () => {
  const report = `multilint could not run these checks, so ${REL} is unverified for them: gitleaks`;
  expect(classifyReport(report)).toEqual({ kind: "passed" });
});

test("classify: a config-warning-only report is a pass", () => {
  expect(classifyReport(`multilint: config warning: unknown key "foo"`)).toEqual({ kind: "passed" });
});

test("decide: a failed run emits its header line and records the failing checks", () => {
  const { line, state } = decideNotice(FILE, classifyReport(failReport), {});
  expect(line).toBe(`multilint: ${REL} failed bashate, shfmt`);
  expect(state).toEqual({ [FILE]: { rel: REL, checks: ["bashate", "shfmt"] } });
});

test("decide: every failed edit re-emits, and the last failure's checks win", () => {
  const first = decideNotice(FILE, classifyReport(failReport), {});
  const second = decideNotice(FILE, classifyReport(`multilint: ${REL} failed bashate`), first.state);
  expect(second.line).toBe(`multilint: ${REL} failed bashate`);
  expect(second.state).toEqual({ [FILE]: { rel: REL, checks: ["bashate"] } });
});

test("decide: a fallback failure emits without check names and recovers as failed checks", () => {
  const failed = decideNotice(OTHER, classifyReport(`multilint found failing checks in other.sh.`), {});
  expect(failed.line).toBe(`multilint: other.sh failed`);
  const recovered = decideNotice(OTHER, { kind: "passed" }, failed.state);
  expect(recovered.line).toBe(`multilint: other.sh clean (was: failed checks)`);
  expect(recovered.state).toEqual({});
});

test("decide: a pass after a failure emits the recovery line and clears the state", () => {
  const failed = decideNotice(FILE, classifyReport(failReport), {});
  const recovered = decideNotice(FILE, { kind: "passed" }, failed.state);
  expect(recovered.line).toBe(`multilint: ${REL} clean (was: bashate, shfmt)`);
  expect(recovered.state).toEqual({});
});

test("decide: a pass after a could-not-lint recovers as such", () => {
  const errored = decideNotice(
    FILE,
    classifyReport(`multilint could not lint ${REL}: docker daemon not running. Treat the file as unverified.`),
    {},
  );
  expect(errored.line).toBe(`multilint: could not lint ${REL}: docker daemon not running`);
  const recovered = decideNotice(FILE, { kind: "passed" }, errored.state);
  expect(recovered.line).toBe(`multilint: ${REL} clean (was: could not lint)`);
  expect(recovered.state).toEqual({});
});

test("decide: a clean file that never failed is silent", () => {
  const { line, state } = decideNotice(FILE, { kind: "passed" }, {});
  expect(line).toBeNull();
  expect(state).toEqual({});
});

test("decide: a null report (unchanged file) leaves the state untouched", () => {
  const failed = decideNotice(FILE, classifyReport(failReport), {});
  const after = decideNotice(FILE, null, failed.state);
  expect(after.line).toBeNull();
  expect(after.state).toBe(failed.state);
});

test("decide: one file failing never shadows another file's recovery", () => {
  const a = decideNotice(FILE, classifyReport(failReport), {});
  const b = decideNotice(OTHER, classifyReport(`multilint: other.sh failed bashate`), a.state);
  expect(b.state).toEqual({
    [FILE]: { rel: REL, checks: ["bashate", "shfmt"] },
    [OTHER]: { rel: "other.sh", checks: ["bashate"] },
  });
  const recovered = decideNotice(FILE, { kind: "passed" }, b.state);
  expect(recovered.line).toBe(`multilint: ${REL} clean (was: bashate, shfmt)`);
  expect(recovered.state).toEqual({ [OTHER]: { rel: "other.sh", checks: ["bashate"] } });
});

test("state: round-trips through the XDG state directory and resets on corruption", () => {
  const saved = process.env.XDG_STATE_HOME;
  const dir = mkdtempSync(path.join(tmpdir(), "multilint-state-"));
  process.env.XDG_STATE_HOME = dir;
  try {
    expect(loadState()).toEqual({});
    saveState({ [FILE]: { rel: REL, checks: ["bashate"] }, [OTHER]: { rel: "other.sh", checks: null } });
    expect(loadState()).toEqual({
      [FILE]: { rel: REL, checks: ["bashate"] },
      [OTHER]: { rel: "other.sh", checks: null },
    });
    writeFileSync(path.join(dir, "multilint", "failing.json"), "{ not json");
    expect(loadState()).toEqual({});
    writeFileSync(path.join(dir, "multilint", "failing.json"), JSON.stringify({ [FILE]: { rel: 1 } }));
    expect(loadState()).toEqual({});
  } finally {
    if (saved === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = saved;
    rmSync(dir, { recursive: true, force: true });
  }
});
