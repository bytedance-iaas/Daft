"""公共数据集目录(2026-08-21):匿名可读的镜像桶 → 界面/CLI 可直接挑的数据集清单。

背景:火山内部有把 HuggingFace 整套镜像进 TOS 的公共桶(oniond 维护),布局是
    <桶>/dataset_files.json            清单:{"<名字>": ["dataset/<名字>/<文件>", …]}
    <桶>/dataset/<名字>/.metadata.json  HF 元数据(含 "id": "<org>/<名字>")
    <桶>/dataset/<名字>/…               数据集本体
谁都能匿名读,但它是整个 HF 的镜像,几百个数据集里只有个位数是 LeRobot 格式 ——
清单里每个数据集的文件列表都在,有没有 meta/info.json **本地一判就知道**,零额外
请求;只对过滤后剩下的那几个再取 meta/info.json(版本/集数)与 .metadata.json(全名)。

配置(站点 site.yaml,出厂默认不配 = 功能整个不出现):
    public_datasets:
      bucket: <镜像桶名>
      region: <桶所在地区>
      prefix: dataset                 # 数据集都在这个前缀下
      manifest: dataset_files.json    # 桶根清单的对象名
桶名/地区是部署常量,不做"猜地区"这类探测(同事脚本里那段 7 地区×3 端点的
TCP 探测在 pod 外要跑几秒,还绑着 ECS 元数据地址)。

与读端的关系:apply_config 把桶登记成匿名桶(tos_store.register_anonymous_bucket),
之后 dsfs / 探针 / 列目录对这个桶自动换不签名的客户端 —— 数据集 URL 本身就是普通
的 tos://,管道一个字不用改。公共桶只读:交付目录绝不借它(runner.borrowed_output_url)。
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

CONFIG_KEY = "public_datasets"
DEFAULT_PREFIX = "dataset"
DEFAULT_MANIFEST = "dataset_files.json"

#: 清单 ETag 多久复查一次(HEAD 一次 0.1s,但下拉每次打开都查也没必要)
RECHECK_S = 60.0
#: 取版本/全名的并发(过滤后只剩个位数到几十个数据集)
_FETCH_THREADS = 8

_CFG: dict = {"value": None, "loaded": False}
_CACHE: dict = {"etag": None, "entries": [], "checked": 0.0}
_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')


class PublicCatalogError(RuntimeError):
    """清单读不到/解析不了(网络或镜像侧问题,不是用户输入问题)。"""


# ── 配置 ────────────────────────────────────────────────────────────────────

def apply_config(cfg: dict | None) -> dict | None:
    """从(合并后的)配置里取 public_datasets 段;bucket 缺省/为空 = 功能关闭。
    合法时登记匿名桶并返回生效配置;显式调用覆盖环境变量的懒加载。"""
    from .. import tos_store
    sec = (cfg or {}).get(CONFIG_KEY) if isinstance(cfg, dict) else None
    _CFG["loaded"] = True
    if not isinstance(sec, dict) or not str(sec.get("bucket") or "").strip():
        _CFG["value"] = None
        _CACHE.update(etag=None, entries=[], checked=0.0)
        return None
    bucket = str(sec["bucket"]).strip()
    region = str(sec.get("region") or "").strip() or None
    val = {"bucket": bucket, "region": region,
           "prefix": str(sec.get("prefix") or DEFAULT_PREFIX).strip().strip("/"),
           "manifest": str(sec.get("manifest") or DEFAULT_MANIFEST).strip()}
    tos_store.register_anonymous_bucket(bucket, region)   # 桶名不合法在这儿抛
    if _CFG["value"] != val:
        _CACHE.update(etag=None, entries=[], checked=0.0)
    _CFG["value"] = val
    return val


def _load_from_env() -> None:
    """没人显式 apply 过就看 CURATION_CONFIG 指的站点文件(CLI 子进程/脚本场景)。"""
    if _CFG["loaded"]:
        return
    _CFG["loaded"] = True
    path = os.environ.get("CURATION_CONFIG", "").strip()
    if not path or not os.path.exists(path):
        return
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 坏 YAML 不该让读数据集的进程炸在这儿
        return
    if isinstance(data, dict) and CONFIG_KEY in data:
        try:
            apply_config(data)
        except Exception:  # noqa: BLE001
            _CFG["value"] = None


def reset() -> None:
    """单测用:忘掉配置与缓存。"""
    _CFG.update(value=None, loaded=False)
    _CACHE.update(etag=None, entries=[], checked=0.0)


def configured() -> dict | None:
    _load_from_env()
    return _CFG["value"]


def root_url() -> str:
    c = configured()
    if not c:
        return ""
    return f"tos://{c['bucket']}/{c['prefix']}" if c["prefix"] else f"tos://{c['bucket']}"


def region() -> str | None:
    c = configured()
    return c["region"] if c else None


def is_public_root(url: str) -> bool:
    r = root_url()
    return bool(r) and str(url or "").strip().rstrip("/") == r


def dataset_url(name: str) -> str:
    return f"{root_url()}/{str(name).strip().strip('/')}"


# ── 清单 ────────────────────────────────────────────────────────────────────

def _store(store=None):
    if store is not None:
        return store
    from .. import tos_store
    c = configured()
    return tos_store.make_store_for(c["bucket"], c["region"])


def _entry(st, c: dict, name: str, files: list[str]) -> dict:
    base = f"{c['prefix']}/{name}/" if c["prefix"] else f"{name}/"
    out = {"name": name, "id": name, "version": "", "episodes": None,
           "files": len(files), "url": dataset_url(name)}
    try:
        info = json.loads(st.get_bytes(c["bucket"], base + "meta/info.json"))
        out["version"] = str(info.get("codebase_version") or "")
        te = info.get("total_episodes")
        out["episodes"] = int(te) if isinstance(te, (int, float)) else None
    except Exception:  # noqa: BLE001 单个数据集的 info 坏了不拖累整份清单
        pass
    if any(f == base + ".metadata.json" for f in files):
        try:
            head = st.get_bytes(c["bucket"], base + ".metadata.json")[:2048]
            m = _ID_RE.search(head.decode("utf-8", "replace"))
            if m:
                out["id"] = m.group(1)
        except Exception:  # noqa: BLE001
            pass
    # 镜像不完整的真机事实(2026-08-21):有的数据集 mp4 全是 0 字节对象(同步没做完),
    # 有的根本没视频。开跑才发现是 416 traceback,不如在清单里就标出来 —— 一个 HEAD。
    mp4s = [f for f in files if f.endswith(".mp4")]
    if not mp4s:
        out["warning"] = "没有视频文件"
    else:
        try:
            size, _etag = st.head(c["bucket"], mp4s[0])
            if size == 0:
                out["warning"] = "视频文件为空(镜像不完整)"
        except Exception:  # noqa: BLE001
            pass
    return out


def _load_entries(st, c: dict) -> list[dict]:
    try:
        mf = json.loads(st.get_bytes(c["bucket"], c["manifest"]))
    except Exception as e:  # noqa: BLE001 SDK 异常族杂,统一成一句话
        raise PublicCatalogError(
            f"读不到公共数据集清单 tos://{c['bucket']}/{c['manifest']}:"
            f"{type(e).__name__}: {str(e)[:160]}") from e
    if not isinstance(mf, dict):
        raise PublicCatalogError(f"公共数据集清单 {c['manifest']} 不是 {{名字: [文件…]}} 形状")
    pre = f"{c['prefix']}/" if c["prefix"] else ""
    lerobot = []
    for name in sorted(mf):
        files = mf[name] if isinstance(mf[name], list) else []
        base = f"{pre}{name}/"
        if any(str(f) == base + "meta/info.json" for f in files):
            lerobot.append((name, [str(f) for f in files]))
    if not lerobot:
        return []
    with ThreadPoolExecutor(min(_FETCH_THREADS, len(lerobot))) as ex:
        return list(ex.map(lambda nf: _entry(st, c, nf[0], nf[1]), lerobot))


def catalog(*, store=None, force: bool = False, now=None) -> list[dict]:
    """LeRobot 格式的公共数据集 [{name, id, version, episodes, files, url[, warning]}],按名字排序。

    清单按 ETag 缓存:RECHECK_S 内直接给缓存;过了就 HEAD 一次清单,ETag 没变不重读。
    没配置 → 空表;读不到 → PublicCatalogError(调用方变成界面上的一句话)。
    """
    c = configured()
    if not c:
        return []
    t = time.time() if now is None else now
    if not force and _CACHE["entries"] and t - _CACHE["checked"] < RECHECK_S:
        return list(_CACHE["entries"])
    st = _store(store)
    etag = None
    try:
        _size, etag = st.head(c["bucket"], c["manifest"])
    except Exception:  # noqa: BLE001 HEAD 失败就当 ETag 未知,走完整读取
        etag = None
    if not force and etag and _CACHE["entries"] and _CACHE["etag"] == etag:
        _CACHE["checked"] = t
        return list(_CACHE["entries"])
    entries = _load_entries(st, c)
    _CACHE.update(etag=etag, entries=entries, checked=t)
    return list(entries)


def names(**kw) -> list[str]:
    return [e["name"] for e in catalog(**kw)]


def resolve(query: str, **kw) -> dict | None:
    """名字(`libero` / `HuggingFaceVLA/libero` / 完整 tos:// URL)→ 条目;没有 → None。"""
    q = str(query or "").strip().rstrip("/")
    if not q:
        return None
    if q.startswith("tos://"):
        if not q.startswith(root_url() + "/"):
            return None
        q = q[len(root_url()) + 1:]
    for e in catalog(**kw):
        if q == e["name"] or q == e["id"]:
            return e
    tail = q.split("/")[-1]
    return next((e for e in catalog(**kw) if e["name"] == tail), None)


def label(e: dict) -> str:
    """下拉里的显示串:`<org>/<名字> · v3.0 · 1693 条`(缺的字段不占位)。"""
    parts = [e.get("id") or e["name"]]
    if e.get("version"):
        parts.append(str(e["version"]))
    if e.get("episodes") is not None:
        parts.append(f"{e['episodes']} 条")
    if e.get("warning"):
        parts.append(f"⚠️ {e['warning']}")
    return " · ".join(parts)


def choices(**kw) -> list[tuple[str, str]]:
    """Dropdown 的 (显示, 值) 对:值 = 目录名(拼 URL 用),显示 = label。"""
    return [(label(e), e["name"]) for e in catalog(**kw)]


def summary_line(count: int | None = None) -> str:
    """说明行:来源 + 数量 + 只读。"""
    c = configured()
    if not c:
        return ""
    n = len(_CACHE["entries"]) if count is None else count
    return (f"公共数据集镜像 {root_url()}(匿名只读)· {n} 个 LeRobot 数据集"
            f"{' · ' + c['region'] if c['region'] else ''}")
