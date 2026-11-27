/** 手动触发：验签 → 记 pending（谁干谁写：发单是 Worker 干的）→ dispatch → 立即返回。 */
import { dispatchGha } from "../jobs/dispatch";
import type { Env } from "../types";

const JOBS = new Set(["generate", "update"]);

export async function handleTrigger(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const token =
    url.searchParams.get("token") ??
    request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ??
    "";
  if (!env.TRIGGER_TOKEN || token !== env.TRIGGER_TOKEN) {
    return new Response(JSON.stringify({ error: "unauthorized" }), {
      status: 401, headers: { "Content-Type": "application/json" },
    });
  }

  let body: { job?: string; args?: string } = {};
  try {
    body = await request.json();
  } catch {
    // 空 body = 默认 generate 全量
  }
  const job = body.job ?? "generate";
  const args = body.args ?? "";
  if (!JOBS.has(job)) {
    return new Response(JSON.stringify({ error: `unknown job: ${job}` }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  const jobId = `job_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;
  const started = Date.now();
  const dispatched = await dispatchGha(env, jobId, job, args);

  // 记账：发单这步是 Worker 干的，pending 由 Worker 写
  if (env.DB) {
    const now = Date.now();
    const jobs = `${env.TABLE_PREFIX ?? ""}jobs`;
    await env.DB.prepare(
      `INSERT INTO ${jobs} (job_id, trigger_type, status, total, done, failed, created_at, updated_at)
       VALUES (?, ?, 'pending', 0, 0, 0, ?, ?)
       ON CONFLICT(job_id) DO NOTHING`
    ).bind(jobId, job, now, now).run().catch(() => undefined);
  }

  return new Response(
    JSON.stringify({ ok: true, jobId, job, dispatched, elapsed_ms: Date.now() - started }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  );
}
