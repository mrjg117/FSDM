/**
 * FSDM Worker 入口（门卫 + 查询台）
 *   GET  /health   健康检查
 *   POST /trigger  手动触发（token 校验）→ 记 pending → repository_dispatch
 *   GET  /query    查询台（token 校验）：jobs / documents / ledger 只读
 * 谁干谁写：发单记 pending 是 Worker 的活；GHA 起跑/干完直写 D1（REST）。
 */
import { handleHealth } from "./routes/health";
import { handleTrigger } from "./routes/trigger";
import { handleQuery } from "./routes/query";
import type { Env } from "./types";

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === "/health" && request.method === "GET") return handleHealth(env);
      if (path === "/trigger" && request.method === "POST") return handleTrigger(request, env);
      if (path === "/query" && request.method === "GET") return handleQuery(request, env);
      return new Response(JSON.stringify({ error: "not found", path }), {
        status: 404, headers: { "Content-Type": "application/json" },
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`[fetch] ${path}:`, msg);
      return new Response(JSON.stringify({ error: msg }), {
        status: 500, headers: { "Content-Type": "application/json" },
      });
    }
  },
};
