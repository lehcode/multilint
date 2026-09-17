/**
 * multilint-lint.js — OpenCode plugin that auto-lints files after save.
 *
 * Listens for `file.edited` events, filters by lintable extension whitelist,
 * and calls the multilint HTTP API (POST /lint) with the changed file's
 * parent directory. Results are logged to console; never blocks the save.
 */

export const MultilintLintPlugin = async () => {
  // Lintable file extensions (matching lint.sh discovery patterns)
  const LINTABLE_EXTENSIONS = new Set([
    ".sh", ".bash",   // shell scripts
    ".py",            // Python
    ".md",            // Markdown
    ".yaml", ".yml",  // YAML
    ".json",          // JSON
    ".toml",          // TOML
  ])

  const MULTILINT_HOST = process.env.MULTILINT_HOST || "http://localhost:8591"

  return {
    "file.edited": async ({ data }) => {
      const filePath = data?.path
      if (!filePath) return

      // Extract extension (correctly handles nested paths)
      const ext = "." + filePath.slice(filePath.lastIndexOf(".") + 1)
      if (!LINTABLE_EXTENSIONS.has(ext)) return

      // Extract parent directory
      const lastSlash = filePath.lastIndexOf("/")
      const parentDir = lastSlash >= 0 ? filePath.slice(0, lastSlash) : "."

      // Non-blocking HTTP call
      try {
        const res = await fetch(`${MULTILINT_HOST}/lint`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: parentDir }),
        })
        const result = await res.json()
        const status = result.return_code === 0 ? "✓ PASS" : "✗ FAIL"
        console.log(`[multilint] ${status} ${filePath} (dir: ${parentDir})`)
        if (result.stderr) {
          const lines = result.stderr.split("\n").filter(Boolean).slice(0, 3)
          lines.forEach((line) => console.log(`  ${line}`))
        }
      } catch (err) {
        console.log(`[multilint] ⚠ Could not reach server at ${MULTILINT_HOST}: ${err.message}`)
      }
    },
  }
}
