#!/usr/bin/env python3
"""Cloudflare D1 直通 + 自动建表。

- 走 Cloudflare REST API 直接执行 SQL，不经 Worker binding。
- 表名前缀 TABLE_PREFIX：共库多项目时每个项目一套表（如 fsdm_ledger），
  fork 项目单独库则留空。schema.sql 里的 __PREFIX__ 在执行时替换。
- 自动建表：ensure_schema() 读 db/schema.sql，替换前缀后 batch 提交（全幂等）。
  调用方在入口处调一次即可，不必再手动 db:init。

配置段 CF_CONFIG（JSON）：account_id / d1_database_id / api_token / table_prefix

官方接口：
  POST https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query
  Authorization: Bearer {token}
  请求体 {"sql":...,"params":[...]} 或 {"batch":[{"sql":...,"params":[...]},...]}
"""
import os
import re
import json
import time
import urllib.request
import urllib.error

import config

ACCOUNT_ID = config.get("CF_CONFIG", "account_id")
DATABASE_ID = config.get("CF_CONFIG", "d1_database_id")
API_TOKEN = config.get("CF_CONFIG", "api_token")
TABLE_PREFIX = config.get("CF_CONFIG", "table_prefix")

BASE = "https://api.cloudflare.com/client/v4/accounts"

# 用量：响应 meta 官方自带 rows_read/rows_written，直接累加，零额外请求。
USAGE = {"calls": 0, "rows_read": 0, "rows_written": 0}


def usage():
    return dict(USAGE)


def reset_usage():
    USAGE.update(calls=0, rows_read=0, rows_written=0)


class D1Error(RuntimeError):
    pass


def _endpoint():
    if not (ACCOUNT_ID and DATABASE_ID and API_TOKEN):
        raise D1Error("缺少 D1 凭证：CF_CONFIG 需 account_id / d1_database_id / api_token")
    return f"{BASE}/{ACCOUNT_ID}/d1/database/{DATABASE_ID}/query"


def _apply_prefix(sql):
    return sql.replace("__PREFIX__", TABLE_PREFIX)


def _call(payload, retries=3):
    url = _endpoint()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    })
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            last = D1Error(f"D1 HTTP {exc.code}: {detail[:500]}")
            if exc.code not in (429, 500, 502, 503, 504):
                raise last
        except (urllib.error.URLError, TimeoutError) as exc:
            last = D1Error(f"D1 网络错误: {exc}")
        if attempt + 1 < retries:
            time.sleep(0.8 * (2 ** attempt))
    else:
        raise last

    if not body.get("success"):
        errs = "; ".join(
            (e.get("message") or str(e)) for e in body.get("errors", [])
        )
        raise D1Error(f"D1 API 失败: {errs}")

    result = body.get("result") or []
    USAGE["calls"] += 1
    for item in result:
        meta = item.get("meta") or {} if isinstance(item, dict) else {}
        USAGE["rows_read"] += int(meta.get("rows_read") or 0)
        USAGE["rows_written"] += int(meta.get("rows_written") or 0)
        if isinstance(item, dict) and item.get("success") is False:
            raise D1Error(f"D1 语句失败: {item}")
    return result


def query(sql, params=None, retries=3):
    """执行单条 SQL，返回行列表。params 统一转字符串（官方要求 string 数组）。"""
    payload = {"sql": _apply_prefix(sql)}
    if params is not None:
        payload["params"] = [str(p) for p in params]
    result = _call(payload, retries=retries)
    return (result[0].get("results") or []) if result else []


def batch(statements, retries=3):
    """一次请求执行多条语句。statements: [(sql, params), ...]。"""
    payload = {"batch": [
        {"sql": _apply_prefix(sql), "params": [str(p) for p in (params or [])]}
        for sql, params in statements
    ]}
    return [item.get("results") or [] for item in _call(payload, retries=retries)]


def ensure_schema(schema_path=None, verbose=True):
    """读 schema.sql、替换前缀、去除注释后 batch 提交。全 IF NOT EXISTS 幂等。

    返回执行的语句数。调用方在入口处调一次，D1 建表从此自动化。
    """
    if schema_path is None:
        schema_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "db", "schema.sql"
        )
    with open(schema_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    # 去整行注释，再按分号拆语句
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    for i in range(0, len(stmts), 20):
        chunk = [(s, []) for s in stmts[i:i + 20]]
        batch(chunk)
    if verbose:
        print(f"[D1] schema 就绪：{len(stmts)} 条 DDL，前缀={TABLE_PREFIX or '(空)'}")
    return len(stmts)


# ---------------- jobs（任务状态）：谁干谁写——
# Worker 发单写 pending；GHA 起跑 job_start 写 running；干完 job_finish 写 done/failed。
def job_start(job_id, job_type="generate"):
    """任务起跑：已有 Worker 写的 pending 行则置 running，没有（本地直跑）则插入 running。"""
    now = int(time.time())
    batch([(
        "INSERT INTO __PREFIX__jobs (job_id, trigger_type, status, created_at, updated_at)"
        " VALUES (?, ?, 'running', ?, ?)"
        " ON CONFLICT(job_id) DO UPDATE SET status='running', trigger_type=?, updated_at=?",
        [job_id, job_type, now, now, job_type, now],
    )])


def job_finish(job_id, status, total=0, done=0, failed=0, error=None):
    """任务结束：写结果计数。error 截 500 字符。"""
    now = int(time.time())
    batch([(
        "UPDATE __PREFIX__jobs SET status=?, total=?, done=?, failed=?, error=?, updated_at=?"
        " WHERE job_id=?",
        [status, int(total), int(done), int(failed), (error or "")[:500], now, job_id],
    )])
