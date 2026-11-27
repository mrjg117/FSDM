import type { Env } from "../types";

export async function handleHealth(env: Env): Promise<Response> {
  const checks: Record<string, string> = {
    DB: env.DB ? "ok" : "missing",
    TRIGGER_TOKEN: env.TRIGGER_TOKEN ? "set" : "missing",
    GITHUB_PAT: env.GITHUB_PAT ? "set" : "missing",
  };
  const allOk = Object.values(checks).every((v) => v === "ok" || v === "set");
  return new Response(
    JSON.stringify({ status: allOk ? "ok" : "degraded", checks, ts: Date.now() }),
    { status: allOk ? 200 : 503, headers: { "Content-Type": "application/json" } }
  );
}
