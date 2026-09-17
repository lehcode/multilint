/**
 * multilint — lint edited Python/shell files through the multilint HTTP API.
 *
 * On every edit/write of a .py/.sh/.bash file, extracts the file's
 * directory and tells the multilint server to lint it. Any errors are
 * appended to the tool result so the model sees them in context.
 *
 * Silent if the server is unreachable or no errors are found.
 */

const LINT_URL = "http://localhost:8591/lint"
const LINT_TIMEOUT_MS = 90_000
const LINTABLE_EXT = /\.(?:py|sh|bash)$/

const MultilintPlugin = {
  id: "multilint",
  async setup(ctx: any) {
    await ctx.tool.hook("execute.after", async (event: any) => {
      if (event.status !== "completed") return

      const file =
        event.input?.filePath ??
        event.input?.file_path ??
        event.input?.path
      if (typeof file !== "string" || file === "") return
      if (!LINTABLE_EXT.test(file)) return

      const lastSlash = file.lastIndexOf("/")
      if (lastSlash < 0) return

      const dir = file.slice(0, lastSlash)
      const base = file.slice(lastSlash + 1)

      let lintResult: { return_code?: number; stdout?: string; stderr?: string }
      try {
        const response = await fetch(LINT_URL, {
          method: "POST",
          body: JSON.stringify({ path: `./${base}`, cwd: dir }),
          signal: AbortSignal.timeout(LINT_TIMEOUT_MS),
        })
        lintResult = await response.json()
      } catch {
        return
      }

      if (!lintResult || lintResult.return_code === 0) return

      event.result = {
        ...event.result,
        output:
          event.result.output +
          "\n\n<multilint>\n" +
          `multilint reported errors in ${file}. Fix them before continuing.\n\n` +
          `${lintResult.stdout ?? ""}\n` +
          `${lintResult.stderr ?? ""}\n` +
          `</multilint>`,
      }
    })
  },
}

export default MultilintPlugin
