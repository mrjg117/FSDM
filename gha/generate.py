#!/usr/bin/env python3
"""FSDM 主执行流程（GHA / 本地通用）。

流程（顺序固定）：
  A. 读模板表（1 次）：名称/平台/文档ID/启用主体/启用产品
     交叉自算：每模板 启用主体×启用产品 = 它的全部 (主体,产品) 对，不读资源表的模板列
  B. 读资源表（1 次）拿变量；交叉对里缺的资源行 batch_create 补行（只填主体/产品，
     Lookup 联动后下轮 run 自动带上），补的行本轮跳过
  C. 按产品分组（组内按模板）；母版延迟构建——只有需要新建/重建时才 copy+裁剪
  D. 每份文档：
     - D1 documents 查重：不存在 → 新建（copy 母版全套生成）
     - 已存在 → 按源检测更新（见下）；修不了 → 不动文档，计数报出来（blocked）。
  E. 收尾：飞书文档表一次 batch_create 回填；ledger/documents/sources 各攒批一次写 D1

按源检测更新（update_doc）：
  1. sources 表（按源记上次填入值，同图 N 文档引用只 1 行）vs 数据源新值 → 判出变化的变量
  2. 只对变化变量：按台账 (subject,product,var) 反推文档块，读一次文档块树验证
  3. 文本变量 → 按模板块形状重建整块文本覆盖（模板文本把 {{标记}} 换成新值）
     图变量 → 按 src_token 归一（下载 1 次/上传 1 次/replace 20 块一批）
  4. 修不了（值有无翻转/块丢失/台账缺失/模板形状缺失）→ 不动文档，返回原因计入 blocked；
     重建是手动触发的独立功能（原链接清空重写），不进自动流程
  无变化 → 零 API 跳过（判定全在内存 + sources 一次读）

标记规范（双花括号 v2，带名配对，标记可嵌在标题文字里）：
  {{X}} 内容 {{/X}}        图片/内容段：有值留内容抹标记；空则整段删
  {{?X}} 内容 {{/?X}}      正条件段：有值留内容抹标记；空则整段删
  {{?!X}} 内容 {{/?X}}     反条件段：空则留内容抹标记；有值整段删
  无结束标记的 {{X}} = 纯文本占位符，填该行真值（不参与配对）
  严格同名配对，错配直接报错，不做旧写法兼容。

用法：
  python gha/generate.py            # 全量
  python gha/generate.py --limit 2  # 只跑前 2 个产品（验证）
"""
import os
import re
import sys
import json
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu as fs
import ledger as L
import d1
import sources as S
import cache  # noqa: F401  （确保缓存目录常量加载）

APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "EohXbZt1waUfsZsxLigcfAssnyf")
TABLE_RESOURCE = os.environ.get("FEISHU_TABLE_RESOURCE", "tblXiHi2IIeS8R41")
TABLE_TEMPLATE = os.environ.get("FEISHU_TABLE_TEMPLATE", "tblUYfX9KFkYyE8l")
TABLE_DOCUMENT = os.environ.get("FEISHU_TABLE_DOCUMENT", "tblzwpLmyThbtJaQ")
OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER_TOKEN", "ZE4wfiDFdlVsyYdAetOck2VDnog")

# 资源表变量字段（应用模板列已废弃：模板归属由模板表启用关联决定）
RESOURCE_FIELDS = [
    "主体", "产品", "资质检查",
    "经营执照", "经营资质", "商标1", "商标证1", "商标1授权",
    "商标2", "商标证2", "商标2授权", "厂家", "厂家执照", "厂家资质",
    "经销授权", "经销二授", "广审件", "广审号", "注册证", "注册号",
    "说明书", "产品图", "产品链接",
]
TEMPLATE_FIELDS = ["名称", "平台", "文档ID", "启用主体", "启用产品"]

# 双花括号标记
VAR_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")
TEXT_KEYS = ("text", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6")


# ---------------------------------------------------------------- 字段工具
def field_text(f):
    if isinstance(f, str):
        return f
    if isinstance(f, list):
        return "".join(str(e.get("text", "")) for e in f if isinstance(e, dict) and e.get("text"))
    return ""


def attachments(f):
    if isinstance(f, list):
        return [e for e in f if isinstance(e, dict) and e.get("attachmentToken")]
    return []


def has_value(v):
    if v is None:
        return False
    if isinstance(v, list):
        return bool(attachments(v)) or len(v) > 0
    if isinstance(v, str):
        return v.strip() != ""
    return True


def value_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


EMPTY_HASH = value_hash("")


def source_of(fields, var):
    """数据源某变量的当前源特征：(kind, src_token, value_hash)。"""
    v = fields.get(var)
    atts = attachments(v)
    if atts:
        return ("image", ",".join(a["attachmentToken"] for a in atts), "")
    return ("text", "", value_hash(field_text(v)))


def text_key(b):
    for k in TEXT_KEYS:
        if k in b:
            return k
    return None


def text_of(b):
    k = text_key(b)
    if not k:
        return ""
    return "".join(e.get("text_run", {}).get("content", "") for e in b[k].get("elements", []))


def content_hash(b):
    return hashlib.sha256(text_of(b).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 共用图判定
def product_var_attachments(subset):
    """某产品全部行：var -> [每个非空行的附件序列元组, ...]"""
    m = {}
    for f in subset:
        for k, v in f.items():
            atts = attachments(v)
            if atts:
                m.setdefault(k, []).append(tuple(
                    (a["attachmentToken"], int(a.get("width", 0) or 0), int(a.get("height", 0) or 0))
                    for a in atts
                ))
    return m


def shared_vars_of(pvmap, subset):
    """全产品所有行都非空、附件序列完全一致的变量 = 共用图变量。"""
    return {
        k for k, seqs in pvmap.items()
        if len(seqs) == len(subset) and len(set(seqs)) == 1
    }


def product_cond_value(subset, name):
    """条件变量在产品维度能否定论：全有 True / 全空 False / 混合 None。"""
    n = sum(1 for f in subset if has_value(f.get(name)))
    if n == len(subset):
        return True
    if n == 0:
        return False
    return None


# ---------------------------------------------------------------- 标记解析
def classify(name):
    """('var'|'varend'|'cond+'|'cond-'|'condend', 变量名)"""
    if name.startswith("/?"):
        return ("condend", name[2:])
    if name.startswith("/"):
        return ("varend", name[1:])
    if name.startswith("?!"):
        return ("cond-", name[2:])
    if name.startswith("?"):
        return ("cond+", name[1:])
    return ("var", name)


def flatten(items):
    """按文档顺序展开 block_id（DFS），标记配对依赖这个顺序。"""
    byid = {b["block_id"]: b for b in items}
    ordered = []

    def walk(bid):
        b = byid.get(bid)
        if not b or bid in ordered:
            return
        ordered.append(bid)
        for cid in b.get("children", []):
            walk(cid)

    if items:
        walk(items[0]["block_id"])
    for b in items:
        if b["block_id"] not in ordered:
            ordered.append(b["block_id"])
    return ordered, byid


def scan_markers(ordered, byid):
    """扫所有文本块，返回按 (块序, 字符位) 排序的标记。"""
    marks = []
    for pos, bid in enumerate(ordered):
        if not text_key(byid.get(bid, {})):
            continue
        full = text_of(byid[bid])
        for m in VAR_TOKEN.finditer(full):
            kind, name = classify(m.group(1))
            marks.append({
                "pos": pos, "bid": bid, "start": m.start(), "end": m.end(),
                "kind": kind, "name": name,
            })
    marks.sort(key=lambda x: (x["pos"], x["start"]))
    return marks


def annotate_marks(marks, ordered, byid):
    """区分 {{X}} 是「图片/内容段开始」还是「纯文本占位符」。
    后面紧跟图片块 → 段开始（会有 {{/X}}）；否则 → 文本占位符（填值，不配对）。"""
    for m in marks:
        if m["kind"] == "var":
            nxt = ordered[m["pos"] + 1] if m["pos"] + 1 < len(ordered) else None
            m["role"] = "seg" if byid.get(nxt, {}).get("block_type") == 27 else "value"
        elif m["kind"] in ("cond+", "cond-"):
            m["role"] = "cond"
        else:
            m["role"] = "end"
    return marks


def pair_segments(marks):
    """栈配对：只有 seg/cond 入栈；结束标记须带名且与栈顶同名同类，错配报错。"""
    segs, stack = [], []
    for m in marks:
        if m["kind"] in ("var", "cond+", "cond-") and m.get("role") in ("seg", "cond"):
            stack.append(m)
            continue
        if m["kind"] not in ("varend", "condend"):
            continue
        if not stack:
            raise ValueError(f"标记错配：结束标记 {m['name']!r} 在第 {m['pos']+1} 块，前面无对应开始标记")
        s = stack[-1]
        kind_ok = ((m["kind"] == "varend" and s["kind"] == "var") or
                   (m["kind"] == "condend" and s["kind"] in ("cond+", "cond-")))
        name_ok = bool(m["name"]) and m["name"] == s["name"]
        if not (kind_ok and name_ok):
            raise ValueError(
                f"标记错配：第 {m['pos']+1} 块结束标记 {m['name']!r} "
                f"与第 {s['pos']+1} 块开始标记 {s['name']!r} 不配对"
            )
        stack.pop()
        segs.append((s, m))
    if stack:
        left = ", ".join(f"{s['name']!r}(第 {s['pos']+1} 块)" for s in stack)
        raise ValueError(f"标记未闭合：{left} 缺结束标记")
    return segs


def merge_edits(edits):
    """edits: [(s, e, repl), ...]；repl='' 表示删除。"""
    out = []
    for s, e, repl in sorted(edits):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e), out[-1][2] + repl)
        else:
            out.append((s, e, repl))
    return out


def rewrite_elements(b, edits):
    """按块内字符区间做局部改写（删标记/填值），保留其余元素与样式。"""
    k = text_key(b)
    if not k:
        return None
    edits = merge_edits(edits)
    new_els, off, changed = [], 0, False
    for el in b[k].get("elements", []):
        tr = el.get("text_run")
        if not isinstance(tr, dict) or not isinstance(tr.get("content"), str):
            new_els.append(el)
            continue
        content = tr["content"]
        n = len(content)
        cuts = []
        for s, e, repl in edits:
            a, bb = max(s, off), min(e, off + n)
            if a < bb:
                cuts.append((a - off, bb - off, repl))
        if not cuts:
            new_els.append(el)
            off += n
            continue
        keep, cur = [], 0
        for a, bb, repl in sorted(cuts):
            keep.append(content[cur:a])
            if repl:
                keep.append(repl)
            cur = bb
        keep.append(content[cur:])
        nc = "".join(keep)
        changed = True
        if nc:
            ne = dict(el)
            ne["text_run"] = dict(tr)
            ne["text_run"]["content"] = nc
            new_els.append(ne)
        off += n
    return new_els if changed else None


# ---------------------------------------------------------------- 扫树规划
def prepare(items):
    ordered, byid = flatten(items)
    marks = annotate_marks(scan_markers(ordered, byid), ordered, byid)
    segs = pair_segments(marks)
    marks_by_bid = {}

    def title_start_of(m):
        prev_end = 0
        for pm in marks_by_bid.get(m["bid"], []):
            if pm["end"] <= m["start"]:
                prev_end = max(prev_end, pm["end"])
        return prev_end

    for m in marks:
        marks_by_bid.setdefault(m["bid"], []).append(m)
    return ordered, byid, marks, segs, title_start_of


def del_ranges_of(items, del_bids):
    """整块删除 → 按父分组、合并连续区间（一次一个连续区间，从后往前删）。"""
    parents = {}
    for b in items:
        for cid in b.get("children", []):
            parents.setdefault(b["block_id"], []).append(cid)
    index_of = {}
    for pid, cids in parents.items():
        for i, cid in enumerate(cids):
            index_of[cid] = (pid, i)
    raw = {}
    for bid in del_bids:
        if bid in index_of:
            pid, i = index_of[bid]
            raw.setdefault(pid, []).append((i, i + 1))
    out = {}
    for pid, rs in raw.items():
        out[pid] = [(s, e) for s, e, _ in merge_edits([(s, e, "") for s, e in rs])]
    return out


def _delete_segment(between, start, end, sp, ep, byid, title_start_of, add_edit, del_bids):
    """整段删：边界块抹字符（含标题），中间块整块删。"""
    if sp == ep:
        add_edit(start["bid"], min(start["start"], end["start"]), max(start["end"], end["end"]))
    else:
        add_edit(start["bid"], title_start_of(start), len(text_of(byid.get(start["bid"], {}))))
        add_edit(end["bid"], 0, end["end"])
        del_bids.update(between)


def _to_text_requests(items, edits, del_bids, byid):
    """生成 batch_update 请求；抹完变空的块整块删，从请求里去掉。"""
    extra_del = set()
    for bid, ed in edits.items():
        rew = rewrite_elements(byid.get(bid, {}), ed)
        new_text = "".join(e.get("text_run", {}).get("content", "") for e in (rew or []))
        if new_text.strip() == "":
            extra_del.add(bid)
    del_bids.update(extra_del)
    reqs = []
    for bid, ed in edits.items():
        if bid in del_bids:
            continue
        ne = rewrite_elements(byid.get(bid, {}), ed)
        if ne is not None:
            reqs.append({"block_id": bid, "update_text_elements": {"elements": ne}})
    return reqs


def tpl_shape_of(tpl_items):
    """原模板块形状：{block_id: {text, marks}} —— 更新时整块文本重建用。

    模板文档块 ID 稳定（母版 copy 后块 ID 会变），台账 template_block_id
    存的就是原模板块 ID。返回 (shape, tpl_blocks={var: 开始块 id})。
    """
    ordered, byid, marks, segs, _ = prepare(tpl_items)
    shape = {}
    for m in marks:
        s = shape.setdefault(m["bid"], {"text": text_of(byid[m["bid"]]), "marks": []})
        s["marks"].append({
            "name": m["name"], "start": m["start"], "end": m["end"],
            "kind": m["kind"], "role": m.get("role"),
        })
    for s in shape.values():
        s["marks"].sort(key=lambda x: x["start"])
    tpl_blocks = _var_blocks(marks, segs)
    return shape, tpl_blocks


def _var_blocks(marks, segs):
    """{变量名: 开始块 id}：段变量取配对开始块；占位符（role=value）也登记，
    否则占位符变量没有 template_block_id，更新链路会误判重建。"""
    blocks = {start["name"]: start["bid"] for start, _ in segs}
    for m in marks:
        if m["kind"] == "var" and m.get("role") == "value" and m["name"] not in blocks:
            blocks[m["name"]] = m["bid"]
    return blocks


def rebuild_text(shape_entry, vars_all):
    """按模板块形状重建整块文本：{{占位符}} 换新值，段/条件标记抹掉。"""
    text = shape_entry["text"]
    for m in sorted(shape_entry["marks"], key=lambda x: -x["start"]):
        if m["kind"] == "var" and m.get("role") == "value":
            repl = field_text(vars_all.get(m["name"], ""))
        else:
            repl = ""
        text = text[:m["start"]] + repl + text[m["end"]:]
    return text


def plan_master(items, subset):
    """母版规划（产品×模板一次，在大量复制之前做）。

    - 共用图：换该产品真图（随 copy 继承）；差异图保留占位等每份换
    - 全空段/多余预置图/产品级条件段：删（不可批量删的非连续元素在此处理）
    - 文本标记一律保留；记录每个变量的 template_block_id + content_hash
    """
    ordered, byid, marks, segs, title_start_of = prepare(items)
    pvmap = product_var_attachments(subset)
    shared = shared_vars_of(pvmap, subset)
    edits, del_bids, image_tasks = {}, set(), []
    tpl_blocks = _var_blocks(marks, segs)

    def add_edit(bid, s, e, repl=""):
        if e > s:
            edits.setdefault(bid, []).append((s, e, repl))

    for start, end in segs:
        name, kind = start["name"], start["kind"]
        sp, ep = start["pos"], end["pos"]
        between = ordered[sp + 1:ep]
        imgs = [bid for bid in between if byid.get(bid, {}).get("block_type") == 27]

        if kind in ("cond+", "cond-"):
            decided = product_cond_value(subset, name)
            if decided is None:
                continue  # 主体间有差异 → 留每份处理
            keep = decided if kind == "cond+" else not decided
            if keep:
                add_edit(start["bid"], start["start"], start["end"])
                add_edit(end["bid"], end["start"], end["end"])
            else:
                _delete_segment(between, start, end, sp, ep, byid, title_start_of, add_edit, del_bids)
            continue

        seqs = pvmap.get(name, [])
        if not seqs:
            _delete_segment(between, start, end, sp, ep, byid, title_start_of, add_edit, del_bids)
            continue

        maxn = max(len(s) for s in seqs)
        for bid in imgs[maxn:]:
            del_bids.add(bid)
        if name in shared:
            first = seqs[0]
            for bid, (tok, w, h) in zip(imgs, first):
                image_tasks.append((bid, {"attachmentToken": tok, "width": w, "height": h}))
        # 文本标记一律不抹

    text_requests = _to_text_requests(items, edits, del_bids, byid)
    return (del_ranges_of(items, del_bids), text_requests, image_tasks, shared, tpl_blocks)


def plan_doc(items, vars_all, shared):
    """每份规划：换差异图 + 抹标记/填值 + 删空段（复用已继承的共用图）。"""
    ordered, byid, marks, segs, title_start_of = prepare(items)
    edits, del_bids, image_tasks = {}, set(), []

    def add_edit(bid, s, e, repl=""):
        if e > s:
            edits.setdefault(bid, []).append((s, e, repl))

    for m in marks:
        if m.get("role") == "value":
            add_edit(m["bid"], m["start"], m["end"], field_text(vars_all.get(m["name"], "")))

    for start, end in segs:
        name, kind = start["name"], start["kind"]
        sp, ep = start["pos"], end["pos"]
        between = ordered[sp + 1:ep]
        imgs = [bid for bid in between if byid.get(bid, {}).get("block_type") == 27]
        hv = has_value(vars_all.get(name, ""))
        keep = (hv if kind == "cond+" else not hv) if kind in ("cond+", "cond-") else hv
        if not keep:
            _delete_segment(between, start, end, sp, ep, byid, title_start_of, add_edit, del_bids)
            continue
        add_edit(start["bid"], start["start"], start["end"])
        add_edit(end["bid"], end["start"], end["end"])
        if kind == "var" and name not in shared:
            atts = attachments(vars_all.get(name, ""))
            for bid, att in zip(imgs, atts):
                image_tasks.append((bid, att))
            for bid in imgs[len(atts):]:
                del_bids.add(bid)

    text_requests = _to_text_requests(items, edits, del_bids, byid)
    return del_ranges_of(items, del_bids), text_requests, image_tasks


# ---------------------------------------------------------------- 执行
def apply_prune(token, doc, del_ranges):
    for pid, rs in del_ranges.items():
        if rs:
            fs.batch_delete(token, doc, pid, rs)


# 换图上传归一：同文档同源 token 只下载一次（cache）+上传一次，file_token 复用
_upload_map = {}

# 源快照留底：main() 载入后快照一份，作为本轮比对的基准（S.MAP 会随 enqueue 更新）
_prev_map = {}


def swap_images(token, doc, image_tasks):
    """换图（按源归一）。返回 [(block_id, src_token, new_token, w, h), ...] 供台账记录。"""
    ready, records = [], []
    for block_id, att in image_tasks:
        stok = att["attachmentToken"]
        cached = _upload_map.get((doc, stok))
        if cached:
            new_token, w, h = cached
        else:
            local, _size, _hit = fs.download_attachment(token, stok)
            new_token = fs.upload_local_file(token, local, block_id)
            w, h = int(att.get("width", 0) or 0), int(att.get("height", 0) or 0)
            _upload_map[(doc, stok)] = (new_token, w, h)
        ready.append((block_id, new_token, w, h))
        records.append((block_id, stok, new_token, w, h))
    fs.replace_images_batch(token, doc, ready)
    return records


def register_sources(subject, product, fields, var_names):
    """把变量的当前源值登记进 sources（新文档首次填入 / 更新替换后回写）。"""
    for var in var_names:
        kind, tok, vh = source_of(fields, var)
        S.enqueue(subject, product, var, kind, tok, vh)


# ---------------------------------------------------------------- 更新链路
def update_doc(token, doc, subject, product, fields, shape, tpl_blocks):
    """已有文档：按源检测 → 反推块 → 原地替换；修不了返回原因，不动文档。

    返回："skip" / ("updated", n) / "blocked:原因"（重建是手动功能，不在此做）。
    """
    led = L.lookup_doc(doc)
    if not led:
        return "blocked:台账缺失"
    if not tpl_blocks:
        return "blocked:模板无标记"

    # ① 按源比对（内存，零 API）。首登（sources 无记录）只登记不动文档。
    changed, fresh = {}, []
    for var in led:
        kind, tok, vh = source_of(fields, var)
        prev = _prev_map.get((subject, product, var))
        if prev is None:
            fresh.append((var, kind, tok, vh))
            continue
        if not (prev.get("kind") == kind and prev.get("src_token", "") == tok
                and prev.get("value_hash", "") == vh):
            changed[var] = (kind, tok, vh)
    for var, k, t, v in fresh:
        S.enqueue(subject, product, var, k, t, v)
    if not changed:
        return "skip"

    # ② 反推 + 验证（读一次文档块树）
    items = fs.list_blocks(token, doc)
    byid = {b["block_id"]: b for b in items}
    text_updates, image_jobs = {}, []
    for var, (_k, tok, vh) in changed.items():
        entry = led.get(var) or {}
        tpl_bid = entry.get("template_block_id") or ""
        shape_entry = shape.get(tpl_bid) if tpl_bid else None
        if not shape_entry:
            return f"blocked:{var} 模板形状缺失（模板改过）"
        bids = entry.get("blocks") or []
        imgs = entry.get("images") or []
        atts = attachments(fields.get(var))
        has_old = bool(entry.get("src_token")) or _prev_hash(subject, product, var) != EMPTY_HASH
        has_new = bool(tok) or vh != EMPTY_HASH
        if has_old != has_new:
            return f"blocked:{var} 值有无翻转（段落增删）"
        if not has_new:
            continue
        role = next((m.get("role") for m in shape_entry["marks"] if m["name"] == var), None)
        if role == "value":
            if not bids or any(b not in byid for b in bids):
                return f"blocked:{var} 占位符块丢失"
            text_updates[bids[0]] = rebuild_text(shape_entry, fields)
        elif imgs:
            if len(imgs) != len(atts) or any(b not in byid for b in imgs):
                return f"blocked:{var} 图片块数量/位置不符"
            for bid, att in zip(imgs, atts):
                image_jobs.append((bid, att))
        # 内容段/条件段同有：内容是模板预写文字，不随数据源变 → 无操作

    # ③ 原地替换（同文档多个替换合并批做）
    n = 0
    text_reqs = []
    for bid, new_text in text_updates.items():
        if text_of(byid.get(bid, {})) != new_text:
            text_reqs.append({"block_id": bid, "update_text_elements": {
                "elements": [{"text_run": {"content": new_text}}]}})
    if text_reqs:
        n += fs.batch_update_blocks(token, doc, text_reqs)
    if image_jobs:
        records = swap_images(token, doc, image_jobs)
        n += len(records)
        by_block = {}
        for bid, src, dst, w, h in records:
            by_block.setdefault(bid, []).append((src, dst, w, h))
        for var, (_k, _t, _v) in changed.items():
            entry = led.get(var) or {}
            pairs = []
            for ib in entry.get("images", []):
                pairs.extend(by_block.get(ib, []))
            if pairs:
                L.mark_replaced(doc, var, ",".join(x[1] for x in pairs),
                                pairs[0][2], pairs[0][3])
    # ④ 回写变化源的新快照
    for var, (kind, tok, vh) in changed.items():
        S.enqueue(subject, product, var, kind, tok, vh)
    return ("updated", n)


def _prev_snapshot(subject, product, var):
    """enqueue 前的旧快照（比对用；enqueue 会覆盖 MAP，所以循环前抓一次）。"""
    old = _prev_map.get((subject, product, var)) or {}
    return (old.get("kind", ""), old.get("src_token", ""), old.get("value_hash", ""))


def _prev_hash(subject, product, var):
    return _prev_snapshot(subject, product, var)[2]


# ---------------------------------------------------------------- 主流程
def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    print("[0/6] D1 自动建表 + 源快照载入 ...")
    d1.ensure_schema()
    n_src = S.load_all()
    _prev_map.update(S.MAP)  # 旧快照留底（比对基准），MAP 之后随 enqueue 更新
    print(f"  源快照 {n_src} 条")

    print("[1/6] 取 tenant token ...")
    token = fs.get_token()

    print("[2/6] 读模板表 → 交叉自算 ...")
    templates = fs.list_records(token, APP_TOKEN, TABLE_TEMPLATE, TEMPLATE_FIELDS)
    tpl_infos = []  # [{tpl_id, tpl_doc, name, platform, pairs:[(subject,product)]}]
    pair_set = set()
    for t in templates:
        f = t["fields"]
        tpl_doc = field_text(f.get("文档ID"))
        if not tpl_doc:
            continue  # 模板ID 是开关：文档ID 空 = 未启用
        subjects = [s.strip() for s in field_text(f.get("启用主体")).split(",") if s.strip()]
        products = [s.strip() for s in field_text(f.get("启用产品")).split(",") if s.strip()]
        if not subjects or not products:
            continue
        pairs = [(s, p) for s in subjects for p in products]
        pair_set.update(pairs)
        tpl_infos.append({
            "tpl_id": tpl_doc,
            "tpl_doc": tpl_doc,
            "name": field_text(f.get("名称")) or tpl_doc,
            "platform": field_text(f.get("平台")),
            "pairs": pairs,
        })
    all_pairs = sorted(pair_set)
    print(f"  模板 {len(tpl_infos)} 个，交叉对 {len(all_pairs)}")

    print("[3/6] 读资源表 → 交叉补行 ...")
    resources = fs.list_records(token, APP_TOKEN, TABLE_RESOURCE, RESOURCE_FIELDS)
    res_map = {}
    for r in resources:
        f = r["fields"]
        s, p = field_text(f.get("主体")), field_text(f.get("产品"))
        if s and p and (s, p) not in res_map:
            res_map[(s, p)] = f
    missing = [(s, p) for (s, p) in all_pairs if (s, p) not in res_map]
    if missing:
        fs.create_records_batch(token, APP_TOKEN, TABLE_RESOURCE,
                                [{"主体": s, "产品": p} for s, p in missing])
        print(f"  交叉补行 {len(missing)} 行（Lookup 联动后下轮 run 自动带上，本轮跳过）")
    print(f"  资源行 {len(res_map)}，需补 {len(missing)}")

    # 模板驱动分组：product → [(fields, tpl_info)]
    product_groups = {}
    for ti in tpl_infos:
        for (s, p) in ti["pairs"]:
            f = res_map.get((s, p))
            if not f:
                continue  # 本轮补的行还没有变量数据
            product_groups.setdefault(p, []).append((f, ti))
    if limit:
        product_groups = dict(list(product_groups.items())[:limit])

    folder_cache = {}
    doc_rows = []
    counters = {"new": 0, "updated": 0, "blocked": 0, "skipped": 0, "failed": 0}
    d1.reset_usage()

    for product, rows in product_groups.items():
        print(f"\n=== 产品: {product} ({len(rows)}) ===")
        if product not in folder_cache:
            folder_cache[product] = fs.ensure_folder(token, OUTPUT_FOLDER, product)
        folder = folder_cache[product]

        by_tpl = {}
        for f, ti in rows:
            by_tpl.setdefault(ti["tpl_id"], (ti, []))[1].append(f)

        for tpl_id, (ti, subset) in by_tpl.items():
            print(f"[模板] {ti['name']} × {len(subset)} 主体")
            tpl_items = fs.list_blocks(token, tpl_id)   # 原模板块树（ID 稳定）
            shape, tpl_blocks = tpl_shape_of(tpl_items)
            master_cache = {}

            def get_master(tpl_id=tpl_id, ti=ti, product=product, subset=subset, folder=folder):
                if tpl_id not in master_cache:
                    minfo = fs.list_blocks(token, ti["tpl_doc"])
                    master = fs.copy_docx(token, ti["tpl_doc"],
                                          f"【母版】{product}-{ti['name']}", folder)
                    m_del, m_text, m_imgs, shared, _mb = plan_master(minfo, subset)
                    fs.batch_update_blocks(token, master, m_text)
                    apply_prune(token, master, m_del)
                    swap_images(token, master, m_imgs)
                    master_cache[tpl_id] = (master, shared)
                    print(f"  [母版] 裁剪完成：共用图 {sorted(shared)}")
                return master_cache[tpl_id]

            for idx, f in enumerate(subset, 1):
                subject = field_text(f.get("主体"))
                t1, t2, reg = field_text(f.get("商标1")), field_text(f.get("商标2")), field_text(f.get("注册号"))
                title = f"{t1}{t2}{product}（{reg}）-{subject}"
                doc_key = f"{subject}|{product}|{ti['platform']}|{tpl_id}"

                try:
                    exist = d1.query(
                        "SELECT doc_token FROM __PREFIX__documents WHERE doc_key = ?", [doc_key]
                    )
                    if exist:
                        doc = exist[0]["doc_token"]
                        result = update_doc(
                            token, doc, subject, product, f, shape, tpl_blocks)
                        if result == "skip":
                            counters["skipped"] += 1
                            print(f"  [{idx}/{len(subset)}] 无变化 {title[:20]}")
                            continue
                        if isinstance(result, tuple):
                            counters["updated"] += 1
                            print(f"  [{idx}/{len(subset)}] 更新 {title[:20]}（{result[1]} 处）")
                            continue
                        counters["blocked"] += 1
                        print(f"  [{idx}/{len(subset)}] [不动] {result} {title[:20]}")
                        continue
                    else:
                        counters["new"] += 1
                        print(f"  [{idx}/{len(subset)}] 新建 {title[:20]}")

                    master, shared = get_master()
                    doc = fs.copy_docx(token, master, title, folder)
                    items = fs.list_blocks(token, doc)
                    del_ranges, text_requests, image_tasks = plan_doc(items, f, shared)
                    led = L.collect(items, f)
                    fs.batch_update_blocks(token, doc, text_requests)
                    apply_prune(token, doc, del_ranges)
                    img_records = swap_images(token, doc, image_tasks)
                    by_block = {}
                    for bid, src, dst, w, h in img_records:
                        by_block.setdefault(bid, []).append((src, dst, w, h))
                    L.attach_features(led, items, by_block, tpl_blocks)

                    now = int(time.time())
                    doc_rows.append({
                        "主体": subject, "产品": product, "平台": ti["platform"],
                        "状态": "done",
                        "文档链接": {"link": f"https://my.feishu.cn/docx/{doc}", "text": doc},
                    })
                    L.enqueue(doc, subject, product, led)
                    L.enqueue_document(doc_key, doc, subject, product, ti["platform"], tpl_id, now)
                    register_sources(subject, product, f, led.keys())
                except Exception as exc:
                    counters["failed"] += 1
                    print(f"  [{idx}/{len(subset)}] [失败] {title[:20]}: {exc}")

    # ------- 收尾：飞书回填 + 台账 + 注册表 + 源快照，各一次批
    if doc_rows:
        fs.create_records_batch(token, APP_TOKEN, TABLE_DOCUMENT, doc_rows)
        print(f"\n文档表回填：{len(doc_rows)} 行，1 次 batch_create")
    n_ledger = L.flush()
    n_src_w = S.flush()
    u = d1.usage()
    print(f"台账写入 {n_ledger} 行、源快照 {n_src_w} 行；"
          f"D1 {u['calls']} 次 / 读 {u['rows_read']} 行 / 写 {u['rows_written']} 行")
    print(f"\n完成：{counters}")
    return counters


if __name__ == "__main__":
    main()
