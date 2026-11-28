/** 四组配置段：每组一个 JSON 环境变量，解析成强类型对象。 */
import type { Env } from "./types";

export interface Config {
  FEISHU_CONFIG: {
    app_id: string; app_secret: string; app_token: string;
    tables: { resource: string; template: string; document: string };
    output_folder: string;
  };
  CF_CONFIG: {
    account_id: string; d1_database_id: string; api_token: string; table_prefix: string;
  };
  GH_CONFIG: { repo: string; pat: string };
  WORKER_CONFIG: { trigger_token: string };
}

const EMPTY: Config = {
  FEISHU_CONFIG: { app_id: "", app_secret: "", app_token: "", tables: { resource: "", template: "", document: "" }, output_folder: "" },
  CF_CONFIG: { account_id: "", d1_database_id: "", api_token: "", table_prefix: "" },
  GH_CONFIG: { repo: "", pat: "" },
  WORKER_CONFIG: { trigger_token: "" },
};

export function loadConfig(env: Env): Config {
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(EMPTY) as (keyof Config)[]) {
    const raw = (env as unknown as Record<string, string | undefined>)[key];
    if (!raw) { out[key] = EMPTY[key]; continue; }
    try {
      out[key] = { ...(EMPTY[key] as object), ...JSON.parse(raw) };
    } catch {
      throw new Error(`${key} 不是合法 JSON`);
    }
  }
  return out as unknown as Config;
}
