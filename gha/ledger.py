"""元素级台账 + 文档注册表：全部存 D1，飞书表不存台账。

台账：一行 = 一个文档的一个变量，主键 (doc_token, var_name)。
      生成抹掉标记后文档里没有变量名痕迹，这是唯一的变量-文档-块关联。
      带特征字段（src/dst token、宽高、模板块、内容hash、替换时间），
      支持定向替换、素材变化判断、回滚审计、模板同步。
注册表：一行 = 一份文档，doc_key 唯一，用于去重（重复触发复用不新建）。

资源节省：
- enqueue/enqueue_document 攒内存，flush() 一次 batch 提交。
- 查询先等值走索引；模糊片段等值查不到才回退 LIKE（全表扫描）。
- 不建 doc_token 单列索引（主键已覆盖前缀）。
"""
import json
import time

import generate as G
import d1

LEDGER_FIELDS = [
    "doc_token", "subject", "product", "var_name",
    "blocks_json", "images_json", "template_block_id", "content_hash",
    "src_token", "dst_token", "width", "height", "replaced_at", "status", "updated_at",
]
SELECT_FIELDS = ", ".join(LEDGER_FIELDS)

_pending = []       # ledger upsert 语句
_pending_docs = []  # documents upsert 语句
BATCH_SIZE = 200


def collect(items, vars_all):
    """扫块树，返回 {变量名: {"blocks":[...], "images":[...]}}。
    与 generate 用同一套 prepare，识别口径一致。"""
    ordered, byid, marks, segs, _ = G.prepare(items)
    led = {}

    def put(name, key, bid):
        d = led.setdefault(name, {"blocks": [], "images": []})
        if bid not in d[key]:
            d[key].append(bid)

    for start, end in segs:
        name = start["name"]
        put(name, "blocks", start["bid"])
        put(name, "blocks", end["bid"])
        for bid in ordered[start["pos"] + 1:end["pos"]]:
            if byid.get(bid, {}).get("block_type") == 27:
                put(name, "images", bid)
    for m in marks:
        if m.get("role") == "value":
            put(m["name"], "blocks", m["bid"])
    return led


def attach_features(led, items, by_block, tpl_blocks):
    """把换图结果与模板映射写进台账条目。

    by_block: {image_block_id: [(src_token, dst_token, w, h), ...]}
    tpl_blocks: {var_name: template_block_id}
    """
    byid = {b["block_id"]: b for b in items}
    now = int(time.time())
    for name, entry in led.items():
        entry["template_block_id"] = tpl_blocks.get(name, "")
        bid = tpl_blocks.get(name)
        if bid and bid in byid:
            entry["content_hash"] = G.content_hash(byid[bid])
        imgs = []
        for ib in entry.get("images", []):
            imgs.extend(by_block.get(ib, []))
        if imgs:
            entry["src_token"] = ",".join(x[0] for x in imgs)
            entry["dst_token"] = ",".join(x[1] for x in imgs)
            w, h = imgs[0][2], imgs[0][3]
            entry["width"], entry["height"] = w, h
            entry["replaced_at"] = now
        entry["status"] = "active"


def _statements(doc_token, subject, product, ledger, now=None):
    now = int(time.time()) if now is None else now
    sql = (
        "INSERT INTO __PREFIX__ledger "
        "(doc_token, subject, product, var_name, blocks_json, images_json, "
        "template_block_id, content_hash, src_token, dst_token, width, height, "
        "replaced_at, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(doc_token, var_name) DO UPDATE SET "
        "subject=excluded.subject, product=excluded.product, "
        "blocks_json=excluded.blocks_json, images_json=excluded.images_json, "
        "template_block_id=excluded.template_block_id, content_hash=excluded.content_hash, "
        "src_token=excluded.src_token, dst_token=excluded.dst_token, "
        "width=excluded.width, height=excluded.height, "
        "replaced_at=excluded.replaced_at, status=excluded.status, "
        "updated_at=excluded.updated_at"
    )
    out = []
    for name, e in ledger.items():
        out.append((sql, [
            doc_token, subject, product, name,
            json.dumps(e.get("blocks", []), ensure_ascii=False),
            json.dumps(e.get("images", []), ensure_ascii=False),
            e.get("template_block_id", ""), e.get("content_hash", ""),
            e.get("src_token", ""), e.get("dst_token", ""),
            int(e.get("width", 0) or 0), int(e.get("height", 0) or 0),
            e.get("replaced_at"), e.get("status", "active"), now,
        ]))
    return out


def enqueue(doc_token, subject, product, ledger):
    if ledger:
        _pending.extend(_statements(doc_token, subject, product, ledger))


def enqueue_document(doc_key, doc_token, subject, product, platform, tpl_id, now=None):
    now = int(time.time()) if now is None else now
    sql = (
        "INSERT INTO __PREFIX__documents "
        "(doc_key, doc_token, subject, product, platform, template_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(doc_key) DO UPDATE SET "
        "doc_token=excluded.doc_token, updated_at=excluded.updated_at"
    )
    _pending_docs.append((sql, [doc_key, doc_token, subject, product, platform, tpl_id, now, now]))


def flush():
    """一次提交台账与注册表，返回 ledger 写入行数。"""
    all_stmts = _pending + _pending_docs
    n_ledger = len(_pending)
    _pending.clear()
    _pending_docs.clear()
    for i in range(0, len(all_stmts), BATCH_SIZE):
        d1.batch(all_stmts[i:i + BATCH_SIZE])
    return n_ledger


def _select(subject, product, var, op):
    where, params = [], []
    if subject:
        where.append(f"subject {op} ?")
        params.append(subject if op == "=" else f"%{subject}%")
    if product:
        where.append(f"product {op} ?")
        params.append(product if op == "=" else f"%{product}%")
    if var:
        where.append("var_name = ?")
        params.append(var)
    sql = f"SELECT {SELECT_FIELDS} FROM __PREFIX__ledger"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return d1.query(sql, params)


def _group(rows):
    grouped = {}
    for r in rows:
        doc = r["doc_token"]
        if doc not in grouped:
            grouped[doc] = {"subject": r.get("subject") or "", "product": r.get("product") or "", "vars": {}}
        grouped[doc]["vars"][r["var_name"]] = {
            "blocks": _loads(r.get("blocks_json")),
            "images": _loads(r.get("images_json")),
            "src_token": r.get("src_token") or "",
            "dst_token": r.get("dst_token") or "",
            "content_hash": r.get("content_hash") or "",
            "template_block_id": r.get("template_block_id") or "",
            "width": r.get("width") or 0,
            "height": r.get("height") or 0,
        }
    return [
        (doc, g["subject"], g["product"], g["vars"])
        for doc, g in grouped.items()
    ]


def lookup(subject=None, product=None, var=None, exact=False, fuzzy_fallback=True):
    """查台账，返回 [(doc_token, subject, product, {var: entry}), ...]。
    默认先等值（走索引）；模糊片段等值查不到才回退 LIKE。exact=True 不回退。"""
    rows = _select(subject, product, var, "=")
    if not rows and not exact and fuzzy_fallback and (subject or product):
        rows = _select(subject, product, var, "LIKE")
    return _group(rows)


def lookup_doc(doc_token):
    """按文档查台账（主键前缀等值），返回 {var: entry}。更新链路反推块用。"""
    rows = d1.query(
        f"SELECT {SELECT_FIELDS} FROM __PREFIX__ledger WHERE doc_token = ?",
        [doc_token],
    )
    grouped = _group(rows)
    return grouped[0][3] if grouped else {}


def mark_replaced(doc_token, var_name, dst_token, width=0, height=0):
    """换图/改文本后即时回写单个变量的 dst_token 与替换时间（定向修改用）。"""
    now = int(time.time())
    d1.query(
        "UPDATE __PREFIX__ledger SET dst_token=?, width=?, height=?, replaced_at=?, updated_at=? "
        "WHERE doc_token=? AND var_name=?",
        [dst_token, int(width), int(height), now, now, doc_token, var_name],
    )


def _loads(s):
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []
