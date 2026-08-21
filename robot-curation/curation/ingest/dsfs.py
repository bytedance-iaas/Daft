"""数据集文件系统(只读):本地路径与 `tos://桶/前缀` 同一套读法。

为什么有这个模块(2026-08-21 用户拍板"读端会说 tos://"):摄入在逻辑上是懒的(只读
meta、视频按指针延迟到用时才解码),但所有读端此前只认本地路径 —— 挂载模式下挂载把桶
变成了路径,直连模式下没人变,于是只好把数据集整包搬进 pod(stage_in),DROID 级别根本
搬不动。病根是"桶不是路径",正解是让读端自己会读桶。

约定:
- 路径形态决定走哪条:`tos://` 开头走 TOS SDK,其余一律按本地文件系统。**本模块只读**,
  写端仍是 safe_write 发布通道 / stage_out,绝不在这里加写操作。
- 小文件(info.json / episodes 元数据 / data parquet)整对象取回内存;视频**不下载**,
  `media_source()` 给出预签名 URL,PyAV 直接按 HTTP Range 顺序读(2026-08-21 实测:21 MB
  视频整段解码 1.0s,跳中段 0.42s,可接受)。
- 远端 `exists/isdir/listdir/glob` 靠**一次性列清单**(`prefetch(root)`):数据集根下全部对象
  一次列完进内存,之后全是字典查找 —— v2 社区集(DROID 9 万条)逐条 exists 若每次 HEAD 一下
  就是几十万次网络调用。没有预取过的路径退到一次 list 调用,结果不缓存。
- 地区/凭证:`configure(region)` 由 CLI 在开跑前调一次(--input-region);没调就按部署
  的 TOS_REGION。凭证同 tos_store(环境变量)。
"""
from __future__ import annotations

import fnmatch
import io
import json
import os
import shutil
import time

TOS_PREFIX = "tos://"
_REGION: dict = {"value": None}
_STORES: dict = {}
_LISTINGS: dict = {}          # (bucket, root_prefix) → _Listing
PRESIGN_EXPIRES_S = 2 * 3600  # 每次 media_source 都现签(本地 HMAC,不出网),过期只影响单次解码


def configure(region: str | None = None) -> None:
    """设置远端读取用的地区(CLI --input-region);传 None 退回部署默认。"""
    _REGION["value"] = str(region or "").strip() or None
    _STORES.clear()


def current_region() -> str:
    """远端读取实际用的地区(configure 的,否则部署的 TOS_REGION;都没有给空串)。"""
    return _REGION["value"] or os.environ.get("TOS_REGION", "").strip()


def is_remote(path) -> bool:
    return str(path or "").startswith(TOS_PREFIX)


def join(base: str, *parts: str) -> str:
    """远端按 POSIX 拼、保留 scheme;本地交给 os.path.join(行为与以前逐字节一致)。"""
    if is_remote(base):
        tail = "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))
        return str(base).rstrip("/") + ("/" + tail if tail else "")
    return os.path.join(base, *parts)


def basename(path: str) -> str:
    return str(path or "").rstrip("/").rsplit("/", 1)[-1]


# ── 远端 ──────────────────────────────────────────────────────────────────────

def _split(url: str) -> tuple[str, str]:
    from .. import tos_store
    bucket, prefix = tos_store.parse_tos_url(url)
    return bucket, prefix.strip("/")


def _store(bucket: str | None = None):
    """按桶挑客户端:公共(匿名)桶不签名、地区用登记值;其余用部署凭证 + 调用方地区。"""
    from .. import tos_store
    if bucket and tos_store.is_anonymous_bucket(bucket):
        key = ("anon", bucket)
        if key not in _STORES:
            _STORES[key] = tos_store.make_store_for(bucket, _REGION["value"])
        return _STORES[key]
    key = _REGION["value"] or ""
    if key not in _STORES:
        _STORES[key] = tos_store.make_store(_REGION["value"])
    return _STORES[key]


RETRY_ATTEMPTS = 3
_RETRY_SLEEP_S = (1.0, 3.0)


def _retry(what: str, fn):
    """远端读的瞬断兜底:列清单/取对象失败重试两次(1s、3s),最后一次原样抛。
    凭证/桶名这类确定性错误也会重试两次 —— 代价是几秒,换来的是不用在这里分辨
    SDK 几十种异常哪些算瞬断。"""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 SDK/网络异常族杂
            if attempt == RETRY_ATTEMPTS:
                raise
            print(f"[curation] TOS {what}失败(第 {attempt}/{RETRY_ATTEMPTS} 次):"
                  f"{type(e).__name__}: {str(e)[:100]} —— 重试", flush=True)
            time.sleep(_RETRY_SLEEP_S[min(attempt - 1, len(_RETRY_SLEEP_S) - 1)])


class _Listing:
    """一个数据集根下的对象清单:key → (size, etag),外加全部"目录"前缀。"""

    def __init__(self, bucket: str, root: str):
        self.bucket, self.root = bucket, root
        self.files: dict[str, tuple[int, str]] = {}
        self.dirs: set[str] = {root} if root else set()

    def add(self, key: str, size: int, etag: str) -> None:
        if key.endswith("/"):                       # 目录标记对象:只当目录
            self.dirs.add(key.rstrip("/"))
            return
        self.files[key] = (int(size), str(etag or ""))
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        while parent and parent not in self.dirs:
            self.dirs.add(parent)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""

    def covers(self, key: str) -> bool:
        return key == self.root or key.startswith(self.root + "/") or not self.root


def prefetch(root_url: str, *, store=None, quiet: bool = False) -> int:
    """把数据集根下全部对象列进内存(幂等);返回对象数。慢步骤不静默:超过一页就报一行。"""
    bucket, root = _split(root_url)
    if (bucket, root) in _LISTINGS:
        return len(_LISTINGS[(bucket, root)].files)
    st = store or _store(bucket)
    t0 = time.time()

    def _list():
        lst = _Listing(bucket, root)
        for key, size, etag in st.iter_object_meta(bucket, root + "/" if root else ""):
            lst.add(key, size, etag)
        return lst
    lst = _retry(f"列清单 {root_url}", _list)
    _LISTINGS[(bucket, root)] = lst
    if not quiet and (len(lst.files) > 1000 or time.time() - t0 > 2):
        print(f"[curation] 列出 {root_url} 的对象清单:{len(lst.files)} 个文件"
              f"({time.time() - t0:.1f}s)", flush=True)
    return len(lst.files)


def forget(root_url: str | None = None) -> None:
    """丢掉清单缓存(单测/同进程换数据集用)。"""
    if root_url is None:
        _LISTINGS.clear()
        return
    bucket, root = _split(root_url)
    _LISTINGS.pop((bucket, root), None)


def _listing_for(bucket: str, key: str) -> _Listing | None:
    for (b, _root), lst in _LISTINGS.items():
        if b == bucket and lst.covers(key):
            return lst
    return None


def _probe(bucket: str, key: str) -> tuple[str | None, int, str]:
    """没预取过的路径:一次 list 判断是文件还是目录 → (kind, size, etag)。"""
    st = _store(bucket)

    def _look():
        kind, size, etag = None, 0, ""
        for k, s, e in st.iter_object_meta(bucket, key):
            if k == key:
                kind, size, etag = "file", s, e
                break
            if k.startswith(key + "/"):
                kind = "dir"
                break
            # 同前缀的别的文件(a/b 与 a/bc):继续看下一条
        return kind, size, etag
    return _retry(f"探测 {key}", _look)


def _stat(url: str) -> tuple[str | None, int, str]:
    bucket, key = _split(url)
    lst = _listing_for(bucket, key)
    if lst is not None:
        if key in lst.files:
            return ("file", *lst.files[key])
        if key in lst.dirs:
            return "dir", 0, ""
        return None, 0, ""
    return _probe(bucket, key)


# ── 统一 API ───────────────────────────────────────────────────────────────────

def exists(path: str) -> bool:
    if not is_remote(path):
        return os.path.exists(path)
    return _stat(path)[0] is not None


def isdir(path: str) -> bool:
    if not is_remote(path):
        return os.path.isdir(path)
    return _stat(path)[0] == "dir"


def isfile(path: str) -> bool:
    if not is_remote(path):
        return os.path.isfile(path)
    return _stat(path)[0] == "file"


def listdir(path: str) -> list[str]:
    """一层子项名(文件 + 目录),排序。远端没预取过就 delimiter 列一层。"""
    if not is_remote(path):
        return sorted(os.listdir(path))
    bucket, key = _split(path)
    lst = _listing_for(bucket, key)
    names: set[str] = set()
    if lst is not None:
        pre = key + "/" if key else ""
        for k in list(lst.files) + list(lst.dirs):
            if k.startswith(pre) and k != key:
                names.add(k[len(pre):].split("/", 1)[0])
    else:
        st = _store(bucket)
        names.update(st.iter_common_prefixes(bucket, key))
        pre = key + "/" if key else ""
        for k, _s, _e in st.iter_object_meta(bucket, pre):
            rel = k[len(pre):]
            if rel and "/" not in rel:
                names.add(rel)
    return sorted(n for n in names if n)


def glob(pattern: str) -> list[str]:
    """只支持"无通配的根 + 带通配的相对模式"(本仓库的两种用法:
    meta/episodes/chunk-*/file-*.parquet 与 *.rrd)。远端按相对键 fnmatch。"""
    if not is_remote(pattern):
        import glob as _glob
        return sorted(_glob.glob(pattern))
    bucket, key = _split(pattern)
    parts = key.split("/")
    i = next((n for n, p in enumerate(parts) if any(c in p for c in "*?[")), len(parts))
    root, rel = "/".join(parts[:i]), "/".join(parts[i:])
    if not rel:
        return [pattern] if exists(pattern) else []
    lst = _listing_for(bucket, root)
    if lst is None:
        prefetch(f"{TOS_PREFIX}{bucket}/{root}", quiet=True)
        lst = _listing_for(bucket, root)
    pre = root + "/" if root else ""
    out = [f"{TOS_PREFIX}{bucket}/{k}" for k in lst.files
           if k.startswith(pre) and fnmatch.fnmatchcase(k[len(pre):], rel)]
    return sorted(out)


def read_bytes(path: str) -> bytes:
    if not is_remote(path):
        with open(path, "rb") as f:
            return f.read()
    bucket, key = _split(path)
    return _retry(f"取对象 {key}", lambda: _store(bucket).get_bytes(bucket, key))


def open_text(path: str, encoding: str = "utf-8"):
    """文本句柄(with 用);远端是整对象取回后的 StringIO。"""
    if not is_remote(path):
        return open(path, encoding=encoding)
    return io.StringIO(read_bytes(path).decode(encoding))


def read_json(path: str):
    with open_text(path) as f:
        return json.load(f)


def read_parquet(path: str):
    import pandas as pd
    if not is_remote(path):
        return pd.read_parquet(path)
    return pd.read_parquet(io.BytesIO(read_bytes(path)))


def mtime_key(path: str):
    """"文件变没变"的键:本地 mtime;远端 etag+size(清单里现成,不出网)。"""
    if not is_remote(path):
        return os.path.getmtime(path)
    kind, size, etag = _stat(path)
    if kind != "file":
        raise FileNotFoundError(path)
    return f"{etag}:{size}"


def content_identity(path: str) -> str:
    """远端文件的内容身份(去重指纹用):etag+size —— 不把整段视频拉回来算哈希。"""
    kind, size, etag = _stat(path)
    if kind != "file":
        raise FileNotFoundError(path)
    return f"tos-etag:{etag}:{size}"


def media_source(path: str) -> str:
    """给 PyAV/ffmpeg 的媒体源:本地路径原样;远端 = 预签名 URL(现签,不出网)。"""
    if not is_remote(path):
        return path
    bucket, key = _split(path)
    return _store(bucket).presign(bucket, key, expires=PRESIGN_EXPIRES_S)


def copy_to_local(path: str, dst: str) -> None:
    """整文件拷到本地(导出时拷 stats.json / v2 mp4 用):顺序写,与 FSX 安全写法一致。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    if not is_remote(path):
        shutil.copyfile(path, dst)
        return
    bucket, key = _split(path)
    kind, size, _etag = _stat(path)
    _retry(f"下载 {key}", lambda: _store(bucket).download(
        bucket, key, dst, size=size if kind == "file" else None))
