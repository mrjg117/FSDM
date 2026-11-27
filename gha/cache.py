"""图片素材本地缓存：按 attachmentToken 存文件，命中就不重新下载。

- 缓存目录由 FSDM_IMAGE_CACHE 指定，默认 gha/.image_cache。
- GHA 里用 actions/cache 持久化这个目录：key=fsdm-img-<日期>，
  restore-keys=fsdm-img- 前缀命中上次缓存；定时 job restore 后 save 新 key 续期。
- 命中判断只看 token 文件在不在、大小是否 >0；文件随 GHA cache 走，
  本地跑也存在磁盘，跨 run 复用。
"""
import os

CACHE_DIR = os.environ.get("FSDM_IMAGE_CACHE", "")


def _dir():
    d = CACHE_DIR or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".image_cache")
    os.makedirs(d, exist_ok=True)
    return d


def path_for(token):
    """返回该 attachmentToken 对应的本地缓存文件路径。"""
    # token 是飞书附件串，直接当文件名安全（无路径分隔符）
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(token))
    return os.path.join(_dir(), safe)


def get(token):
    """命中返回本地路径，未命中返回 None。空文件视为未命中。"""
    p = path_for(token)
    if os.path.isfile(p) and os.path.getsize(p) > 100:
        return p
    return None


def put(token, src_path):
    """把下载好的临时文件放进缓存，返回缓存路径。"""
    p = path_for(token)
    tmp = p + ".tmp"
    with open(src_path, "rb") as a, open(tmp, "wb") as b:
        while True:
            buf = a.read(1 << 20)
            if not buf:
                break
            b.write(buf)
    os.replace(tmp, p)
    return p
