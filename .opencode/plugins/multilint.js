/**
 * multilint.js — OpenCode V2 plugin that lints a file after it is written.
 *
 * Runs one throwaway container per write:
 *
 *   docker run --rm --network none --memory 2g --cpus 2 \
 *     --mount type=bind,source=<root>,target=<root>,readonly \
 *     --workdir <root> --entrypoint bash <image> -c 'lint.sh <file> --format json'
 *
 * The bind mount is an identity mount, so the host path and the container path are the same string.
 *
 * WHY THIS WAS REWRITTEN — the previous version reported success without linting anything.
 * It posted `{ path: parentDir }` to the multilint HTTP API, where parentDir was a *host* path and
 * no `cwd` was sent. The container resolved that path against its own filesystem, found nothing
 * there, and every category came back "(none found)" with `return_code: 0`. The plugin then took the
 * zero exit as a pass. Reproduced against the live service: `All checks passed ✓` for a directory
 * the container could not see. A check that cannot fail is worse than no check, because it is
 * indistinguishable from a passing one.
 *
 * POSIX only. WSL counts and needs nothing extra; native Windows is not a target, so process.getuid
 * is called directly rather than guarded.
 *
 * Silent when docker is missing, when the container cannot be run, or when nothing is wrong.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import fs from "node:fs/promises";
import os from "node:os";
import { Database } from "bun:sqlite";

const execFileAsync = promisify(execFile);

// Published on Docker Hub, which serves anonymous pulls. Override with the "image" hook-setting
// (`python3 claude-plugin/scripts/multilint.py --set image local/build:dev`) to test a local
// build; this plugin only ever reads that setting, never writes it.
const DEFAULT_IMAGE = "lehcode/multilint:latest";
const CONTAINER_LINT_SH = "/usr/local/bin/lint.sh";

// Generous: a first run may have to pull ~506MB, and a timeout reports the file as unchecked
// rather than clean.
const RUN_TIMEOUT_MS = 300_000;

const MEMORY_LIMIT = "2g";
const CPU_LIMIT = "2";

// Cap on text handed to the agent. A failing file's full output runs to several kilobytes.
const MAX_OUTPUT_CHARS = 4000;

// The message lint.sh gives a formatter finding; the check's "fix" line already says it.
const FORMAT_ONLY_MESSAGE = "formatting required";

const LINTABLE_EXTENSIONS = new Set([
  ".sh",
  ".bash", // shell scripts
  ".py", // Python
  ".md", // Markdown
  ".yaml",
  ".yml", // YAML
  ".json", // JSON
  ".toml", // TOML
]);

// Nothing is filtered any more. gitleaks used to be listed here because lint.sh gated it on
// "<target>/.git" and a single-file target can never satisfy that, so it was reported skipped after
// every write. lint.sh now picks --no-git when there is no repository, so the check produces a real
// verdict either way and a reported skip means something again.
const STRUCTURALLY_SKIPPED = new Set();

/**
 * Directory holding the settings database, mirroring state_dir() in multilint.py exactly: same
 * fixed path, same XDG_STATE_HOME honouring, no MULTILINT_STATE_DIR equivalent.
 */
function stateDir() {
  const xdg = process.env.XDG_STATE_HOME;
  // The XDG spec makes a relative value invalid, to be ignored; honouring it would look for the
  // database relative to whatever directory OpenCode runs in.
  const base =
    xdg && path.isAbsolute(xdg) ? xdg : path.join(os.homedir(), ".local", "state");
  return path.join(base, "multilint");
}

/**
 * Read-only lookup into the settings table the Python hook's --set/--get/--unset CLI writes.
 * `{ readonly: true, create: false }` so a missing file cannot be created by the read path itself.
 * A missing database file, a missing table and a missing row all resolve to null (-> built-in
 * default) through the same catch, rather than three separately-tested failure paths.
 */
function readSetting(key) {
  try {
    const db = new Database(path.join(stateDir(), "changes.db"), {
      readonly: true,
      create: false,
    });
    try {
      // Wait out a concurrent hook write, as the Python side does (timeout=10), instead of
      // failing SQLITE_BUSY at once and quietly linting with the default image.
      db.exec("PRAGMA busy_timeout = 10000");
      const row = db.query("SELECT value FROM settings WHERE key = ?").get(key);
      return row?.value ?? null;
    } finally {
      db.close();
    }
  } catch (error) {
    // An absent db or table just means nothing is set. Anything else (still locked, corrupt)
    // falls back to the default too, but says so: silently linting with another image would
    // report on a different toolchain than the one configured.
    if (!/unable to open|no such table/i.test(String(error?.message))) {
      console.error(`[multilint] could not read the "${key}" setting, using the default: ${error?.message}`);
    }
    return null;
  }
}

/** Image to run. The "image" hook-setting overrides the published default; this plugin never writes it. */
function resolveImage() {
  return readSetting("image") || DEFAULT_IMAGE;
}

/** Nearest ancestor containing .git, or null. Mirrors the scope-root rule in multilint.py. */
async function findGitRoot(startDir) {
  let current = path.resolve(startDir);
  for (;;) {
    try {
      await fs.access(path.join(current, ".git"));
      return current;
    } catch {
      const parent = path.dirname(current);
      if (parent === current) return null;
      current = parent;
    }
  }
}

/**
 * Build the docker argv. Separated out and free of side effects so it can be reasoned about, and
 * so the arguments are visible in one place.
 *
 * --mount rather than -v: the `-v src:dst:opts` form is colon-delimited and easy to corrupt. In zsh
 * `"$PWD:$PWD:ro"` silently becomes `.../multilint:.../multilinto`, because `:r` is a parameter
 * modifier that applies even inside double quotes — which mounts read-write at the wrong target and
 * lints an empty directory. execFile passes an argv array so no shell is involved here, but the
 * named-key form stays unambiguous for anyone copying it.
 */
function buildDockerArgs(scopeRoot, relativePath) {
  return [
    "run",
    "--rm",
    "--network",
    "none",
    "--memory",
    MEMORY_LIMIT,
    "--cpus",
    CPU_LIMIT,
    "--mount",
    `type=bind,source=${scopeRoot},target=${scopeRoot},readonly`,
    // lint.sh resolves .multilint.json and .markdownlint.json relative to the working directory.
    "--workdir",
    scopeRoot,
    "--entrypoint",
    "bash",
    // Run as the invoking user so nothing in the container acts as root, whatever USER the image
    // declares. POSIX only, which includes WSL; native Windows is not a target.
    "--user",
    `${process.getuid()}:${process.getgid()}`,
    resolveImage(),
    "-c",
    `bash ${CONTAINER_LINT_SH} "$1" --format json`,
    "_",
    relativePath,
  ];
}

/**
 * Failed check names and their notice blocks, built from the "findings"/"fix" fields. Mirrors
 * finding_blocks() in multilint.py. Both are empty for a document from an older image, whose
 * checks carry counts only, so the caller can fall back to the marker lines.
 */
function findingBlocks(document) {
  const names = [];
  const blocks = [];
  for (const [name, check] of Object.entries(document.checks || {})) {
    if (!check || check.status !== "failed" || !Array.isArray(check.findings)) continue;
    names.push(name);
    const lines = [typeof check.fix === "string" && check.fix ? `${name} — ${check.fix}` : name];
    for (const item of check.findings) {
      if (!item || item.message === FORMAT_ONLY_MESSAGE) continue;
      const location =
        item.line !== null && item.line !== undefined && item.file ? `${item.file}:${item.line}` : item.file || "";
      const head = [item.rule, item.symbol, location].filter(Boolean).join(" ");
      lines.push(`  ${head}  ${item.message || ""}`.trimEnd());
    }
    if (check.findings_truncated) lines.push("  … more findings omitted");
    blocks.push(lines.join("\n"));
  }
  return { names, blocks };
}

/** The structured failure notice: header, findings, Rules footer. Only the findings are cut to the cap. */
function structuredNotice(relativePath, document, { names, blocks }) {
  const header = `multilint: ${relativePath} failed ${names.join(", ")}`;
  const rules = Array.isArray(document.summary?.rules_violated)
    ? document.summary.rules_violated.filter((r) => typeof r === "string")
    : [];
  const footer = rules.length ? `Rules: ${rules.join(" ")}` : "";
  let body = blocks.join("\n");
  const budget = MAX_OUTPUT_CHARS - header.length - footer.length - 4;
  if (body.length > budget) {
    body = body.slice(0, Math.max(budget - 2, 0)).split("\n").slice(0, -1).join("\n") + "\n…";
  }
  return [header, body, footer].filter(Boolean).join("\n\n");
}

export default {
  id: "multilint",

  async setup(ctx) {
    ctx.tool.hook("execute.after", async (event) => {
      if (event.status !== "completed") return;

      const filePath =
        event.input?.filePath || event.input?.file_path || event.input?.path;
      if (typeof filePath !== "string" || filePath === "") return;
      if (!LINTABLE_EXTENSIONS.has(path.extname(filePath))) return;

      // An absolute path is required: the mount source has to be a real host path, and a relative
      // one would be resolved against whatever cwd the agent happens to have.
      const absolute = path.resolve(filePath);
      const scopeRoot =
        (await findGitRoot(path.dirname(absolute))) || path.dirname(absolute);
      const relativePath = path.relative(scopeRoot, absolute);
      if (relativePath === "" || relativePath.startsWith("..")) return;

      let document;
      let detail = "";
      try {
        const { stdout, stderr } = await execFileAsync(
          "docker",
          buildDockerArgs(scopeRoot, relativePath),
          {
            timeout: RUN_TIMEOUT_MS,
            maxBuffer: 16 * 1024 * 1024,
          },
        );
        document = JSON.parse(stdout);
        detail = stderr;
      } catch (error) {
        // docker exits non-zero when lint.sh finds problems, and execFile treats that as a throw,
        // so the failure path still carries the result. Only give up when there is no parsable
        // document — a missing docker binary, an unpullable image, or a timeout.
        if (error?.stdout) {
          try {
            document = JSON.parse(error.stdout);
            detail = error.stderr || "";
          } catch {
            return;
          }
        } else {
          return;
        }
      }

      if (!document || typeof document !== "object") return;

      const skipped = (document.summary?.checks_skipped || []).filter(
        (name) => !STRUCTURALLY_SKIPPED.has(name),
      );
      const failed = document.return_code !== 0;
      // Mirrors config_warnings() in multilint.py: tolerant of a missing or malformed field,
      // and surfaced independent of return_code -- a malformed .multilint.json is worth knowing
      // about even on an otherwise-clean run.
      const warnings = Array.isArray(document.warnings)
        ? document.warnings.filter((w) => typeof w === "string")
        : [];
      if (!failed && skipped.length === 0 && warnings.length === 0) return;

      const notices = [];
      const structured = findingBlocks(document);
      if (failed && structured.names.length) {
        notices.push(structuredNotice(relativePath, document, structured));
      } else if (failed) {
        // Older image, no findings: only the marked lines; the full transcript is mostly passing checks.
        const lines = detail
          .split("\n")
          .filter((line) => line.includes("✗") || line.includes("⚠"))
          .map((line) => line.trimEnd());
        const body = (
          lines.length
            ? lines.join("\n")
            : JSON.stringify(document.checks, null, 2)
        ).slice(0, MAX_OUTPUT_CHARS);
        notices.push(
          `multilint reported failing checks in ${relativePath}. Fix them before continuing.\n\n${body}`,
        );
      }
      if (skipped.length) {
        // Reported even when everything passed: a check that did not run is not a check that passed.
        notices.push(
          `multilint could not run these checks, so ${relativePath} is unverified for them: ` +
            skipped.join(", "),
        );
      }
      if (warnings.length) {
        notices.push("multilint: config warning: " + warnings.join("; "));
      }

      event.result = {
        ...event.result,
        output:
          (event.result?.output || "") +
          "\n\n<multilint>\n" +
          notices.join("\n\n") +
          "\n</multilint>",
      };
    });
  },
};
