/**
 * multilint-lint.js — OpenCode V2 plugin that auto-lints files after save.
 *
 * Listens for tool execution completion (execute.after), extracts the file
 * path from the tool input, checks whether the extension is lintable, and
 * calls the multilint HTTP API (POST /lint) with the file's parent directory.
 * Any errors are appended to the tool result so the agent sees them in context.
 *
 * Silent if the server is unreachable or no errors are found.
 */

export default {
  id: "multilint-lint",

  async setup(ctx) {
    const LINT_URL = process.env.MULTILINT_HOST || "http://localhost:8591/lint";
    const LINT_TIMEOUT_MS = 90_000;

    const LINTABLE_EXTENSIONS = new Set([
      ".sh", ".bash",   // shell scripts
      ".py",            // Python
      ".md",            // Markdown
      ".yaml", ".yml",  // YAML
      ".json",          // JSON
      ".toml",          // TOML
    ]);

    // Hook into tool execution so we can lint after files are written
    ctx.tool.hook("execute.after", async (event) => {
      if (event.status !== "completed") return;

      // Extract the file path from various possible input shapes
      const filePath =
        event.input?.filePath ||
        event.input?.file_path ||
        event.input?.path;

      if (typeof filePath !== "string" || filePath === "") return;

      // Extract extension (handles nested paths)
      const lastDot = filePath.lastIndexOf(".");
      if (lastDot < 0) return;
      const ext = filePath.slice(lastDot);

      if (!LINTABLE_EXTENSIONS.has(ext)) return;

      // Extract parent directory
      const lastSlash = filePath.lastIndexOf("/");
      const parentDir = lastSlash >= 0 ? filePath.slice(0, lastSlash) : ".";
      const baseName = lastSlash >= 0 ? filePath.slice(lastSlash + 1) : filePath;

      // Call the multilint HTTP API
      let lintResult;
      try {
        const response = await fetch(LINT_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: parentDir }),
          signal: AbortSignal.timeout(LINT_TIMEOUT_MS),
        });
        lintResult = await response.json();
      } catch {
        // Server unreachable — silently skip
        return;
      }

      if (!lintResult || lintResult.return_code === 0) return;

      // Append lint errors to the tool result so the agent sees them
      event.result = {
        ...event.result,
        output:
          (event.result?.output || "") +
          "\n\n<multilint>\n" +
          `multilint reported errors in ${filePath}. Fix them before continuing.\n\n` +
          `${lintResult.stdout ?? ""}\n` +
          `${lintResult.stderr ?? ""}\n` +
          `</multilint>`,
      };
    });
  },
};
