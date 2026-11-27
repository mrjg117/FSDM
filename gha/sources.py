"""源快照：按源记"上次填入值"，检测比对专用。

- 一行 = 主体|产品|变量 → kind(image/text) + src_token/value_hash。
- 同一张图被 N 份文档引用只此一行，比对判变化只读这张表（几百行）。
- 判出变化后，反推受影响文档走 ledger 的 (subject,product,var_name) 索引。
- 攒批回写：enqueue 攒内存，flush() 一次 batch 提交。
"""
import hashlib
import time

import d1

_pending = []
BATCH_SIZE = 200

# 内存缓存：{(subject, product, var): {"kind","src_token","value_hash"}}
MAP = {}


def load_all():
    """启动时一次全表读入内存（行数 = 唯一源数，几百行）。"""
    rows = d1.query(
        "SELECT subject, product, var_name, kind, src_token, value_hash"
        " FROM __PREFIX__sources"
    )
    MAP.clear()
    for r in rows:
        MAP[(r["subject"], r["product"], r["var_name"])] = {
            "kind": r["kind"],
            "src_token": r["src_token"] or "",
            "value_hash": r["value_hash"] or "",
        }
    return len(MAP)


def changed(subject, product, var, kind, src_token, value_hash):
    """新值与快照比对：True = 变了（或首次登记，由调用方决定首登怎么处理）。"""
    old = MAP.get((subject, product, var))
    if old is None:
        return None  # 无旧值参照：无法判定
    return not (
        old["kind"] == kind
        and old["src_token"] == src_token
        and old["value_hash"] == value_hash
    )


def enqueue(subject, product, var, kind, src_token, value_hash):
    """登记/更新一个源的快照（攒批）。"""
    MAP[(subject, product, var)] = {
        "kind": kind, "src_token": src_token, "value_hash": value_hash,
    }
    now = int(time.time())
    _pending.append((
        "INSERT INTO __PREFIX__sources"
        " (subject, product, var_name, kind, src_token, value_hash, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(subject, product, var_name) DO UPDATE SET"
        " kind=excluded.kind, src_token=excluded.src_token,"
        " value_hash=excluded.value_hash, updated_at=excluded.updated_at",
        [subject, product, var, kind, src_token, value_hash, now],
    ))


def flush():
    n = len(_pending)
    for i in range(0, n, BATCH_SIZE):
        d1.batch(_pending[i:i + BATCH_SIZE])
    _pending.clear()
    return n
