#!/usr/bin/env python3
"""定向批量修改：按 D1 台账定位，只替换、不删块不重建。

场景：给所有厚朴堂文档换营业执照 / 把某产品的注册号统一改成新值。
  python gha/update.py --subject 厚朴堂 --var 营业执照 --file new.jpg
  python gha/update.py --product 参柏 --var 注册号 --text 123456

流程：查台账拿 (doc_token, 块ID) → 上传新图/改文本 → 批量替换 → 回写台账特征。
与生成流程的区别：这里不解析标记、不删块、不复制 —— 台账已记录变量到块的映射。
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu as fs
import ledger as L
import d1


def find_targets(subject=None, product=None, var=None):
    """查 D1 台账，返回 [(doc_token, {var: entry}), ...]。"""
    return [(doc, vmap) for doc, _s, _p, vmap in L.lookup(subject, product, var)]


def replace_image_for(token, doc, block_ids, file_path, width=0, height=0):
    """本地图片换到指定文档的一组块：逐块上传 → 一次批量 replace。
    返回 [(block_id, new_token), ...] 供回写。"""
    ready, pairs = [], []
    size = os.path.getsize(file_path)
    if size < 100:
        raise fs.FeishuError(f"文件过小: {file_path}")
    for bid in block_ids:
        new_token = fs.upload_local_file(token, file_path, bid)
        ready.append((bid, new_token, int(width), int(height)))
        pairs.append((bid, new_token))
    fs.replace_images_batch(token, doc, ready)
    return pairs


def set_text_for(token, doc, entries):
    """entries: [(block_id, new_text), ...] → 一次 batch_update。"""
    reqs = [{
        "block_id": bid,
        "update_text_elements": {"elements": [{"text_run": {"content": text}}]},
    } for bid, text in entries]
    return fs.batch_update_blocks(token, doc, reqs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", help="主体模糊匹配（如 厚朴堂）")
    ap.add_argument("--product", help="产品模糊匹配")
    ap.add_argument("--var", required=True, help="变量名（如 营业执照）")
    ap.add_argument("--file", help="新图片文件（图片变量）")
    ap.add_argument("--text", help="新文本（文本变量，与 --file 二选一）")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="只列出将改哪些文档/块")
    args = ap.parse_args()

    if bool(args.file) == bool(args.text):
        print("错误：--file 和 --text 必须且只能给一个")
        sys.exit(2)

    targets = find_targets(args.subject, args.product, args.var)
    print(f"命中 {len(targets)} 份文档")
    if args.dry_run:
        for doc, vmap in targets:
            entry = vmap.get(args.var, {})
            bids = entry.get("images" if args.file else "blocks", [])
            print(f"  {doc[:10]} 变量={args.var} 目标块={[b[:8] for b in bids]}")
        return
    if not targets:
        return

    d1.ensure_schema(verbose=False)
    token = fs.get_token()
    total = 0
    for doc, vmap in targets:
        entry = vmap.get(args.var)
        if not entry:
            print(f"  [skip] {doc[:10]} 台账无变量 {args.var}")
            continue
        bids = entry.get("images" if args.file else "blocks", [])
        if args.file:
            pairs = replace_image_for(token, doc, bids, args.file, args.width, args.height)
            # 换完回写：把新的 dst_token 写回台账（每个目标块可能对应多个变量，按 var 汇总）
            if pairs:
                L.mark_replaced(doc, args.var, ",".join(t for _, t in pairs),
                                args.width, args.height)
            n = len(pairs)
        else:
            n = set_text_for(token, doc, [(b, args.text) for b in bids])
            L.mark_replaced(doc, args.var, "", args.width, args.height)
        print(f"  {doc[:10]} 完成 {n} 块")
        total += n
    print(f"\n完成：{total} 块，{len(targets)} 份文档")


if __name__ == "__main__":
    main()
