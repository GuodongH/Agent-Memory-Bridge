// Optional OpenCode wiring for the shared Python lifecycle entrypoint.
// Prompt-level activation is a pending lane. The plugin surface exposes session
// and compaction hooks, not a prompt hook that can be used without per-token
// spam. Do not bind message.updated.
import { spawn } from "node:child_process";

const HOOK_TIMEOUT_MS = 10000;

function hookCommand() {
  // Test-only command override. Not a user setting.
  const override = process.env.AMB_LIFECYCLE_HOOK_COMMAND;
  if (override) {
    try {
      const parsed = JSON.parse(override);
      if (
        Array.isArray(parsed) &&
        parsed.length > 0 &&
        parsed.every((part) => typeof part === "string" && part.length > 0)
      ) {
        return { command: parsed[0], args: parsed.slice(1) };
      }
    } catch {
      // Invalid JSON falls back to the production command.
    }
  }
  if (process.platform === "win32") {
    return { command: "py", args: ["-3", "-m", "agent_mem_bridge", "lifecycle-hook"] };
  }
  return { command: "python3", args: ["-m", "agent_mem_bridge", "lifecycle-hook"] };
}

function stopChild(child) {
  if (!child || child.pid == null) {
    return;
  }
  if (process.platform === "win32") {
    spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
    return;
  }
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch {
    child.kill("SIGKILL");
  }
}

function runHook(payload) {
  const { command, args } = hookCommand();
  return new Promise((resolve) => {
    let settled = false;
    const child = spawn(command, args, {
      stdio: ["pipe", "pipe", "pipe"],
      detached: process.platform !== "win32",
      windowsHide: true,
    });
    let out = "";
    const finish = (value) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      stopChild(child);
      finish({});
    }, HOOK_TIMEOUT_MS);
    child.stdout.on("data", (chunk) => {
      if (out.length < 100000) {
        out += chunk;
      }
    });
    child.stderr.on("data", () => {
      // Drain stderr so a noisy child cannot block on a full pipe.
    });
    child.on("error", () => finish({}));
    child.on("close", () => {
      try {
        finish(JSON.parse(out));
      } catch {
        finish({});
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

function sessionIdFromCreated(event) {
  const properties = (event && event.properties) || {};
  const info = properties.info || {};
  return info.id || properties.sessionID || properties.sessionId || "";
}

export const AmbLifecycle = async ({ directory, worktree }) => {
  const cwd = worktree || directory || ".";
  return {
    event: async ({ event }) => {
      if (!event || event.type !== "session.created") {
        return;
      }
      await runHook({
        host: "opencode",
        hook_event_name: "SessionStart",
        source: "startup",
        cwd,
        session_id: sessionIdFromCreated(event),
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
