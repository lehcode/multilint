/**
 * @lehcode/multilint — Dual-mode plugin (V1 + V2)
 *
 * Ships both OpenCode V1 and V2 implementations so it works on any
 * installed version. V2 takes priority; V1 `server()` runs only when
 * the V2 entrypoint is not detected.
 *
 * Install:
 *   opencode plugin add @lehcode/multilint@latest
 *
 * Requires the multilint Docker container running (default :8591).
 * Override with MULTILINT_HOST env var.
 */

import { Plugin } from "@opencode/plugin"

const HOST = process.env.MULTILINT_HOST ?? "http://localhost:8591"

// ─── V2 implementation ────────────────────────────────────────────

async function setupV2(ctx: any) {
  // Auto-register the MCP server (only if not already configured)
  await ctx.mcp.transform((editor: any) => {
    const exists = editor.get("multilint")
    if (exists) return
    editor.set("multilint", {
      type: "remote",
      url: `${HOST}/mcp`,
    })
  })

  // On-demand linting command
  await ctx.command.transform((editor: any) => {
    editor.add({
      name: "multilint",
      description:
        "Run code quality checks on a directory via the multilint service.",
      execute: async ({ sessionID, prompt }: any) => {
        try {
          const res = await fetch(`${HOST}/lint`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: "." }),
          })
          const result = await res.json()

          const text =
            result.return_code === 0
              ? "✅ Linting passed — all checks OK."
              : `Linting failed.\n${result.stderr || "(no details)"}`

          await ctx.session.prompt({
            sessionID,
            text,
          })
        } catch {
          await ctx.session.prompt({
            sessionID,
            text: `❌ Could not reach multilint at ${HOST}. Start the Docker container.`,
          })
        }
      },
    })
  })

  // Post-save auto-lint hook
  await ctx.session.hook("context", (event: any) => {
    // Nothing to do at model-request level; use execute.after for file edits
  })

  // File edit listener (runs after every edit)
  await ctx.tool.hook("execute.after", async (event: any) => {
    if (event.status !== "completed") return

    const toolName = event.tool
    const input = event.input as { filePath?: string }

    if (!input?.filePath) return

    const file = input.filePath
    const lastDot = file.lastIndexOf(".")
    if (lastDot <= 0) return

    const ext = "." + file.slice(lastDot + 1)
    const LINTABLE = new Set([
      ".sh",
      ".bash",
      ".py",
      ".md",
      ".yaml",
      ".yml",
      ".json",
      ".toml",
    ])
    if (!LINTABLE.has(ext)) return

    const dir = file.slice(0, file.lastIndexOf("/")) || "."
    try {
      const res = await fetch(`${HOST}/lint`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: dir }),
      })
      const result = await res.json()

      if (result.return_code !== 0 && result.stderr) {
        const lines = result.stderr
          .split("\n")
          .filter(Boolean)
          .slice(0, 5)
        lines.forEach((l: string) => console.log(`[multilint] ${l}`))
      }
    } catch {
      console.log(`[multilint] ⚠ unreachable at ${HOST}`)
    }
  })
}

// ─── V1 implementation ────────────────────────────────────────────

async function serverV1() {
  const LINTABLE = new Set([
    ".sh",
    ".bash",
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
  ])

  return {
    "execute.after": async (event: any) => {
      if (event.status !== "completed") return

      const input = event.input as { filePath?: string }
      if (!input?.filePath) return

      const file = input.filePath
      const lastDot = file.lastIndexOf(".")
      if (lastDot <= 0) return

      const ext = "." + file.slice(lastDot + 1)
      if (!LINTABLE.has(ext)) return

      const dir = file.slice(0, file.lastIndexOf("/")) || "."
      try {
        const res = await fetch(`${HOST}/lint`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: dir }),
        })
        const result = await res.json()

        if (result.return_code !== 0 && result.stderr) {
          const lines = result.stderr
            .split("\n")
            .filter(Boolean)
            .slice(0, 5)
          lines.forEach((l: string) => console.log(`[multilint] ${l}`))
        }
      } catch {
        console.log(`[multilint] ⚠ unreachable at ${HOST}`)
      }
    },
  }
}

// ─── Dual-mode export ─────────────────────────────────────────────

export default {
  ...Plugin.define({
    id: "@lehcode/multilint",
    async setup(ctx: any) {
      await setupV2(ctx)
    },
  }),
  async server() {
    return serverV1()
  },
}
