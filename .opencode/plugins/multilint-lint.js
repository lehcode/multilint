/**
 * multilint-lint.js — OpenCode V2 plugin that lints a file after it is written.
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

const execFileAsync = promisify(execFile);

// Published on Docker Hub, which serves anonymous pulls. Override to test a local build.
const DEFAULT_IMAGE = "lehcode/multilint:latest";
const CONTAINER_LINT_SH = "/usr/local/bin/lint.sh";

// Generous: a first run may have to pull ~506MB, and a timeout reports the file as unchecked
// rather than clean.
const RUN_TIMEOUT_MS = 300_000;

const MEMORY_LIMIT = "2g";
const CPU_LIMIT = "2";

// Cap on text handed to the agent. A failing file's full output runs to several kilobytes.
const MAX_OUTPUT_CHARS = 4000;

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

// gitleaks scans git history and lint.sh only runs it when "<target>/.git" exists. The target here
// is a single file, so that test can never pass. Its skip is structural rather than informative, so
// it is filtered out instead of being reported after every write.
const STRUCTURALLY_SKIPPED = new Set(["gitleaks"]);

/** Nearest ancestor containing .git, or null. Mirrors the scope-root rule in lint_changed.py. */
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
    process.env.MULTILINT_IMAGE || DEFAULT_IMAGE,
    "-c",
    `bash ${CONTAINER_LINT_SH} "$1" --format json`,
    "_",
    relativePath,
  ];
}

export default {
  id: "multilint-lint",

  async setup(ctx) {
    if (process.env.MULTILINT_HOST) {
      console.error(
        "[multilint] MULTILINT_HOST is set but no longer used: linting now runs a container " +
          "per write instead of calling an HTTP API. Set MULTILINT_IMAGE to choose an image.",
      );
    }

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
      if (!failed && skipped.length === 0) return;

      const notices = [];
      if (failed) {
        // Only the marked lines; the full transcript is mostly passing checks.
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
