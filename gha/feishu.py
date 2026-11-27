"""飞书 API 最小封装：tenant token、多维表、云盘文件夹、文档块树、图片。

- 凭证从环境变量取（FEISHU_APP_ID / FEISHU_APP_SECRET），不落代码。
- curl 强制 -4 --noproxy '*'：飞书直连，不走 GitHub 的代理（代理只给 GitHub）。
- 图片下载先查本地缓存（cache.py），命中则跳过下载。
"""
import os
import json
import time
import subprocess
import tempfile

import cache

BASE = "https://open.feishu.cn/open-apis"

# 官方可重试错误码：临时错误 / 应用限频(3次每秒) / 文档复制中禁止编辑
RETRY_CODES = (1771001, 2200, 99991672, 99991400, 1770020)


class FeishuError(RuntimeError):
    pass


def curl(args, retries=10, timeout=40):
    """发一次 curl；-4 --noproxy '*' 保证飞书直连不走代理。带递增退避。"""
    last = ""
    for i in range(retries):
        out = subprocess.run(
            ["curl", "-4", "--noproxy", "*", "-s", "--max-time", str(timeout)] + args,
            capture_output=True, text=True,
        ).stdout
        last = out
        try:
            d = json.loads(out)
            if d.get("code") == 0:
                return d
            if d.get("code") not in RETRY_CODES:
                raise FeishuError(f"feishu code={d.get('code')} msg={d.get('msg')}")
        except json.JSONDecodeError:
            pass
        time.sleep(min(0.3 * (i + 1), 2.0))
    raise FeishuError(f"feishu request failed: {last[:300]}")


def auth_headers(token):
    return ["-H", f"Authorization: Bearer {token}"]


def get_token():
    d = curl([
        "-X", "POST", f"{BASE}/auth/v3/tenant_access_token/internal",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({
            "app_id": os.environ["FEISHU_APP_ID"],
            "app_secret": os.environ["FEISHU_APP_SECRET"],
        }),
    ])
    return d["tenant_access_token"]


# ------------------------------------------------------------------ 多维表
def list_records(token, app_token, table_id, field_names=None, page_size=500):
    """列出记录（search 接口，page_size 官方上限 500），自动翻页。"""
    records = []
    page_token = ""
    for _ in range(50):
        params = f"page_size={page_size}&page_token={page_token}"
        body = {"field_names": field_names} if field_names else {}
        d = curl([
            "-X", "POST",
            f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search?{params}",
        ] + auth_headers(token) + [
            "-H", "Content-Type: application/json", "-d", json.dumps(body),
        ])
        records += d["data"].get("items", [])
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token", "")
        if not page_token:
            break
    return records


def create_records_batch(token, app_token, table_id, rows, chunk=1000):
    """批量新增（官方 batch_create 单次≤1000、50QPS）。全成功或全失败；
    同表不支持并发写，这里顺序分批。rows: [fields_dict, ...]。"""
    out = []
    for i in range(0, len(rows), chunk):
        d = curl([
            "-X", "POST",
            f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
        ] + auth_headers(token) + [
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"records": [{"fields": f} for f in rows[i:i + chunk]]}),
        ])
        out.extend(d.get("data", {}).get("records", []))
    return out


# -------------------------------------------------------------------- 云盘
def list_folder(token, folder_token):
    d = curl([
        f"{BASE}/drive/v1/files?page_size=100&folder_token={folder_token}",
    ] + auth_headers(token))
    return d["data"].get("files", [])


def ensure_folder(token, parent_token, name):
    for f in list_folder(token, parent_token):
        if f.get("name") == name and f.get("type") == "folder":
            return f["token"]
    d = curl([
        "-X", "POST", f"{BASE}/drive/v1/files/create_folder",
    ] + auth_headers(token) + [
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"name": name, "folder_token": parent_token}),
    ])
    return d["data"]["token"]


def copy_docx(token, source_token, name, folder_token):
    """copy 是异步接口，复制中编辑报 1770020（curl 已纳入重试）。"""
    d = curl([
        "-X", "POST", f"{BASE}/drive/v1/files/{source_token}/copy",
    ] + auth_headers(token) + [
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"name": name, "folder_token": folder_token, "type": "docx"}),
    ])
    return d["data"]["file"]["token"]


# -------------------------------------------------------------------- 文档
def list_blocks(token, doc_token):
    items, page_token = [], ""
    for _ in range(50):
        d = curl([
            f"{BASE}/docx/v1/documents/{doc_token}/blocks?page_size=500&page_token={page_token}",
        ] + auth_headers(token))
        items += d["data"]["items"]
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token", "")
        if not page_token:
            break
    return items


def batch_delete(token, doc_token, parent_id, ranges):
    """ranges: [(start,end), ...]，一次删一个连续区间（官方限制），从后往前删。"""
    for s, e in sorted(set(ranges), reverse=True):
        curl([
            "-X", "DELETE",
            f"{BASE}/docx/v1/documents/{doc_token}/blocks/{parent_id}/children/batch_delete?document_revision_id=-1",
        ] + auth_headers(token) + [
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"start_index": s, "end_index": e}),
        ])


def batch_update_blocks(token, doc_token, requests):
    """一次 PATCH 改多个块（官方 ≤200 requests、同一 block_id 不可重复）。
    不改块数量/顺序，故之后按原树 index 做 batch_delete 仍准确。"""
    if not requests:
        return 0
    done = 0
    for i in range(0, len(requests), 200):
        chunk = requests[i:i + 200]
        curl([
            "-X", "PATCH",
            f"{BASE}/docx/v1/documents/{doc_token}/blocks/batch_update?document_revision_id=-1",
        ] + auth_headers(token) + [
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"requests": chunk}),
        ])
        done += len(chunk)
    return done


# -------------------------------------------------------------------- 图片
def download_attachment(token, attachment_token):
    """下载附件到本地缓存，返回 (本地路径, 大小, 是否命中缓存)。"""
    hit = cache.get(attachment_token)
    if hit:
        return hit, os.path.getsize(hit), True
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tf:
        path = tf.name
    subprocess.run(
        ["curl", "-4", "--noproxy", "*", "-s", "-L", "--max-time", "40", "-o", path,
         f"{BASE}/drive/v1/medias/{attachment_token}/download"] + auth_headers(token),
        capture_output=True,
    )
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size < 100:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise FeishuError(f"附件下载过小: {attachment_token}")
    cached = cache.put(attachment_token, path)
    os.unlink(path)
    return cached, size, False


def upload_local_file(token, local_path, block_id):
    """把本地文件传到目标图片块（parent_node 必须是块 id），返回 new file_token。"""
    size = os.path.getsize(local_path)
    if size < 100:
        raise FeishuError(f"文件过小: {local_path}")
    d = curl([
        "-X", "POST", f"{BASE}/drive/v1/medias/upload_all",
    ] + auth_headers(token) + [
        "-F", "file_name=image.jpg",
        "-F", "parent_type=docx_image",
        "-F", f"parent_node={block_id}",
        "-F", f"size={size}",
        "-F", f"file=@{local_path}",
    ], timeout=90)
    return d["data"]["file_token"]


def replace_images_batch(token, doc_token, images):
    """images: [(block_id, new_token, width, height), ...]；
    一次 batch_update 放多个 replace_image，官方单请求 Image≤20 → 分批。"""
    done = 0
    for i in range(0, len(images), 20):
        reqs = []
        for block_id, new_token, width, height in images[i:i + 20]:
            payload = {"token": new_token}
            if width > 0 and height > 0:
                payload["width"] = int(width)
                payload["height"] = int(height)
            reqs.append({"block_id": block_id, "replace_image": payload})
        if reqs:
            batch_update_blocks(token, doc_token, reqs)
            done += len(reqs)
    return done
