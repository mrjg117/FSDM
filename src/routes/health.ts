import { loadConfig } from "../config";
import type { Env } from "../types";

export async function handleHealth(env: Env): Promise<Response> {
  const cfg = loadConfig(env);
  const checks: Record<string, string> = {
    DB: env.DB ? "ok" : "missing",
    WORKER_CONFIG: cfg.WORKER_CONFIG.trigger_token ? "ok" : "missing",
    GH_CONFIG: cfg.GH_CONFIG.repo && cfg.GH_CONFIG.pat ? "ok" : "missing",
    CF_CONFIG: cfg.CF_CONFIG.table_prefix ? "ok" : "missing",
  };
  const allOk = Object.values(checks).every((v) => v === "ok");
  return new Response(
    JSON.stringify({ status: allOk ? "ok" : "degraded", checks, ts: Date.now() }),
    { status: allOk ? 200 : 503, headers: { "Content-Type": "application/json" } }
  );
}
