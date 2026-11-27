/** Worker 环境变量：门卫 + 查询台。D1 可读写（发单记 pending / 查询只读）。 */
import type { D1Database } from "@cloudflare/workers-types";

export interface Env {
  DB: D1Database;
  TRIGGER_TOKEN: string;
  GITHUB_PAT: string;
  TABLE_PREFIX?: string;   // 表名前缀 = 项目名（共用库隔离），如 fsdm_
  ENVIRONMENT: string;
}
