// Optional OpenCode wiring for the shared Python lifecycle entrypoint.
// Prompt-level activation is a pending lane. The plugin surface inspected on
// 2026-09-26 exposes session and compaction hooks, not a prompt hook that can
// be used without per-token spam. Do not bind message.updated.
import { spawn } from "node:child_process";

function runHook(payload) {
  const command = process.platform === "win32" ? "py" : "python3";
  const args =
    process.platform === "win32"
      ? ["-3", "-m", "agent_mem_bridge", "lifecycle-hook"]
      : ["-m", "agent_mem_bridge", "lifecycle-hook"];
  return new Promise((resolve) => {
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (chunk) => {
      out += chunk;
    });
    child.on("error", () => resolve({}));
    child.on("close", () => {
      try {
        resolve(JSON.parse(out));
      } catch {
        resolve({});
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export const AmbLifecycle = async ({ directory, worktree }) => {
  const cwd = worktree || directory || ".";
  return {
    event: async ({ event }) => {
      if (!event || event.type !== "session.created") {
        return;
      }
      const properties = event.properties || {};
      await runHook({
        host: "opencode",
        hook_event_name: "SessionStart",
        source: "startup",
        cwd,
        session_id: properties.sessionID || properties.sessionId || "",
      });
    },
    "experimental.session.compacting": async (input, output) => {
      const result = await runHook({
        host: "opencode",
        hook_event_name: "PreCompact",
        trigger: "auto",
        cwd,
        session_id: (input && (input.sessionID || input.sessionId)) || "",
      });
      const text = result && result.hookSpecificOutput && result.hookSpecificOutput.additionalContext;
      if (typeof text === "string" && text && output && Array.isArray(output.context)) {
        output.context.push(text);
      }
    },
  };
};
