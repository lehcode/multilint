#!/usr/bin/env node
// Mock OpenAI-compatible provider and assertions for the OpenCode compatibility harness.
//   serve   scripted model: one `write` tool call, then "Done."; logs requests to $MOCK_LOG
//   check   asserts the model received the <multilint> notice (see run.sh --help)
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const PORT = Number(process.env.MOCK_PORT || 18765);
const LOG = process.env.MOCK_LOG || "requests.jsonl";
const PROJECT = process.env.MOCK_PROJECT || process.cwd();
const BROKEN_SCRIPT = "#!/usr/bin/env bash\nif [ -z \"$1\" ; then\n  echo broken\n";

const textOf = (content) =>
  typeof content === "string"
    ? content
    : Array.isArray(content)
      ? content.map((p) => (typeof p?.text === "string" ? p.text : "")).join("\n")
      : "";

function scriptedReply(body) {
  const tools = Array.isArray(body.tools) ? body.tools : [];
  const write = tools.find((t) => t?.function?.name === "write");
  const hasToolResult = (body.messages || []).some((m) => m.role === "tool");
  if (!write || hasToolResult) return { text: "Done." };
  const props = write.function.parameters?.properties || {};
  const key = "filePath" in props ? "filePath" : "path";
  const args = { [key]: path.join(PROJECT, "broken.sh"), content: BROKEN_SCRIPT };
  return { call: { name: "write", arguments: JSON.stringify(args) } };
}

function chunk(id, delta, finish) {
  return `data: ${JSON.stringify({ id, object: "chat.completion.chunk", created: 0, model: "mock-model", choices: [{ index: 0, delta, finish_reason: finish ?? null }] })}\n\n`;
}

function respond(res, body, reply) {
  const id = "mock-1";
  const finish = reply.call ? "tool_calls" : "stop";
  const message = reply.call
    ? { role: "assistant", content: null, tool_calls: [{ id: "call_1", type: "function", function: reply.call }] }
    : { role: "assistant", content: reply.text };
  if (!body.stream) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ id, object: "chat.completion", created: 0, model: "mock-model", choices: [{ index: 0, message, finish_reason: finish }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
    return;
  }
  res.writeHead(200, { "content-type": "text/event-stream" });
  res.write(chunk(id, { role: "assistant" }));
  res.write(chunk(id, reply.call ? { tool_calls: [{ index: 0, id: "call_1", type: "function", function: reply.call }] } : { content: reply.text }));
  res.write(chunk(id, {}, finish));
  res.end("data: [DONE]\n\n");
}

function serve() {
  http
    .createServer((req, res) => {
      if (req.method === "GET" && req.url.endsWith("/models")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ object: "list", data: [{ id: "mock-model", object: "model" }] }));
        return;
      }
      let raw = "";
      req.on("data", (d) => (raw += d));
      req.on("end", () => {
        let body = {};
        try {
          body = JSON.parse(raw);
        } catch {
          // an unparsable body gets the default reply
        }
        fs.appendFileSync(LOG, JSON.stringify(body) + "\n");
        respond(res, body, scriptedReply(body));
      });
    })
    .listen(PORT, "127.0.0.1", () => console.log(`mock provider on 127.0.0.1:${PORT}`));
}

function check(argv) {
  const opt = (name) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : undefined);
  const requests = fs
    .readFileSync(LOG, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));
  const failures = [];
  const toolTexts = requests.flatMap((r) => (r.messages || []).filter((m) => m.role === "tool").map((m) => textOf(m.content)));
  const hit = toolTexts.find((t) => t.includes("<multilint>") && t.includes("broken.sh"));
  if (!hit) failures.push(`no tool-role message with <multilint> and broken.sh (${toolTexts.length} tool messages in ${requests.length} requests)`);
  else if (hit.split("<multilint>").length - 1 !== 1) failures.push("<multilint> occurs more than once in the tool message");
  const dockerLog = opt("--docker-log");
  if (dockerLog) {
    const lines = fs.existsSync(dockerLog) ? fs.readFileSync(dockerLog, "utf8").split("\n").filter(Boolean) : [];
    if (lines.length !== 1) failures.push(`expected exactly 1 shim invocation, got ${lines.length}`);
    else if (!lines[0].includes(`source=${PROJECT},`)) failures.push(`mount source is not ${PROJECT}: ${lines[0]}`);
  }
  const system = opt("--expect-system");
  if (system && !requests.some((r) => (r.messages || []).some((m) => m.role === "system" && textOf(m.content).includes(system))))
    failures.push(`no system message contains: ${system}`);
  if (failures.length) {
    console.error("FAIL: " + failures.join("; "));
    process.exit(1);
  }
  console.log("PASS");
}

const [cmd, ...rest] = process.argv.slice(2);
if (cmd === "serve") serve();
else if (cmd === "check") check(rest);
else {
  console.error("usage: mock-provider.mjs serve | check [--docker-log FILE] [--expect-system TEXT]");
  process.exit(2);
}
