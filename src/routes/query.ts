/** 查询台：只读 D1 即时查询。GET /query?token=X&job_id= / doc_key= / subject=&product= */
import { loadConfig } from "../config";
import type { Env } from "../types";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json" },
  });
}

export async function handleQuery(request: Request, env: Env): Promise<Response> {
  const cfg = loadConfig(env);
  const url = new URL(request.url);
  const token = url.searchParams.get("token")
    ?? request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "")
    ?? "";
  if (!cfg.WORKER_CONFIG.trigger_token || token !== cfg.WORKER_CONFIG.trigger_token) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!env.DB) return json({ error: "no d1 binding" }, 500);

  const p = cfg.CF_CONFIG.table_prefix;
  const jobId = url.searchParams.get("job_id");
  const docKey = url.searchParams.get("doc_key");
  const subject = url.searchParams.get("subject");
  const product = url.searchParams.get("product");
  const out: Record<string, unknown> = {};

  // 任务状态
  if (jobId) {
    out.job = (await env.DB.prepare(
      `SELECT * FROM ${p}jobs WHERE job_id = ?`
    ).bind(jobId).first()) ?? null;
  }

  // 单份文档：注册行 + 台账
  if (docKey) {
    const row = await env.DB.prepare(
      `SELECT * FROM ${p}documents WHERE doc_key = ?`
    ).bind(docKey).first();
    out.document = row ?? null;
    if (row) {
      const { results } = await env.DB.prepare(
        `SELECT var_name, blocks_json, images_json, src_token, dst_token, status, updated_at
         FROM ${p}ledger WHERE doc_token = ?`
      ).bind(row.doc_token as string).all();
      out.ledger = results ?? [];
    }
  }

  // 按主体/产品查台账（等值走索引）
  if (subject && product) {
    const { results } = await env.DB.prepare(
      `SELECT doc_token, var_name, src_token, dst_token, status, updated_at
       FROM ${p}ledger WHERE subject = ? AND product = ?`
    ).bind(subject, product).all();
    out.ledger = results ?? [];
  } else if (subject || product) {
    return json({ error: "subject 和 product 需成对给（等值索引）" }, 400);
  }

  if (!jobId && !docKey && !subject) {
    return json({
      error: "参数：job_id / doc_key / subject+product 任选其一",
      examples: [
        "/query?token=X&job_id=job_...",
        "/query?token=X&doc_key=主体|产品|平台|模板ID",
        "/query?token=X&subject=某主体&product=某产品",
      ],
    }, 400);
  }
  return json(out);
}
