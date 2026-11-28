"""四组配置段（每组一个 JSON 环境变量，缺失=空字典，不抛异常）。

  FEISHU_CONFIG: app_id / app_secret / app_token / tables{resource,template,document} / output_folder
  CF_CONFIG:     account_id / d1_database_id / api_token / table_prefix
  GH_CONFIG:     repo / pat
  WORKER_CONFIG: trigger_token

GHA 只用 FEISHU + CF；Worker 用 WORKER + GH + CF(table_prefix)。
"""
import json
import os

GROUPS = ("FEISHU_CONFIG", "CF_CONFIG", "GH_CONFIG", "WORKER_CONFIG")


def _parse(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} 不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"{name} 必须是 JSON 对象")
    return obj


C = {name: _parse(name) for name in GROUPS}


def get(group, key, default=""):
    return C[group].get(key, default)
