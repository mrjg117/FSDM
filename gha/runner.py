#!/usr/bin/env python3
"""通用执行器：白名单路由 job，任务状态直写 D1（谁干谁写）。

- GHA 唯一入口：workflow 只调本脚本，job 名走白名单，加新活只改 JOBS 表。
- job_start 写 running（已有 Worker 写的 pending 行则更新，本地直跑则插入）。
- try/finally 保证 job_finish：成功 done+计数，异常 failed+错误信息，绝不留死单。

用法（workflow 里）：
  python gha/runner.py
环境变量：FSDM_JOB（generate/update/...）、JOB_ID、FSDM_ARGS（如 "--limit 2"）
本地直跑：FSDM_JOB=generate python gha/runner.py   # JOB_ID 空则不记 jobs
"""
import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import d1

JOBS = {
    "generate": "generate",
    "update": "update",
}


def main():
    job = os.environ.get("FSDM_JOB", "generate")
    job_id = os.environ.get("JOB_ID", "")
    args = os.environ.get("FSDM_ARGS", "")

    if job not in JOBS:
        raise SystemExit(f"未知 job: {job}（白名单: {', '.join(JOBS)}）")

    if job_id:
        d1.ensure_schema(verbose=False)
        d1.job_start(job_id, job)

    status, total, done, failed, error = "done", 0, 0, 0, None
    try:
        mod = importlib.import_module(JOBS[job])
        if args:
            sys.argv += args.split()
        result = mod.main() or {}
        new = int(result.get("new", 0))
        updated = int(result.get("updated", 0))
        blocked = int(result.get("blocked", 0))
        failed = int(result.get("failed", 0))
        done = new + updated + blocked
        total = done + int(result.get("skipped", 0)) + failed
    except SystemExit as exc:
        status, error = "failed", str(exc)
        raise
    except Exception as exc:  # noqa: BLE001
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if job_id:
            try:
                d1.job_finish(job_id, status, total, done, failed, error)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] job_finish 写失败: {exc}")

    print(f"\n[job] {job} {status}：total={total} done={done} failed={failed}")


if __name__ == "__main__":
    main()
