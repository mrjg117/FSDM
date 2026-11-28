/** Worker 环境变量：4 组 JSON 配置段（见 config.ts）。DB 为 Dashboard 绑定的 D1。 */
import type { D1Database } from "@cloudflare/workers-types";

export interface Env {
  DB: D1Database;
  FEISHU_CONFIG?: string;
  CF_CONFIG?: string;
  GH_CONFIG?: string;
  WORKER_CONFIG?: string;
}
