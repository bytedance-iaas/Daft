"""TOS 直连存储层(2026-08-19 云产品化 P0):数据源/输出直接指 tos:// URL。

用户体验目标:跑批的输入输出都可以是 `tos://<桶>/<前缀>` + 地区,桶不再在部署时
绑定到 pod。凭证走部署注入的环境变量(helm 的 secrets.tosAccessKey/tosSecretKey),
用户只提供 URL 与地区 —— 与 rerun viewer 的 OpenTosModal 同一模型:
**凭证来自部署,目标来自用户**。

端点与地区的推导规则(照 rerun `re_data_source/tos/client.rs` 的既定行为,
两边必须一致,深链跳转的用户在两个产品间不该看到两套规则):
  · 用户给的地区 == 部署端点(TOS_ENDPOINT)所在地区 → 沿用部署端点
    (保住 `.ivolces.com` 内网端点:VPC 内更快且不计公网流量);
  · 其他地区 → 合成 `https://tos-s3-<region>.volces.com`(公网端点);
  · 端点主机名里的地区用 `tos-s3-<region>` / `tos-<region>` 标签识别,
    识别不出按未知处理(不硬猜)。

传输策略(MVP,与管道的本地路径假设解耦):
  · 读:stage_in 把 `tos://` 前缀下的对象整体下到**容器本地缓存**,管道照旧
    读本地目录。体积预检沿用 fetch.staging_capacity 的容器限额纪律(超预算
    拒绝并明说,绝不写到 pod 被驱逐);文件级续传按「本地大小 == 远端大小」
    跳过(与 fetch.split_resume 同一判据),meta/info.json 一律重下(2 KB,
    换来"缓存新鲜度以远端为准")。
  · 写:跑批先落容器本地盘,成功后 stage_out 整树上传。**marker 最后传**:
    对象 PUT 是原子的,但"一份交付"是一组对象 —— 判定完整性的标志文件
    (数据集的 meta/info.json、跑批的 passed.json、交付根的 latest)押到
    其余对象全部就位后再传,读者绝不会把半份交付当完整交付列出。
    这与挂载时代"哨兵最后写"是同一条协议,只是载体从 rename 换成了 PUT。

凭证与端点的环境变量名与 Daft 引擎 TosConfig.from_env 完全一致
(TOS_ACCESS_KEY / TOS_SECRET_KEY / TOS_SESSION_TOKEN / TOS_ENDPOINT /
TOS_REGION),后续管道内改用 daft 直读 tos:// 时零迁移。

tos SDK 是懒导入:单测用假客户端注入,不碰网络也不要求装 SDK。
"""
from __future__ import annotations

import os
import re
import tempfile

DEFAULT_REGION = "cn-beijing"

#: 本地缓存根(stage_in 的落地处)。环境变量可换,缺省在系统临时目录下。
CACHE_ENV = "CURATION_TOS_CACHE"

#: 一次 list 的页大小(SDK 上限 1000;分页游标翻到底,不设总数上限)。
_LIST_PAGE = 1000

#: 大文件走分片断点续传的门槛(2026-08-19 实测教训:办公网公网链路上,几百 MB
#: 的 mp4 单流 GET 拖了 996 秒后 Read timed out —— 单流传大文件,链路一抖全部
#: 重来;分片 + checkpoint 后单片失败只重传那一片)。云上内网虽然快到不太会
#: 超时,分片照走:同一条代码路径,两边行为一致。
_MULTIPART_THRESHOLD = 16 * 1024 * 1024
_PART_SIZE = 8 * 1024 * 1024
_PART_TASKS = 4

#: 单文件传输的重试次数(分片层之上的整文件兜底;checkpoint 在,重试不从零来)。
_TRANSFER_ATTEMPTS = 3

#: TOS 桶名规则(火山文档:3-63 位小写字母/数字/中划线,首尾不能是中划线)。
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

#: 端点主机名里的地区标签:tos-s3-<region> 或 tos-<region>。
_HOST_REGION_RE = re.compile(r"^tos(?:-s3)?-([a-z0-9-]+)$")


class TosUrlError(ValueError):
    """tos:// URL 写法不合法(桶名/前缀问题)。"""


class TosConfigError(RuntimeError):
    """凭证/端点配置缺失或不可用(部署问题,不是用户输入问题)。"""


class TosStageError(RuntimeError):
    """传输或校验环节的失败。"""


# ── 纯逻辑:URL / 地区 / 端点(可直接单测)────────────────────────────────


def parse_tos_url(url: str) -> tuple[str, str]:
    """`tos://<桶>/<前缀>` → (桶, 前缀)。前缀可空(= 整桶);尾部斜杠不敏感。

    只认 tos://(s3:// 都不收):本产品对用户承诺的就是 TOS,收别的 scheme
    只会把"连不上"的报错推迟到传输层,不如在门口说清楚。
    """
    s = str(url or "").strip()
    if not s.startswith("tos://"):
        raise TosUrlError(f"不是 tos:// URL:{s!r}(写法:tos://桶名/前缀)")
    rest = s[len("tos://"):]
    bucket, _, prefix = rest.partition("/")
    if not _BUCKET_RE.match(bucket):
        raise TosUrlError(f"桶名不合法:{bucket!r}(3-63 位小写字母/数字/中划线)")
    prefix = prefix.strip("/")
    for seg in prefix.split("/"):
        if seg == "..":
            raise TosUrlError(f"前缀里不允许 '..':{prefix!r}")
    if "\\" in prefix:
        raise TosUrlError(f"前缀里不允许反斜杠:{prefix!r}")
    return bucket, prefix


def region_from_endpoint(endpoint: str) -> str | None:
    """端点 → 地区;识别不出返回 None(不硬猜)。

    识别 `tos-s3-<region>` / `tos-<region>` 打头、volces.com / ivolces.com
    收尾的主机名(内外网端点同规则)。
    """
    s = str(endpoint or "").strip()
    if not s:
        return None
    host = re.sub(r"^[a-z]+://", "", s).split("/", 1)[0].split(":", 1)[0].lower()
    if not (host.endswith(".volces.com") or host.endswith(".ivolces.com")):
        return None
    m = _HOST_REGION_RE.match(host.split(".", 1)[0])
    return m.group(1) if m else None


def endpoint_for_region(region: str, deployment_endpoint: str | None) -> str:
    """地区 → **原生协议**端点 `tos-<region>.(i)volces.com`。

    ⚠️ 不是 rerun 用的 `tos-s3-<region>`:那是 S3 兼容网关,给自签 SigV4 的
    客户端(rerun 手写的 client.rs、object_store)用;本层用的是火山官方 tos
    SDK,走原生协议原生签名,端点必须是 `tos-<region>`。部署端点(TOS_ENDPOINT,
    helm 里与 rerun 共用一个值,是 s3 形式)只取两个事实:地区、内外网 ——
    与用户要的地区相同就保内网(.ivolces.com,VPC 内快且不计公网流量),
    异地区一律公网;主机名总是重新合成,s3/原生的形式差异在这儿归一。
    """
    dep = str(deployment_endpoint or "").strip()
    internal = ".ivolces.com" in dep and region_from_endpoint(dep) == region
    return f"https://tos-{region}.{'ivolces' if internal else 'volces'}.com"


def is_marker(relpath: str) -> bool:
    """这个相对路径是不是「完整性标志」(上传押后 + 永不按大小跳过)。

    三层:数据集哨兵 meta/info.json、跑批 marker passed.json、交付根 latest。
    marker 不参与续传跳过的原因与 fetch.split_resume 拒绝跳过哨兵同源:
    marker 的语义是"它背书的内容全部就位",按大小相等跳过等于让一个旧 marker
    为新内容背书。
    """
    p = str(relpath or "")
    name = p.rsplit("/", 1)[-1]
    return (p == "latest" or p.endswith("/latest")
            or p == "meta/info.json" or p.endswith("/meta/info.json")
            or name == "passed.json")


def upload_plan(relpaths: list[str]) -> list[str]:
    """整树上传的顺序:普通文件 → meta/info.json(数据集哨兵)→ passed.json
    (跑批 marker)→ latest(交付根,最后)。同级内按字典序,顺序确定可复测。

    为什么是这个顺序:数据集清单认 meta/info.json、跑批清单认 passed.json、
    报告页默认打开 latest 记的那次 —— 每一层"完整性标志"都必须在它背书的
    内容全部就位后才出现,否则读者(另一个 pod 的 UI / 用户的下游脚本)会把
    半份交付当完整交付用。
    """
    normal, sentinels, markers, latest = [], [], [], []
    for p in sorted(relpaths):
        name = p.rsplit("/", 1)[-1]
        if p == "latest" or p.endswith("/latest"):
            latest.append(p)
        elif p == "meta/info.json" or p.endswith("/meta/info.json"):
            sentinels.append(p)
        elif name == "passed.json":
            markers.append(p)
        else:
            normal.append(p)
    return normal + sentinels + markers + latest


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.2f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{n} B"


# ── 客户端封装(SDK 懒导入;假客户端注入即可离线单测)──────────────────────


class TosStore:
    """薄封装:分页 list / 下载 / 上传。方法名与用途一一对应,不藏语义。"""

    def __init__(self, endpoint: str, region: str, ak: str = "", sk: str = "",
                 security_token: str | None = None, client=None):
        self.endpoint = endpoint
        self.region = region
        if client is None:
            import tos  # 懒导入:离线单测注入假客户端,不碰这里
            client = tos.TosClientV2(ak, sk, endpoint, region,
                                     security_token=security_token)
        self._c = client

    def iter_objects(self, bucket: str, prefix: str):
        """前缀下全部对象 → (key, size) 迭代;游标翻页翻到底。"""
        token = None
        while True:
            out = self._c.list_objects_type2(
                bucket, prefix=prefix, continuation_token=token,
                max_keys=_LIST_PAGE)
            for obj in out.contents or []:
                yield obj.key, int(obj.size)
            if not getattr(out, "is_truncated", False):
                return
            token = out.next_continuation_token

    def download(self, bucket: str, key: str, local_path: str,
                 size: int | None = None) -> None:
        """下载一个对象。大文件(≥门槛)走 SDK 的分片并发 + checkpoint 断点续传;
        checkpoint 文件放独立目录(_ckpt_path),不污染数据目录。"""
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        if (size or 0) >= _MULTIPART_THRESHOLD and hasattr(self._c, "download_file"):
            self._c.download_file(bucket, key, local_path,
                                  part_size=_PART_SIZE, task_num=_PART_TASKS,
                                  enable_checkpoint=True,
                                  checkpoint_file=_ckpt_path("dl", bucket, key))
        else:
            self._c.get_object_to_file(bucket, key, local_path)

    def upload(self, local_path: str, bucket: str, key: str) -> None:
        """上传一个文件。大文件同样分片断点续传;checkpoint **必须**指到独立
        目录 —— 缺省会落在源文件旁边,也就是交付树里,重试跑的 stage_out
        会把它当交付件传上去。"""
        try:
            size = os.path.getsize(local_path)
        except OSError:
            size = 0
        if size >= _MULTIPART_THRESHOLD and hasattr(self._c, "upload_file"):
            self._c.upload_file(bucket, key, local_path,
                                part_size=_PART_SIZE, task_num=_PART_TASKS,
                                enable_checkpoint=True,
                                checkpoint_file=_ckpt_path("ul", bucket, key))
        else:
            self._c.put_object_from_file(bucket, key, local_path)


def make_store(region: str | None = None, client=None) -> TosStore:
    """按「用户地区 + 部署凭证/端点」组一个客户端。

    凭证只认环境变量(TOS_ACCESS_KEY / TOS_SECRET_KEY,可选 TOS_SESSION_TOKEN),
    与 Daft TosConfig.from_env 同名 —— helm 把 secrets.tosAccessKey/tosSecretKey
    以这组名字注入即可,两边(本层 / daft 引擎)共用一份配置。
    缺凭证是**部署问题**,报错点名环境变量与 helm 值,不让用户猜。
    """
    ak = os.environ.get("TOS_ACCESS_KEY", "").strip()
    sk = os.environ.get("TOS_SECRET_KEY", "").strip()
    if client is None and (not ak or not sk):
        raise TosConfigError(
            "缺少 TOS 凭证:环境变量 TOS_ACCESS_KEY / TOS_SECRET_KEY 未注入。"
            "云上部署由 helm 的 secrets.tosAccessKey / tosSecretKey 提供;"
            "本地调试请 export 这两个变量")
    dep_endpoint = os.environ.get("TOS_ENDPOINT", "").strip()
    dep_region = (os.environ.get("TOS_REGION", "").strip()
                  or region_from_endpoint(dep_endpoint) or DEFAULT_REGION)
    want = str(region or "").strip() or dep_region
    endpoint = endpoint_for_region(want, dep_endpoint)
    return TosStore(endpoint, want, ak, sk,
                    security_token=os.environ.get("TOS_SESSION_TOKEN") or None,
                    client=client)


# ── stage in / out ───────────────────────────────────────────────────────


def cache_root() -> str:
    return (os.environ.get(CACHE_ENV, "").strip()
            or os.path.join(tempfile.gettempdir(), "curation-tos-cache"))


def _ckpt_path(kind: str, bucket: str, key: str) -> str:
    """断点续传 checkpoint 文件的存放路径(缓存根下的独立目录,按对象定名)。"""
    import hashlib
    h = hashlib.sha256(f"{kind}:{bucket}:{key}".encode()).hexdigest()[:24]
    d = os.path.join(cache_root(), "ckpt")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{h}.json")


def _with_retry(what: str, rel: str, fn) -> None:
    """整文件级重试(分片层之上的兜底)。checkpoint 在,重试不从零来。
    最后一次仍失败按原样抛给调用方转成 TosStageError。"""
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            fn()
            return
        except Exception as e:  # noqa: BLE001 —— SDK/网络异常族杂,重试面前一视同仁
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            print(f"[tos] {what}失败(第 {attempt}/{_TRANSFER_ATTEMPTS} 次)"
                  f" {rel}:{type(e).__name__}: {str(e)[:120]} —— 重试"
                  "(断点续传,已传部分不重来)", flush=True)


def _list_prefix(store: TosStore, bucket: str, prefix: str) -> dict[str, int]:
    """前缀下的对象清单 → {相对路径: 大小}。

    前缀按「目录」语义补尾斜杠再 list:不补的话 `datasets/foo` 会把
    `datasets/foobar/…` 一起捞进来(对象存储的前缀匹配是字符串级的)。
    """
    p = prefix + "/" if prefix else ""
    out: dict[str, int] = {}
    for key, size in store.iter_objects(bucket, p):
        rel = key[len(p):]
        if not rel or rel.endswith("/"):        # 「目录占位对象」跳过
            continue
        out[rel] = size
    return out


def stage_in(url: str, region: str | None = None, *, store: TosStore | None = None,
             budget_bytes: int | None = None) -> str:
    """`tos://桶/前缀` 的数据集 → 本地缓存目录(下完/续传完返回本地路径)。

    体积预检:总量必须装得进本地缓存可写预算(缺省 fetch.staging_capacity 的
    容器限额口径)—— 装不下就拒绝并明说差多少。这是 MVP 的**已知限制**:
    数据集必须整体落得进本地盘;按需分批/直读是后续版本的事,先把口径说清,
    不静默把 pod 写死。
    """
    bucket, prefix = parse_tos_url(url)
    store = store or make_store(region)
    sizes = _list_prefix(store, bucket, prefix)
    if not sizes:
        raise TosStageError(f"{url} 下没有对象:桶/前缀写错,或该地区"
                            f"({store.region})下没有这个桶")
    # "in/" 命名空间与 CLI 的输出暂存 "out/" 分开:桶名恰好叫 out 也不撞路径
    dest = os.path.join(cache_root(), "in", bucket, prefix or "_root")
    os.makedirs(dest, exist_ok=True)

    need = sum(sizes.values())
    if budget_bytes is None:
        from .fetch import staging_capacity
        budget_bytes, basis = staging_capacity(cache_root())
    else:
        basis = "调用方指定"
    # 已在本地、大小一致的部分不再占预算(续传的意义就在这)
    pending = {}
    for rel, size in sizes.items():
        local = os.path.join(dest, rel)
        if rel != "meta/info.json":            # 哨兵一律重下,缓存新鲜度以远端为准
            try:
                if os.path.getsize(local) == size:
                    continue
            except OSError:
                pass
        pending[rel] = size
    todo = sum(pending.values())
    if todo > budget_bytes:
        raise TosStageError(
            f"数据集体积超出本地缓存可写预算:还需下载 {_fmt_bytes(todo)},"
            f"可用 {_fmt_bytes(budget_bytes)}(依据:{basis})。"
            f"MVP 限制:tos:// 数据源须整体装进本地缓存;请换更小的数据集,"
            f"或加大容器 ephemeral-storage 限额")

    skipped = len(sizes) - len(pending)
    if skipped:
        print(f"[tos] 本地缓存已有 {skipped} 个文件(大小与远端一致),跳过重下",
              flush=True)
    print(f"[tos] 下载 tos://{bucket}/{prefix} → {dest}"
          f"({len(pending)} 个文件,{_fmt_bytes(todo)},地区 {store.region},"
          f"端点 {store.endpoint})", flush=True)
    p = prefix + "/" if prefix else ""
    for i, rel in enumerate(sorted(pending), 1):
        try:
            _with_retry("下载", rel, lambda: store.download(
                bucket, p + rel, os.path.join(dest, rel), size=pending[rel]))
        except Exception as e:  # noqa: BLE001 —— SDK 异常族杂,统一转成本层错误
            raise TosStageError(f"下载失败 {p + rel}:{type(e).__name__}: "
                                f"{str(e)[:200]}") from e
        got = os.path.getsize(os.path.join(dest, rel))
        if got != pending[rel]:
            raise TosStageError(f"下载后大小不符 {rel}:本地 {got} B ≠ "
                                f"远端 {pending[rel]} B")
        if i % 50 == 0 or i == len(pending):
            print(f"[tos] 已下 {i}/{len(pending)}", flush=True)
    return dest


def stage_out(local_root: str, url: str, region: str | None = None, *,
              store: TosStore | None = None) -> int:
    """本地产出树 → `tos://桶/前缀`(marker 最后传,顺序见 upload_plan)。

    上传中途失败:已传上去的都是普通产物,三层完整性标志一个都还没出现,
    远端不会把半份交付当完整交付列出 —— 失败就报错让上层重跑,已传部分
    下次按「远端大小 == 本地大小」跳过(marker 永不跳过,理由见 is_marker)。
    """
    bucket, prefix = parse_tos_url(url)
    store = store or make_store(region)
    root = os.path.abspath(local_root)
    rels = []
    for cur, _dirs, names in os.walk(root):
        for n in names:
            if n.startswith(".curation-"):     # safe_write 的发布残留,不进交付
                continue
            rels.append(os.path.relpath(os.path.join(cur, n), root)
                        .replace(os.sep, "/"))
    if not rels:
        raise TosStageError(f"本地产出目录是空的:{root},没有东西可上传")
    remote = _list_prefix(store, bucket, prefix)   # 续传对账(空桶/首传 = 空表)
    plan, skipped = [], 0
    for rel in upload_plan(rels):
        if not is_marker(rel):
            try:
                if remote.get(rel) == os.path.getsize(os.path.join(root, rel)):
                    skipped += 1
                    continue
            except OSError:
                pass
        plan.append(rel)
    p = prefix + "/" if prefix else ""
    if skipped:
        print(f"[tos] 远端已有 {skipped} 个文件(大小与本地一致),跳过重传",
              flush=True)
    print(f"[tos] 上传 {root} → tos://{bucket}/{prefix}"
          f"({len(plan)} 个文件,地区 {store.region},端点 {store.endpoint};"
          "完整性标志最后传)", flush=True)
    for i, rel in enumerate(plan, 1):
        try:
            _with_retry("上传", rel, lambda: store.upload(
                os.path.join(root, rel), bucket, p + rel))
        except Exception as e:  # noqa: BLE001
            raise TosStageError(f"上传失败 {p + rel}:{type(e).__name__}: "
                                f"{str(e)[:200]};完整性标志尚未上传,远端不会"
                                "把这份半成品当完整交付列出,重跑即可续传") from e
        if i % 50 == 0 or i == len(plan):
            print(f"[tos] 已传 {i}/{len(plan)}", flush=True)
    return len(plan)
