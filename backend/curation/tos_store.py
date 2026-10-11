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

#: 匿名可读的桶(2026-08-21 公共数据集):注册进来的桶一律用**不签名**的客户端。
#: 真机事实(生产 pod 实测):公共镜像桶对匿名请求开放读,而用本实例密钥**签名**
#: 的请求反而 AccessDenied —— 跨账号的签名请求还要过密钥所属账号的 IAM,匿名
#: 只看桶策略。所以"有密钥"不等于"能读公共桶",得按桶挑客户端。
#: 值 = 桶所在地区(None = 跟部署/调用方给的地区走)。
_ANON_BUCKETS: dict = {}


def register_anonymous_bucket(bucket: str, region: str | None = None) -> None:
    """登记一个匿名可读的桶(站点配置 public_datasets 用)。桶名不合法直接抛。"""
    b = str(bucket or "").strip()
    if not _BUCKET_RE.match(b):
        raise TosUrlError(f"桶名不合法:{b!r}(3-63 位小写字母/数字/中划线)")
    _ANON_BUCKETS[b] = str(region or "").strip() or None


def is_anonymous_bucket(bucket: str) -> bool:
    return str(bucket or "").strip() in _ANON_BUCKETS


def anonymous_region(bucket: str) -> str | None:
    """登记时给的地区(没给返回 None)。"""
    return _ANON_BUCKETS.get(str(bucket or "").strip())


def clear_anonymous_buckets() -> None:
    _ANON_BUCKETS.clear()


#: 火山 TOS 的地区清单(与控制台建桶页/rerun viewer 同值同序;界面下拉允许
#: 自由输入,清单落后于新地区时不挡路)。locate_bucket 自愈按它扫。
REGION_CANDIDATES = ("cn-beijing", "ap-southeast-1", "ap-southeast-3",
                     "cn-guangzhou", "cn-hongkong", "cn-shanghai")

#: 桶 → 所在地区。签名端(dsfs 全局单例)离"人说了地区"的参数最远,拿默认地区
#: 给异地桶签名 = URL 指向不存在的坐标(2026-08-28 用户实见:上海交付桶,
#: 轨迹页视频全黑)。⚠️ 只登记**被事实验证过**的组合(操作成功之后 / origin
#: 文件 / locate_bucket 找到):曾经"见到参数就登记",用户在界面把地区切到
#: 上海的一瞬间就把 (北京桶, 上海) 毒进了表,源数据集全体 404(dataverse 实见)。
_BUCKET_REGIONS: dict = {}


def register_bucket_region(bucket: str, region: str | None) -> None:
    """登记一个桶所在的地区(空地区 = 不登记)。调用方限"验证过"的场合,见上。"""
    b, r = str(bucket or "").strip(), str(region or "").strip()
    if b and r:
        _BUCKET_REGIONS[b] = r


def bucket_region(bucket: str) -> str | None:
    return _BUCKET_REGIONS.get(str(bucket or "").strip())


def clear_bucket_regions() -> None:
    _BUCKET_REGIONS.clear()


class TosUrlError(ValueError):
    """tos:// URL 写法不合法(桶名/前缀问题)。"""


class TosConfigError(RuntimeError):
    """凭证/端点配置缺失或不可用(部署问题,不是用户输入问题)。"""


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
                 security_token: str | None = None, client=None,
                 anonymous: bool = False, browser_client=None):
        self.endpoint = endpoint
        self.region = region
        #: 匿名客户端(公共桶):不签名;presign 退化成裸的公网 URL
        self.anonymous = bool(anonymous)
        if client is None:
            import tos  # 懒导入:离线单测注入假客户端,不碰这里
            client = tos.TosClientV2(ak, sk, endpoint, region,
                                     security_token=security_token)
        self._c = client
        # 浏览器面第二客户端(懒建):端点是内网时,给浏览器的预签名必须换
        # 公网端点重签(主机名签进签名里,不能事后改域名)。凭证留在实例上,
        # 只为这一个用途;测试注入 browser_client 即可离线验。
        self._creds = (ak, sk, security_token)
        self._cb = browser_client

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

    def iter_object_meta(self, bucket: str, prefix: str):
        """前缀下全部对象 → (key, size, etag)。ingest.dsfs 的清单预取用(etag 当
        内容身份,省掉为算哈希把整段视频拉回来)。"""
        token = None
        while True:
            out = self._c.list_objects_type2(
                bucket, prefix=prefix, continuation_token=token,
                max_keys=_LIST_PAGE)
            for obj in out.contents or []:
                yield obj.key, int(obj.size), str(getattr(obj, "etag", "") or "")
            if not getattr(out, "is_truncated", False):
                return
            token = out.next_continuation_token

    def get_bytes(self, bucket: str, key: str) -> bytes:
        """整对象取回内存(KB~几百 MB 的 meta/parquet;视频不走这里)。"""
        out = self._c.get_object(bucket, key)
        return out.read()

    @staticmethod
    def _presign_get(client, bucket: str, key: str, expires: int) -> str:
        """在给定客户端上做 GET 预签名;离线单测的假客户端不看 method 参数。"""
        try:
            import tos
            method = tos.HttpMethodType.Http_Method_Get
        except ImportError:
            method = "GET"
        return client.pre_signed_url(method, bucket, key,
                                     expires=expires).signed_url

    def presign(self, bucket: str, key: str, expires: int = 3600) -> str:
        """GET 预签名 URL(本地 HMAC,不出网):PyAV 直接按 Range 读视频。
        匿名客户端没有密钥可签,给裸的公网 URL(公共桶本来就匿名可读)。"""
        if self.anonymous:
            return self.public_url(bucket, key)
        return self._presign_get(self._c, bucket, key, expires)

    def public_url(self, bucket: str, key: str, endpoint: str | None = None) -> str:
        """`https://<桶>.<端点主机>/<key>`(虚拟主机风格,key 做 URL 编码)。"""
        from urllib.parse import quote
        ep = str(endpoint or self.endpoint or "")
        m = re.match(r"^([a-z]+)://", ep)
        scheme = m.group(1) if m else "https"
        host = re.sub(r"^[a-z]+://", "", ep).split("/", 1)[0]
        return f"{scheme}://{bucket}.{host}/{quote(str(key).lstrip('/'))}"

    def browser_endpoint(self) -> str:
        """给浏览器用的**公网**端点(原生协议)。与 self.endpoint 的差别只在
        内外网:`*.ivolces.com` 只在火山 VPC 内可达,发给浏览器就是死链。"""
        return f"https://tos-{self.region}.volces.com"

    def browser_url(self, bucket: str, key: str, expires: int = 3600) -> str:
        """给**浏览器**的媒体 URL:一律公网端点。

        pod 自己读桶保内网端点(快、不计公网流量),但预签名把主机名签进
        签名里,不能签完换域名 —— 端点是内网时必须用公网端点客户端**重签**
        (2026-08-28 dataverse 实见:内网预签名发给浏览器,视频永远转圈)。
        匿名桶不签名,直接给公网裸 URL。
        """
        if self.anonymous:
            return self.public_url(bucket, key, endpoint=self.browser_endpoint())
        if ".ivolces.com" not in str(self.endpoint or ""):
            return self.presign(bucket, key, expires)  # 本来就是公网端点
        if self._cb is None:
            import tos
            ak, sk, token = self._creds
            self._cb = tos.TosClientV2(ak, sk, self.browser_endpoint(),
                                       self.region, security_token=token)
        return self._presign_get(self._cb, bucket, key, expires)

    def head(self, bucket: str, key: str) -> tuple[int, str]:
        """单对象的 (大小, etag);不存在让 SDK 的异常原样出来。"""
        out = self._c.head_object(bucket, key)
        return (int(getattr(out, "content_length", 0) or 0),
                str(getattr(out, "etag", "") or "").strip('"'))

    def list_dir(self, bucket: str, prefix: str):
        """前缀下的**一层**内容 → (子目录名列表, [(文件名, 大小)])。

        delimiter="/" 一次列一层(翻页翻到底),绝不递归枚举 —— 数据集根下
        可能有几十万个对象。`curation ls` 用(2026-08-26):直连形态数据集
        不落盘,终端里看源桶 / 交付桶就靠它。"""
        p = prefix.strip("/") + "/" if prefix.strip("/") else ""
        dirs: list = []
        files: list = []
        token = None
        while True:
            out = self._c.list_objects_type2(bucket, prefix=p, delimiter="/",
                                             continuation_token=token,
                                             max_keys=_LIST_PAGE)
            for cp in getattr(out, "common_prefixes", None) or []:
                name = str(getattr(cp, "prefix", ""))[len(p):].strip("/")
                if name:
                    dirs.append(name)
            for obj in out.contents or []:
                name = obj.key[len(p):]
                if name:
                    files.append((name, int(obj.size)))
            if not getattr(out, "is_truncated", False):
                return dirs, files
            token = out.next_continuation_token

    def iter_common_prefixes(self, bucket: str, prefix: str):
        """前缀下**第一层**「子目录」名(delimiter="/" 的 common prefix)。

        给「数据集」下拉列清单用(2026-08-20 融合时补):一次 list 调用拿一层
        名字,绝不枚举前缀下全部对象 —— 一个数据集根下可能有几十万个对象,
        iter_objects 在这儿会把一次下拉变成一场遍历。
        """
        p = prefix.strip("/") + "/" if prefix.strip("/") else ""
        token = None
        while True:
            out = self._c.list_objects_type2(bucket, prefix=p, delimiter="/",
                                             continuation_token=token,
                                             max_keys=_LIST_PAGE)
            for cp in getattr(out, "common_prefixes", None) or []:
                name = str(getattr(cp, "prefix", ""))[len(p):].strip("/")
                if name:
                    yield name
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

    # ── 可写探针用的三个最小动作(2026-08-21):列一下 / 放一个空对象 / 删掉 ──
    def head_prefix(self, bucket: str, prefix: str) -> None:
        """前缀能不能列(桶在不在、有没有读权限);不行就让 SDK 的异常原样出来。"""
        self._c.list_objects_type2(bucket, prefix=prefix, max_keys=1)

    def put_empty(self, bucket: str, key: str) -> None:
        self._c.put_object(bucket, key, content=b"")

    def delete(self, bucket: str, key: str) -> None:
        self._c.delete_object(bucket, key)


def make_store(region: str | None = None, client=None, *,
               anonymous: bool = False) -> TosStore:
    """按「用户地区 + 部署凭证/端点」组一个客户端。

    anonymous=True(公共桶,见 register_anonymous_bucket):不要凭证、不签名;
    地区仍按下面的规则推(调用方一般传登记时的地区)。

    凭证只认环境变量(TOS_ACCESS_KEY / TOS_SECRET_KEY,可选 TOS_SESSION_TOKEN),
    与 Daft TosConfig.from_env 同名 —— helm 把 secrets.tosAccessKey/tosSecretKey
    以这组名字注入即可,两边(本层 / daft 引擎)共用一份配置。
    缺凭证是**部署问题**,报错点名环境变量与 helm 值,不让用户猜。
    """
    ak = "" if anonymous else os.environ.get("TOS_ACCESS_KEY", "").strip()
    sk = "" if anonymous else os.environ.get("TOS_SECRET_KEY", "").strip()
    if client is None and not anonymous and (not ak or not sk):
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
                    security_token=(None if anonymous
                                    else os.environ.get("TOS_SESSION_TOKEN") or None),
                    client=client, anonymous=anonymous)


def make_store_for(bucket: str, region: str | None = None, client=None) -> TosStore:
    """按桶挑客户端:登记为匿名的桶 → 不签名(地区优先用登记值);其余 → 部署凭证。
    所有"已知桶名"的调用点都该走这里,别直接 make_store —— 公共桶用签名客户端
    会被 AccessDenied,报错还长得像"密钥没权限",极其误导。

    地区:调用方明确给了就用;没给按桶查登记表 —— dsfs 这类离参数最远的签名端
    全靠它拿对异地桶的地区。**这里不登记**:参数只是"调用方以为",登记要等
    操作成功(mirror/prefetch/resolve 收尾)或 locate_bucket 实探。"""
    if is_anonymous_bucket(bucket):
        return make_store(anonymous_region(bucket) or region, client,
                          anonymous=True)
    return make_store(region or bucket_region(bucket), client)


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


# ── stage in / out ───────────────────────────────────────────────────────


# ── 可写探针(2026-08-21 用户定:只读桶填进交付目录要"瞬间"知道)──────────────
#: 探针对象名。TOS 没有"问一下能不能写"的接口,权限接口查出来的与实际能不能写常对
#: 不上 —— 唯一可靠的判据是真写一下:放一个 0 字节对象,成功立刻删掉。
PROBE_NAME = ".curation-write-probe"


def _http_status(e: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    return None


def _err_text(e: BaseException) -> str:
    """给人看的一句错误:优先 SDK 的错误码 + message(NoSuchBucket: The specified
    bucket does not exist.),没有才退到异常类名 + str(e);request_id 之类不进界面。"""
    code = getattr(e, "code", None) or type(e).__name__
    msg = getattr(e, "message", None)
    if not isinstance(msg, str) or not msg.strip():
        msg = str(e)
    return f"{code}: {msg.strip()[:100]}"


def probe_writable(url: str, region: str | None = None, *,
                   store: TosStore | None = None) -> dict:
    """`tos://桶/前缀` 能不能写 → {"kind", "detail"}。kind 六态:
    ok(能写能删)/ leftover(能写不能删:留了个 0 字节探针,要说出来)/
    missing(桶不存在或不在这个地区)/ forbidden(连列都不让:密钥没这个桶的权限)/
    readonly(能列不能写)/ error(别的错,detail 里是原话)。
    只读桶**不是错误**:读报告合法,只是不能当交付目录 —— 分类交给调用方措辞。
    """
    bucket, prefix = parse_tos_url(url)
    st = store or make_store_for(bucket, region)
    p = prefix.strip("/")
    key = (p + "/" if p else "") + PROBE_NAME
    try:
        st.head_prefix(bucket, p + "/" if p else "")
    except Exception as e:  # noqa: BLE001 SDK 异常族杂,按状态码分
        kind, detail = classify_list_error(e)
        return {"kind": kind, "detail": detail}
    try:
        st.put_empty(bucket, key)
    except Exception as e:  # noqa: BLE001
        if _http_status(e) == 403:
            return {"kind": "readonly", "detail": _err_text(e)}
        return {"kind": "error", "detail": _err_text(e)}
    try:
        st.delete(bucket, key)
    except Exception as e:  # noqa: BLE001
        return {"kind": "leftover", "detail": _err_text(e)}
    return {"kind": "ok", "detail": ""}


def classify_list_error(e: BaseException) -> tuple[str, str]:
    """list 失败的异常 → (kind, detail):missing(404/301:桶不存在或不在这个地区 ——
    ⚠️ 真机事实:地区填错时 TOS 也答 404 NoSuchBucket,与桶名写错**无法区分**,措辞必须
    两种可能都点到)/ forbidden(403)/ error(其余)。"""
    code = _http_status(e)
    if code in (404, 301):
        return "missing", _err_text(e)
    if code == 403:
        return "forbidden", _err_text(e)
    return "error", _err_text(e)


def locate_bucket(bucket: str, candidates, *, skip: str | None = None,
                  make=None) -> str | None:
    """404 之后到别的地区找这个桶:逐个地区 list 一次(内网几十毫秒一次),找到就回地区名,
    都没有回 None。给界面把"桶不存在或地区不对"说成"桶在 cn-shanghai,把地区改过来"用。
    make(bucket, region) 注入供单测;匿名桶绕开登记地区,否则永远探的是同一个端点。"""
    for rg in candidates:
        if not rg or rg == skip:
            continue
        try:
            if make is not None:
                st = make(bucket, rg)
            elif is_anonymous_bucket(bucket):
                st = make_store(rg, anonymous=True)
            else:
                st = make_store(rg)
            st.head_prefix(bucket, "")
            return rg
        except Exception as e:  # noqa: BLE001 不在这个地区就看下一个
            if _http_status(e) == 403:
                return rg          # 403 = 桶在这儿,只是没权限 —— 地区这件事已经答了
            continue
    return None


