/** 触发 GHA：传 jobId + job 名 + 附加参数，任务数据和图片由 GHA 自己读飞书。 */
import type { Env } from "../types";
import { loadConfig } from "../config";

export interface DispatchResult {
  ok: true;
  jobId: string;
  status: number;
}

export async function dispatchGha(
  env: Env, jobId: string, job: string, args: string,
): Promise<DispatchResult> {
  const { repo, pat } = loadConfig(env).GH_CONFIG;
  if (!repo || !pat) throw new Error("GH_CONFIG 缺少 repo / pat");
  const resp = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${pat}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "fsdm-docgen-worker",
    },
    body: JSON.stringify({
      event_type: "fsdm-job",
      client_payload: { jobId, job, args },
    }),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`GitHub dispatch ${resp.status}: ${detail.slice(0, 300)}`);
  }
  return { ok: true, jobId, status: resp.status };
}
