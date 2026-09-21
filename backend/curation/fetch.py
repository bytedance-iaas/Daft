"""`curation fetch`:从内网镜像站把公开数据集拉进本站的数据集根,下完即可直接跑质检。

流程(六步纪律,照 RLinf download_assets.sh 的严谨度):
  ① 哨兵判存在:目标 `<into>/<name>/meta/info.json` 已在 → 跳过(除非 --overwrite);
  ② 本地暂存目录 + 兜底清理(正常结束清;**失败时保留**并告知路径);
  ③ 体积预检:真实体积逐文件 HEAD 实测(见下),只有**单个文件**比暂存可写预算还大
     才拒绝 —— 总体积可以大于本地盘,分批下载就是为了这个;
  ④ 文件级续传:目标目录里已有、且大小与源站 HEAD 一致的文件跳过,只下缺的;
  ⑤ 剩余文件按体积打包成批(预算 = min(8 GiB, 可写/3),≤50 文件/批),
     **下载与入库重叠**:主线程逐批下载+逐批校验,唯一的拷贝线程逐批拷进目标后
     **立删本地**(双缓冲,本地盘峰值 ≈ 2 批,数据集多大都不怕);
  ⑥ 全部批完成后整体校验(info.json 可解析、声明 dtype video 就必须真有非空视频),
     meta/info.json(哨兵)**最后写**,拷完回验在位。
     ⚠️ 仅完整拉取走这一步:--include 部分拉取**一律不发布哨兵** —— 哨兵的含义是
     "这是一份完整可用的数据集",部分拉取按定义不完整,发哨兵就是撒谎(meta 声明
     的 episode 大多不在盘上,被清单列出后质检会读到不存在的数据;2026-08-18 事故
     的因果链根子就在这儿)。

为什么下载与入库要重叠(2026-08-18 oniond-p0 实测;谁觉得"重叠没必要"想拆回串行,
先看这组数):下载 4.0 GB 23 秒(167 MB/s);本地→TOS 拷同一批 17 秒(223 MB/s);
**两者并发时拷贝仍是 17 秒,一点没被拖慢,两件事全完 24 秒**(串行约 40 秒)。
两段吃的是不同资源(下载=外网→本地盘写;拷贝=本地盘读→FSX 写,CPU 基本闲着),
重叠省约 40% 且互不干扰。拷贝**只开一个线程**(顺序写):FSX 并发写没有把握,
单线程已实测跑满 223 MB/s。

为什么体积必须自己逐文件 HEAD 量(2026-08-18 P0 实测,别回退成读 inspect 报告值):
镜像站 inspect 报的 Used Storage 完全不可信 —— libero 报 65 GiB / 真实 32.53 GiB
(高报一倍);aloha_sim_transfer_cube_human 报 2.90 GB / 真实 2.4 MB(高报 1200 倍)。

为什么必须"本地暂存 → 校验 → 顺序拷入",绝不让下载器直指 /mnt/tos(2026-08-05 实锤):
FSX 挂载撞上 aria2 的多连接随机写会产出**静默残缺**的文件;顺序整份拷贝是这张挂载
唯一安全的写法(safe_write 模块同一条纪律,入库走它的原子发布通道)。

暂存位置:环境变量 CURATION_FETCH_STAGING 指定的目录,缺省系统临时目录。
暂存可写预算见 staging_capacity(容器存储限额从 CURATION_EPHEMERAL_LIMIT_BYTES 读,
deploy yaml 用 downward API 注入)。批预算只有测试注入用的环境变量
CURATION_FETCH_BATCH_BUDGET(字节)可改,不设 CLI 旋钮 —— 用户不该为了下个
数据集调参数。

退出码:0 = 成功/幂等跳过;2 = 输入或状态问题(未知来源/别人在下/单文件装不下/
根目录不存在);1 = 下载或校验或入库失败(失败时暂存保留供排查,细节见 run_fetch
的失败现场保全注释)。
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .export.safe_write import content_state, publish_file, write_json

#: 出厂默认的数据来源(配置没写 fetch_sources 段时用)。
#: ⚠️ 与 pipeline/default.yaml 里的示例注释保持同值(改一处必改另一处)。
#: base_url = 逐文件 HEAD 量真实体积的对象存储前缀(2026-08-18 P0 实测通的那个);
#: 端点将来会变、curation/ 要推公开仓,所以它必须可配,不许在业务逻辑里再写一份。
FACTORY_FETCH_SOURCES = {
    "ai-infra": {
        "display_name": "内网 HF 镜像缓存桶",
        "bucket": "ai-infra",
        "base_url": "https://ai-infra.tos-cn-beijing.volces.com/dataset",
    },
}

#: 跨 pod 进行中标记的陈旧线(小时)。同区内网实测吞吐很高(64GB 权重 25s、
#: so101 数据集分钟级),几百 GB 的大集也在 1-2 小时内下完 —— 取 12h ≈ 最长预期
#: 下载的 6 倍富余;一次 pod 崩溃留下的残留标记最多堵 12 小时就能被接管,
#: 不至于永久堵死。
STALE_MARKER_HOURS = 12

#: 逐文件 HEAD 的并发度(P0 实测:16 并发下 384 个文件几十秒,够快)。
HEAD_CONCURRENCY = 16

GIB = 1024 ** 3

#: 哨兵的相对路径。list_datasets 判"有效数据集"只看它,所以它在 fetch 全流程里是
#: 特殊公民:押后暂存、整体校验过了**最后**发布、且**永不参与文件级续传**
#: (2026-08-18 libero 实机事故,见 split_resume 的注释)。
SENTINEL_RELPATH = "meta/info.json"

#: 单批体积预算上限。取 8 GiB:libero(真实 32.5 GiB)分 5 批、双缓冲峰值 16 GiB,
#: 现有容器 30Gi ephemeral-storage 配额装得下且余量充足。
BATCH_BUDGET_CAP = 8 * GIB

#: 单批文件数上限。oniond 一次带 50 个精确路径 --include 实测可用(2026-08-18
#: oniond-p0,50 个文件 4.0 GB 全下);更大的量级(命令行长度、jq 传参)没验过,
#: 不无脑放大 —— 超了就再切一批,多一批只多几秒开销。
BATCH_MAX_FILES = 50

#: 容器 ephemeral-storage 限额是按**容器总用量**执行的(日志/缓存/别的临时文件
#: 都算),我们从容器里量不全 —— 留 2 GiB 安全余量兜量不到的部分,宁可少用,
#: 也绝不把 pod 写到被 kubelet 驱逐。
EPHEMERAL_SAFETY_MARGIN = 2 * GIB

#: 没注入限额时的保守缺省(理由见 staging_capacity:绝不静默退回 df)。
DEFAULT_EPHEMERAL_LIMIT = 10 * GIB


class FetchError(RuntimeError):
    """下载/校验/入库环节的失败(区别于输入错误)。"""


# ── 可替换的接缝(测试用假件覆盖;生产环境才真调下载器/发 HTTP)──────────


def _oniond_inspect_text(source: dict, ref: str) -> str:
    """镜像站数据集清单的原始文本(`inspect dataset <ref>`)。"""
    env = dict(os.environ, BUCKET=str(source.get("bucket") or ""))
    proc = subprocess.run(["oniond", "inspect", "dataset", ref],
                          env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise FetchError(f"源站清单获取失败(退出码 {proc.returncode}):{tail}")
    return proc.stdout


def parse_inspect_files(text: str) -> list[str]:
    """inspect 输出 → 文件相对路径清单。

    真实格式(2026-08-18 在 bookworm pod 上实拍):`Files:` 行之后是一行纯虚线
    分隔,再往下每行一个裸路径,直到输出结束。这里对"表头"按内容识别(纯虚线/
    空行跳过)而不是数行数,两种写法都兼容。
    ⚠️ `Used Storage(in Bytes):` 那行**故意不读**:它就是那个高报 1200 倍的数,
    体积只认逐文件 HEAD。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Files:"):
            start = i + 1
            break
    if start is None:
        raise FetchError("源站清单输出里没有 Files: 段,格式变了?原文开头:"
                         + text[:200])
    out = []
    for line in lines[start:]:
        s = line.strip()
        if not s or set(s) <= {"-"}:
            continue
        out.append(s)
    if not out:
        raise FetchError("源站清单 Files: 段是空的 —— 数据集不存在或名字写错")
    return out


def list_remote_files(source: dict, ref: str) -> list[str]:
    return parse_inspect_files(_oniond_inspect_text(source, ref))


def remote_size(url: str) -> int:
    """单文件 HEAD 取 content-length。取不到就抛错(诚实弃权,不猜 0 也不猜大)。"""
    import urllib.request
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as resp:
        cl = resp.headers.get("Content-Length")
    if cl is None:
        raise FetchError(f"源站没报 content-length:{url}")
    return int(cl)


def measure_sizes(source: dict, ref: str, files: list[str]) -> dict[str, int]:
    """逐文件 HEAD 量真实体积(这是唯一可信的体积来源,理由见模块头)。"""
    base = str(source["base_url"]).rstrip("/")
    sizes: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=HEAD_CONCURRENCY) as ex:
        futs = {ex.submit(remote_size, f"{base}/{ref}/{f}"): f for f in files}
        for fut in as_completed(futs):
            sizes[futs[fut]] = fut.result()
    return sizes


def download_dataset(source: dict, ref: str, staging: str,
                     includes: list[str] | None) -> None:
    """下载到暂存目录(产物落在 `<staging>/<ref>/` 下,下载器的固定布局)。

    - --include **每个模式各带一次**(2026-08-18 oniond-p0 实测:一个 --include 后
      跟一串只认第一个);模式里**不许**转义点号 —— 模式经 jq 传递时反斜杠被吃掉,
      `file-000\\.parquet` 匹配不到任何文件,精确路径原样传就对了;
    - BUCKET 环境变量 = 配置里的桶名(下载器以此定位镜像桶);
    - SKIP_DISK_CHECK=true:下载器**自带的**磁盘预检用的正是 inspect 那个高报
      1200 倍的体积数(读它的脚本源码核实),我们已经用 HEAD 实测体积做过预检,
      让它再按假数拦一次只会把装得下的下载误拒掉;
    - cwd=staging:下载器会在**进程当前目录**留一份续传清单文件,指到暂存里
      让它跟暂存一起被清理,不污染调用方的工作目录;
    - 输出直通终端:下载是最慢的一步,进度必须看得见(静默慢步骤=bug)。
    """
    cmd = ["oniond", "download", "dataset", ref, "--dir", staging]
    for pat in includes or []:
        cmd += ["--include", pat]
    env = dict(os.environ, BUCKET=str(source.get("bucket") or ""),
               SKIP_DISK_CHECK="true")
    proc = subprocess.run(cmd, env=env, cwd=staging)
    if proc.returncode != 0:
        raise FetchError(f"下载失败(退出码 {proc.returncode}),暂存:{staging}")


def free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def same_filesystem(a: str, b: str) -> bool:
    """落地根与暂存根是否同一个文件系统(st_dev 相等)。

    单独成函数是给测试留的接缝:生产常态是落地在 /mnt/tos 挂载、暂存在容器
    本地盘(不同盘),而单测里两者都在 tmp_path 下天然同盘 —— 用例按要验的
    场景各自打点。"""
    return os.stat(a).st_dev == os.stat(b).st_dev


def remove_local_batch(path: str) -> None:
    """拷完一批就删本地 —— 双缓冲的全部意义:本地盘峰值 ≈ 2 批,不是整个数据集。
    独立成函数是给测试留的接缝(重叠/峰值用例在这儿打点)。"""
    shutil.rmtree(path, ignore_errors=True)


# ── 暂存可写预算 ─────────────────────────────────────────────────────────


def _tree_bytes(root: str) -> int:
    total = 0
    for cur, _dirs, names in os.walk(root):
        for n in names:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(cur, n))
    return total


def staging_capacity(staging_root: str) -> tuple[int, str]:
    """暂存目录能**安全**写入的字节数 + 依据说明(进预检报文,让用户对得上账)。

    ⚠️ 不能只信 shutil.disk_usage(2026-08-18 生产 pod 实测):容器的
    ephemeral-storage limit 是 30Gi,而 df / disk_usage 报的是**节点整块盘**的
    2.2 TiB —— 限额是 kubelet 轮询用量强制执行的,不是文件系统配额,downward API
    之外容器内看不到它。按 df 当预算,下一个大数据集会把 pod 写到被 kubelet
    **驱逐**(pod 直接消失,日志看不出为什么),比拒绝下载严重得多。

    所以:预算 = min(限额侧, df 实际可用)。限额从 CURATION_EPHEMERAL_LIMIT_BYTES
    读(deploy yaml 用 downward API 注入 limits.ephemeral-storage);限额是容器
    **总**用量上限,还要扣掉暂存里已占的(上次失败保留的现场)和安全余量(日志/
    缓存这些量不到的部分)。环境变量缺失/坏值时**绝不静默退回 df**,按保守缺省
    10 GiB 估算并明说 —— 宁可保守拒绝,也不要把 pod 写死。
    """
    df_free = free_bytes(staging_root)
    raw = os.environ.get("CURATION_EPHEMERAL_LIMIT_BYTES", "").strip()
    limit = None
    if raw:
        try:
            limit = int(raw)
        except ValueError:
            pass
    if limit is not None and limit > 0:
        used = _tree_bytes(staging_root)
        from_limit = max(0, limit - used - EPHEMERAL_SAFETY_MARGIN)
        basis = (f"容器存储限额 {_fmt_bytes(limit)} − 暂存已占 {_fmt_bytes(used)}"
                 f" − 安全余量 {_fmt_bytes(EPHEMERAL_SAFETY_MARGIN)}")
    else:
        from_limit = DEFAULT_EPHEMERAL_LIMIT
        basis = (f"未注入容器存储限额,按保守值 {_fmt_bytes(DEFAULT_EPHEMERAL_LIMIT)} "
                 "估算;要下更大的数据集请在部署里注入 CURATION_EPHEMERAL_LIMIT_BYTES")
    avail = min(df_free, from_limit)
    basis += f";磁盘实际可用 {_fmt_bytes(df_free)},取较小者"
    return avail, basis


# ── 纯逻辑(可直接单测)─────────────────────────────────────────────────


def fetch_sources(cfg: dict) -> dict:
    """配置的 fetch_sources 段;缺段用出厂默认(值=P0 实测通的那组)。"""
    return cfg.get("fetch_sources") or FACTORY_FETCH_SOURCES


def _include_match(relpath: str, includes: list[str], url_prefix: str) -> bool:
    """--include 的匹配语义**照抄下载器**(读它的脚本源码核实):模式里 `.` 转义、
    `*` → `.*`,按**未锚定正则**匹配到**完整下载 URL**上 —— 我们的体积预检必须
    与它下载的文件集合逐字一致,否则预检量 A 集合、实下 B 集合,预检就是假的。
    """
    url = url_prefix + relpath
    for pat in includes:
        rx = pat.replace(".", r"\.").replace("*", ".*")
        if re.search(rx, url):
            return True
    return False


def filter_manifest(files: list[str], includes: list[str] | None,
                    source: dict, ref: str) -> list[str]:
    if not includes:
        return list(files)
    prefix = f"{str(source['base_url']).rstrip('/')}/{ref}/"
    return [f for f in files if _include_match(f, includes, prefix)]


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.2f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{n} B"


def batch_budget(avail_bytes: int) -> int:
    """单批体积预算 = min(8 GiB, 可写/3)。除以 3 = 在途两批(一批在下、一批在拷)
    + 一份安全余量;8 GiB 上限的取值理由见 BATCH_BUDGET_CAP。"""
    return min(BATCH_BUDGET_CAP, avail_bytes // 3)


def plan_batches(files: list[str], sizes: dict[str, int],
                 budget: int) -> list[list[str]]:
    """按清单顺序打包成批:体积到预算、或文件数到 BATCH_MAX_FILES 就切一刀。
    单个文件比预算还大 → 自己独占一批(装不装得下盘由调用方对照可写预算把关,
    这里不拒也不死循环)。"""
    batches: list[list[str]] = []
    cur: list[str] = []
    cur_bytes = 0
    for f in files:
        sz = sizes[f]
        if cur and (cur_bytes + sz > budget or len(cur) >= BATCH_MAX_FILES):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(f)
        cur_bytes += sz
    if cur:
        batches.append(cur)
    return batches


def split_resume(target: str, files: list[str],
                 sizes: dict[str, int]) -> tuple[list[str], list[str]]:
    """文件级续传:目标目录里已有、且大小与源站 HEAD 相等的文件跳过。
    返回 (还要下的, 跳过的)。

    为什么按**文件**不按批:批次边界只是执行细节,批预算一调整"第几批"就全变,
    存进标记再读回来必然对不上;而且不该信任一份可能过期的进度记录 —— 目标目录
    本身就是唯一可信的进度(失败标记里的批号只是给人看的现场描述,这里绝不读它)。
    判据 = 大小与源站 HEAD 相等,与逐批校验同一条判据;内容哈希源站不提供,
    诚实地只认大小,认不出的一律重下。

    ⚠️ 哨兵(meta/info.json)**永不参与续传跳过**(2026-08-18 libero 实机事故):
    ① 判存在之后才在目标目录里冒出来的哨兵(一次 `--include "meta/*"` 部分拉取
    恰在我们开工后 18 秒发布了它)被这里当成"已下好"跳过 —— 它不进任何批、
    不被押后暂存,整体校验一边报"缺失"、目标目录里一边躺着它本尊;更糟的是
    校验失败后这份半成品就挂着"可入住"的牌子被数据集清单列成有效数据集。
    哨兵只有"押后-整体校验-最后发布"一条路,一律重下(才 2 KB,不心疼)。
    """
    pending: list[str] = []
    skipped: list[str] = []
    for f in files:
        if f == SENTINEL_RELPATH:
            pending.append(f)
            continue
        try:
            ok = os.path.getsize(os.path.join(target, f)) == sizes.get(f)
        except OSError:
            ok = False
        (skipped if ok else pending).append(f)
    return pending, skipped


def validate_batch(staged_root: str, batch: list[str],
                   sizes: dict[str, int]) -> list[str]:
    """每批入库前的结构化校验(两层校验的第一层;第二层见 validate_final)。
    **不能只看下载器退出码**:P0 实测镜像里存在源头就残缺的数据集
    (aloha_sim_transfer_cube_human 唯一的 mp4 在源站 content-length 就是 0,
    下载器会忠实把 0 字节下下来并报成功)。

    三条,任一不过 = 本批拒绝入库:
      1. 没有 *.aria2 残留(有 = 没下完);
      2. 批内每个文件的本地大小与源站 HEAD 的 content-length **逐个相等**
         (⚠️ 不许用 du 总量对账:跨文件系统 du 把目录项也算进去,本地与 FSX
         正好差 目录数×4096,P0 实测踩过);
      3. 任何 0 字节的 .mp4 都点名(源头即残缺,下多少遍都一样)。
    返回问题清单(空 = 通过)。
    """
    problems: list[str] = []
    for cur, _dirs, names in os.walk(staged_root):
        for n in names:
            if n.endswith(".aria2"):
                rel = os.path.relpath(os.path.join(cur, n), staged_root)
                problems.append(f"续传残留 {rel}(下载没有完成)")
    for f in batch:
        local = os.path.join(staged_root, f)
        want = sizes.get(f)
        if not os.path.isfile(local):
            problems.append(f"清单文件缺失 {f}(应有 {_fmt_bytes(want or 0)})")
            continue
        got = os.path.getsize(local)
        if want is not None and got != want:
            problems.append(f"大小不符 {f}:本地 {got} B ≠ 源站 {want} B")
        if f.endswith(".mp4") and got == 0:
            problems.append(f"0 字节视频 {f}(源头即残缺)")
    return problems


def validate_final(info_path: str | None, manifest: list[str],
                   sizes: dict[str, int]) -> list[str]:
    """全部批完成后的整体校验(第二层)。单独一层的原因:这些判据要看**整个
    数据集**,放进逐批校验会在 info.json 不在本批时误报。

      1. meta/info.json 能解析(完整拉取必须有它);
      2. info.json 声明了 dtype: video → 必须有非空的视频文件(dtype: image
         内嵌 parquet 则不要求)。
    "非空"判据用源站 HEAD 的 sizes,不回读 FSX:每批入库前已逐文件核对
    本地大小 = HEAD,续传跳过的文件也是按同一判据放行的;这里再去读 FSX
    反而会被它 20-60s 的可见性延迟误报。
    """
    problems: list[str] = []
    if info_path is None:
        problems.append("meta/info.json 缺失(完整拉取必须包含它)")
        return problems
    info = None
    try:
        with open(info_path, encoding="utf-8") as fp:
            info = json.load(fp)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        problems.append(f"meta/info.json 不可解析({type(e).__name__}: "
                        f"{str(e)[:80]})")
    if isinstance(info, dict):
        dtypes = {str(v.get("dtype")) for v in (info.get("features") or {}).values()
                  if isinstance(v, dict)}
        if "video" in dtypes:
            good_mp4 = [f for f in manifest if f.endswith(".mp4")
                        and sizes.get(f, 0) > 0]
            if not good_mp4:
                problems.append("info.json 声明了 dtype: video,但没有一个"
                                "非空的视频文件")
    return problems


def unsupported_visual_note(info: dict, manifest: list[str]) -> str | None:
    """格式吃不进去时的明确标注(用户定:照下 + 点名判据,不含糊说"可能不支持")。

    判据要具体:视觉质量/视频-动作同步/VLM 任务判定都依赖 mp4 —— 数据集 0 个
    视频文件、画面以图像(dtype: image)内嵌在 parquet 里,这几步就没有原料。
    """
    feats = info.get("features") or {}
    dtypes = {str(v.get("dtype")) for v in feats.values() if isinstance(v, dict)}
    has_mp4 = any(f.endswith(".mp4") for f in manifest)
    if "video" not in dtypes and not has_mp4 and "image" in dtypes:
        return ("⚠️ 数据已入库,但本系统当前无法对它做视觉类检查(视觉质量/"
                "视频-动作同步/VLM 任务判定):这个数据集没有视频文件,画面以图像"
                "(dtype: image)内嵌在 parquet 里,上述检查都以 mp4 为原料;"
                "只能跑数值类检查(时间戳/运动学/运动质量)。")
    return None


# ── 跨 pod 进行中标记 ────────────────────────────────────────────────────


def _marker_path(into: str, name: str) -> str:
    return os.path.join(into, f".fetching-{name}.json")


def pod_identity() -> str:
    return os.environ.get("HOSTNAME") or socket.gethostname()


def _marker_run_id(path: str) -> str | None:
    """标记里记录的 run_id(读不出 / 没记 → None)。

    收尾删/改标记前必须先比对它:不是自己写的绝不动(2026-08-18 libero 事故的
    帮凶 —— 两个运行同名标记互相覆盖,先完成的把并发运行的标记也顺手删了,
    第三个运行会误判"没人在下"闯进来一起写;失败记录同理:把别人的活动标记
    改写成墓碑等于替它宣告死亡)。"""
    try:
        data = json.loads(open(path, "rb").read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    rid = data.get("run_id")
    return str(rid) if rid else None


def marker_verdict(path: str, now: datetime.datetime | None = None) -> tuple[str, str]:
    """现有进行中标记的裁定:("none"|"fresh"|"stale"|"failed", 给人看的描述)。

    ⚠️ 这**不是锁**,也不许在文档/帮助里说成锁:对象存储挂载没有原子创建保证,
    两个 pod 同一瞬间开工仍可能都写上自己的标记。它做的是把"两个实例静默互相
    覆盖写了一半的文件"降级成"大概率能明确告诉你有人在下、是谁、什么时候开始
    的"。陈旧线 STALE_MARKER_HOURS(取值理由见常量注释):超线的标记按残留
    处理、允许接管 —— 否则一次 pod 崩溃就把这个数据集永久堵死。

    "failed" = 上一跑退出前写下的**失败记录**(断在第几批),是墓碑不是占用:
    写得出它说明那次跑已经死透,**不设陈旧门槛**,重跑立即接管按文件级续传走
    (断点批号只是给人看的现场描述,续传逻辑绝不读它)。
    标记内容读不出(FSX 可见性延迟/写坏)时退回看文件 mtime,谁在下、是否失败
    都说不出,但"多新"还答得出来 —— 按 fresh/stale 保守处理。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        raw = open(path, "rb").read()
    except OSError:
        return "none", ""
    started = None
    host = "(读不出)"
    ref = ""
    try:
        data = json.loads(raw.decode("utf-8"))
        host = str(data.get("host") or "(未记录)")
        ref = str(data.get("ref") or "")
        if "failed_at_batch" in data or "failed_stage" in data:
            where = (str(data.get("failed_stage") or "")
                     or f"第 {data.get('failed_at_batch')}/"
                        f"{data.get('batches_total', '?')} 批")
            return "failed", (f"{host} 于 {data.get('started_at', '?')} 开始"
                              + (f"(ref={ref})" if ref else "")
                              + f",断在{where}")
        started = datetime.datetime.fromisoformat(data["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, KeyError, UnicodeDecodeError, TypeError):
        try:
            started = datetime.datetime.fromtimestamp(
                os.path.getmtime(path), tz=datetime.timezone.utc)
        except OSError:
            return "none", ""
    age_h = (now - started).total_seconds() / 3600.0
    desc = (f"{host} 于 {started.isoformat(timespec='seconds')} 开始"
            + (f"(ref={ref})" if ref else "") + f",距今 {age_h:.1f} 小时")
    return ("stale" if age_h >= STALE_MARKER_HOURS else "fresh"), desc


# ── 主流程 ───────────────────────────────────────────────────────────────


def _default_into(cfg: dict) -> str | None:
    """--into 缺省 = 配置里**第一个** tos_buckets 的 datasets_path(与 UI 的
    "第一个桶就是默认桶"同一条约定)。"""
    for item in cfg.get("tos_buckets") or []:
        if isinstance(item, dict) and str(item.get("datasets_path") or "").strip():
            return str(item["datasets_path"]).strip()
    return None


def run_fetch(cfg: dict, source_name: str, ref: str, *, into: str | None = None,
              name: str | None = None, includes: list[str] | None = None,
              overwrite: bool = False, staging_root: str | None = None) -> int:
    sources = fetch_sources(cfg)
    if source_name not in sources:
        print(f"[输入错误] 未知数据来源 {source_name!r};可用: {sorted(sources)}",
              file=sys.stderr)
        return 2
    source = sources[source_name]
    name = (name or ref).strip()
    into = into or _default_into(cfg)
    if not into:
        print("[输入错误] 没有落地目录:请给 --into,或在站点配置的 tos_buckets "
              "里声明数据集根(datasets_path)", file=sys.stderr)
        return 2
    if not os.path.isdir(into):
        # 根目录不存在多半是挂载没就位 —— 静默 makedirs 会把数据下到容器本地盘,
        # 看着成功、桶里什么都没有(未挂载陷阱),必须当场拒绝。
        print(f"[输入错误] 数据集根不存在:{into}(挂载没就位?)", file=sys.stderr)
        return 2

    target = os.path.join(into, name)
    sentinel = os.path.join(target, "meta", "info.json")
    # ① 哨兵判存在:list_datasets 认 meta/info.json,它在 = 数据集已入库
    if os.path.exists(sentinel) and not overwrite:
        print(f"[fetch] {target} 已有 meta/info.json,跳过(要重下加 --overwrite)")
        return 0

    staging_root = (staging_root or os.environ.get("CURATION_FETCH_STAGING")
                    or tempfile.gettempdir())
    real_staging_root = os.path.realpath(staging_root)
    if real_staging_root == "/mnt/tos" or real_staging_root.startswith("/mnt/tos/"):
        # 2026-08-05 实锤:FSX 挂载撞 aria2 多连接随机写 → 静默残缺。暂存永远在本地盘。
        print(f"[输入错误] 暂存目录不能在 /mnt/tos 挂载上({staging_root}):"
              "FSX 不能承受下载器的多连接随机写,会产出静默残缺的文件;"
              "暂存必须放容器本地盘", file=sys.stderr)
        return 2

    # 跨 pod 进行中标记(检查 + 落自己的;语义与局限见 marker_verdict)
    marker = _marker_path(into, name)
    state, desc = marker_verdict(marker)
    if state == "fresh":
        print(f"[fetch] 有别的实例正在下 {name!r}:{desc}。等它完成,或确认它已"
              f"死透后删除 {marker} 再试", file=sys.stderr)
        return 2
    if state == "stale":
        print(f"[fetch] 发现陈旧的进行中标记({desc},已超 {STALE_MARKER_HOURS} "
              "小时陈旧线),按残留接管")
    if state == "failed":
        print(f"[fetch] 上一次下载没有完成({desc});失败记录不算占用,"
              "按文件级续传接着跑")
    started_at = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")
    # 本次运行的唯一 id:主机 + 进程 + 起始时刻。⚠️ 标记仍然**不是锁**(对象存储
    # 挂载没有原子创建,两个运行仍可能同时开工、互相覆盖对方的标记,帮助/文档里
    # 也不许说成锁)—— run_id 的作用只有一个:收尾删/改标记前先认领,不是自己
    # 写的就不动,把"先完成的运行顺手删掉并发运行的标记"降级成"大概率能发现
    # 并警告"(细节见 _marker_run_id)。
    run_id = f"{pod_identity()}/{os.getpid()}/{started_at}"
    marker_payload = {"host": pod_identity(), "ref": ref, "run_id": run_id,
                      "started_at": started_at, "target": target}
    write_json(marker, marker_payload)

    staging = None
    keep_staging = False
    #: 失败现场描述(断在第几批/哪个阶段),非 None = 本次以失败收场,
    #: finally 里把它写进标记(⑤ 失败现场保全,理由见 finally 的注释)。
    failure_note: dict | None = None
    try:
        # 清单 + 体积实测(inspect 的 Used Storage 一个字不用,理由见模块头)
        # full_manifest = 源站的完整清单;manifest = --include 过滤后要下的那些。
        # 两个都留着:判"这个数据集有没有视频"必须看**完整清单**,只看下载集合
        # 会把"我这次没拉视频"误判成"这数据集没有视频"。
        full_manifest = list_remote_files(source, ref)
        manifest = filter_manifest(full_manifest, includes, source, ref)
        if not manifest:
            print(f"[输入错误] --include 没匹配到任何文件(源站清单里没有满足 "
                  f"{includes} 的路径)", file=sys.stderr)
            return 2
        print(f"[fetch] 源站清单 {len(manifest)} 个文件,逐文件核对真实体积…",
              flush=True)
        sizes = measure_sizes(source, ref, manifest)
        need = sum(sizes.values())
        # flush:下一步是子进程直通输出,不冲缓冲会让这行"迟到"在下载日志后面
        print(f"[fetch] 真实体积 {_fmt_bytes(need)}({len(sizes)} 个文件)",
              flush=True)

        # ②③ 暂存 + 体积预检。分批入库后总体积再大都不拒,只有**单个文件**
        # 连暂存可写预算都装不下才是死路(批再小也得装下最大的那个文件)。
        os.makedirs(staging_root, exist_ok=True)
        avail, avail_basis = staging_capacity(staging_root)
        print(f"[fetch] 暂存可写预算 {_fmt_bytes(avail)}(依据:{avail_basis})",
              flush=True)
        staging = tempfile.mkdtemp(prefix=f"curation-fetch-{name}-",
                                   dir=staging_root)
        biggest = max(manifest, key=lambda f: sizes[f])
        if sizes[biggest] > avail:
            print(f"[体积预检] 单个文件比暂存可写预算还大:{biggest} "
                  f"需要 {_fmt_bytes(sizes[biggest])}、可用 {_fmt_bytes(avail)},"
                  f"还差 {_fmt_bytes(sizes[biggest] - avail)}"
                  f"(暂存 {staging_root};依据:{avail_basis})—— 拒绝下载",
                  file=sys.stderr)
            return 2

        # --overwrite 的清场必须在续传扫描**之前**:分批模式边下边入库,旧文件
        # 留着会被文件级续传当作"已下好"跳过,--overwrite 的语义就丢了。
        # 先摘哨兵:清场窗口里这个目录不再自称有效数据集。
        if overwrite and os.path.isdir(target):
            with contextlib.suppress(OSError):
                os.unlink(sentinel)
            shutil.rmtree(target, ignore_errors=True)

        # 硬性不变量:整体校验通过之前,哨兵绝不出现在目标目录里。走到这儿
        # 说明 ① 判存在时它还不在 —— 现在在,只能是开工后才冒出来的
        # (2026-08-18 libero 实机事故:并发的 `--include "meta/*"` 部分拉取在
        # 我们开工后 18 秒发布了它;FSX 可见性延迟也能造出同样的窗口)。
        # 摘掉它:本次拉取期间这个目录不许自称有效数据集,校验过了会按
        # "哨兵最后写"重新发布(哨兵永不参与续传,见 split_resume)。
        if os.path.exists(sentinel):
            try:
                os.unlink(sentinel)
            except OSError as e:
                raise FetchError(
                    f"目标目录里有一个不属于本次拉取的哨兵({sentinel}),"
                    f"摘除失败({type(e).__name__}: {e})—— 摘不掉就无法保证"
                    "\"校验通过前目录不自称有效数据集\",拒绝继续")
            print(f"[fetch] 目标目录里发现开工后才出现的 meta/info.json(哨兵),"
                  "已先摘下 —— 本次拉取通过整体校验后会重新发布它,校验不过"
                  "则数据集清单不会把这份未完成的数据列出", flush=True)

        # ④ 文件级续传(为什么不按批续传:见 split_resume)
        pending, skipped = split_resume(target, manifest, sizes)
        if skipped:
            print(f"[fetch] 目标目录已有 {len(skipped)} 个文件(大小与源站一致),"
                  f"跳过重下,还差 {len(pending)} 个", flush=True)

        # 落地根与暂存根同盘(--into 指本地路径)时,落地的数据同样吃容器的
        # ephemeral-storage 配额:"暂存在途 + 落地全量"两份一起写,pod 会被
        # kubelet 驱逐(2026-08-18 真事故:--into /tmp 落 32.5 GiB,pod Evicted,
        # 而预检只算了暂存)。同盘就按更严口径判:落地全量常驻,暂存只能用
        # 剩下的,且剩下的至少还要装下最大单文件。生产常态(落地在 /mnt/tos
        # 挂载,不同盘)维持原口径。
        avail_for_staging = avail
        if pending and same_filesystem(into, staging_root):
            landing = sum(sizes[f] for f in pending)
            biggest_pending = max(sizes[f] for f in pending)
            print(f"[fetch] 落地目录与暂存在同一个文件系统,总量一起算:"
                  f"落地全量 {_fmt_bytes(landing)} + 暂存在途,共用可写预算 "
                  f"{_fmt_bytes(avail)}", flush=True)
            if landing + biggest_pending > avail:
                print(f"[体积预检] 落地目录与暂存在同一个文件系统,总量一起算:"
                      f"落地全量 {_fmt_bytes(landing)} + 暂存至少要装下最大单文件 "
                      f"{_fmt_bytes(biggest_pending)},需要 "
                      f"{_fmt_bytes(landing + biggest_pending)}、可用 "
                      f"{_fmt_bytes(avail)},还差 "
                      f"{_fmt_bytes(landing + biggest_pending - avail)}"
                      f"(依据:{avail_basis})—— 拒绝下载;要下大数据集请把 "
                      "--into 指向挂载上的数据集根", file=sys.stderr)
                return 2
            avail_for_staging = avail - landing

        # ⑤ 打包成批 + 下载/入库重叠(收益实测数据见模块头,别拆回串行)
        budget = (int(os.environ.get("CURATION_FETCH_BATCH_BUDGET") or 0)
                  or batch_budget(avail_for_staging))
        batches = plan_batches(pending, sizes, budget)
        total = len(batches)
        current_batch = 0
        held_info: dict = {"path": None}
        copier_box: dict = {"error": None}
        # 深度 1 的队列 + 2 个"磁盘位":下载第 N+2 批之前,第 N 批必须已拷完
        # 并删掉本地 —— 这才是"本地峰值 ≈ 2 批"的硬保证(只靠队列深度拦不住:
        # 拷贝一取走队头,下载就能再超前一批,峰值会变 3 批)。
        work: queue.Queue = queue.Queue(maxsize=1)
        slots = threading.BoundedSemaphore(2)

        def copier_main():
            # 唯一的拷贝线程(顺序写,FSX 并发写没把握;单线程实测 223 MB/s 已跑满)。
            # 入库仍走 safe_write.publish_file 的原子发布通道,不另造第二种 FSX 写法。
            while True:
                item = work.get()
                if item is None:
                    return
                no, batch, staged_root, batch_dir = item
                try:
                    for f in batch:
                        src = os.path.join(staged_root, f)
                        if f == SENTINEL_RELPATH:
                            # 哨兵押后:分批后 info.json 可能在中间某批,先挪到
                            # 暂存根(本地盘内 rename),等全部批完成+整体校验过
                            # 才发布 —— 哨兵最后落地的纪律不因分批而破。
                            held = os.path.join(staging, "held-info.json")
                            os.replace(src, held)
                            held_info["path"] = held
                        else:
                            publish_file(src, os.path.join(target, f))
                    remove_local_batch(batch_dir)
                    print(f"[fetch] 第 {no}/{total} 批已入库,本地暂存已清",
                          flush=True)
                except BaseException as e:  # noqa: BLE001 —— 必须传回主线程,吞掉=静默半库
                    copier_box["error"] = (no, e)
                    return
                finally:
                    slots.release()

        copier = threading.Thread(target=copier_main, daemon=True,
                                  name="curation-fetch-copier")

        def _raise_if_copier_failed():
            # 拷贝线程的异常必须传回主线程并停下 —— 不检查就是"下载全绿、
            # 一半没入库"的静默事故
            nonlocal failure_note, keep_staging
            if copier_box["error"] is None:
                return
            no, exc = copier_box["error"]
            failure_note = {"failed_at_batch": no, "batches_total": total}
            keep_staging = True
            raise FetchError(
                f"第 {no}/{total} 批入库(拷贝线程)失败:"
                f"{type(exc).__name__}: {exc};已入库的批留在 {target},"
                f"该批暂存保留供排查:{staging}")

        def _finish_copier():
            # 收尾:让在途批拷完再退出(已通过校验的批入库是安全的,半途丢弃
            # 只会浪费一遍下载);拷贝线程死了就不投毒药丸,直接收尸。
            while copier_box["error"] is None:
                try:
                    work.put(None, timeout=0.5)
                    break
                except queue.Full:
                    if not copier.is_alive():
                        break
            copier.join(timeout=600)

        if batches:
            copier.start()
            try:
                for no, batch in enumerate(batches, 1):
                    current_batch = no
                    # 等磁盘位时轮询拷贝线程死活,否则它死了这里会干等到天荒地老
                    while not slots.acquire(timeout=0.5):
                        _raise_if_copier_failed()
                    _raise_if_copier_failed()
                    batch_bytes = sum(sizes[f] for f in batch)
                    print(f"[fetch] 第 {no}/{total} 批:下载 {len(batch)} 个文件"
                          f"({_fmt_bytes(batch_bytes)})…", flush=True)
                    batch_dir = os.path.join(staging, f"batch-{no:04d}")
                    os.makedirs(batch_dir, exist_ok=True)
                    # 批内文件以**精确路径**逐个 --include(实测约束见
                    # download_dataset):下载集合 = 校验集合 = 入库集合,逐字一致
                    download_dataset(source, ref, batch_dir, list(batch))
                    staged_root = os.path.join(batch_dir, ref)
                    if not os.path.isdir(staged_root):
                        raise FetchError(
                            f"下载完成但暂存里没有 {ref}/ 目录:{batch_dir}")
                    problems = validate_batch(staged_root, batch, sizes)
                    if problems:
                        failure_note = {"failed_at_batch": no,
                                        "batches_total": total}
                        keep_staging = True
                        shown = problems[:10]
                        print(f"[校验失败] 第 {no}/{total} 批 {len(problems)} 处"
                              f"问题,拒绝入库(前 {len(shown)} 处):",
                              file=sys.stderr)
                        for p in shown:
                            print(f"  - {p}", file=sys.stderr)
                        print(f"  已入库的批留在 {target}(哨兵没写,数据集清单"
                              f"不会把它当完整数据集列出);该批暂存保留供排查:"
                              f"{staged_root}", file=sys.stderr)
                        return 1
                    while True:
                        _raise_if_copier_failed()
                        try:
                            work.put((no, batch, staged_root, batch_dir),
                                     timeout=0.5)
                            break
                        except queue.Full:
                            pass
            except FetchError:
                if failure_note is None and current_batch:
                    failure_note = {"failed_at_batch": current_batch,
                                    "batches_total": total}
                    keep_staging = True
                raise
            finally:
                _finish_copier()
            _raise_if_copier_failed()
        else:
            print("[fetch] 所有文件都已在目标目录,无需下载", flush=True)

        # ⑥ 整体校验(两层校验的第二层)+ 哨兵最后写。哨兵最后落地的原因:
        # list_datasets 判"有效数据集"的依据就是它 —— 先落哨兵会让边拷边打开
        # 界面的人读到一半没了的视频;比目录 rename 更稳(FSX 上目录 rename
        # 大概率是 copy)。
        held = held_info["path"]
        notes: list[str] = []
        if includes:
            # 部分拉取**一律不发布哨兵**(哪怕 meta/info.json 被 --include 命中、
            # 已经下到手,也只押在暂存里随暂存清理):哨兵的含义是"这是一份完整
            # 可用的数据集",部分拉取按定义不完整,发哨兵就是撒谎 —— meta 声明
            # 的 episode 大多不在盘上,被数据集清单列出后质检会读到不存在的数据;
            # 2026-08-18 libero 事故的整条因果链(部分拉取发了哨兵 → 全量续传把
            # 它当"已下好"跳过 → 整体校验报"缺失"而它就在)根子就在这儿。
            notes.append(
                "ℹ️ 这是部分拉取(--include),数据按定义不完整,这个目录不会被"
                "当作可用数据集列出 —— 故意不写 meta/info.json:meta 里声明的 "
                "episode 大多不在盘上,列成可用数据集的话,质检一跑就会读到"
                "不存在的数据。要得到可用数据集,请不带 --include 完整拉取"
                "(已下好的文件会被续传跳过,只补缺的)")
            info = None
            if held is not None:
                with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
                    with open(held, encoding="utf-8") as fp:
                        info = json.load(fp)
            if isinstance(info, dict):
                # ⚠️ 判据是"证据够不够",不是"用没用 --include"(2026-08-18
                # 实机咬到:--include "meta/*" 拉了 image-only 的 libero 却一声
                # 不吭)。判"这个数据集里没有 mp4"必须看**源站完整清单**;下载
                # 集合只是子集,判不了就明说是"本次没拉视觉文件",不装作没事。
                vnote = unsupported_visual_note(info, full_manifest)
                if vnote is None and not any(f.endswith(".mp4") for f in manifest):
                    vnote = ("ℹ️ 本次没有拉取视觉文件(--include 只命中了非视频"
                             "文件) —— 视觉类检查在补齐视频文件之前跑不了")
                if vnote:
                    notes.append(vnote)
        else:
            problems = validate_final(held, manifest, sizes)
            if problems:
                failure_note = {"failed_stage": "整体校验"}
                keep_staging = True
                print(f"[校验失败] 整体校验 {len(problems)} 处问题,拒绝入库"
                      "(哨兵不写,数据集清单不会列出它):", file=sys.stderr)
                for p in problems[:10]:
                    print(f"  - {p}", file=sys.stderr)
                print(f"  暂存保留供排查:{staging}", file=sys.stderr)
                return 1
            # 整体校验过了 held 必不为 None(缺失早就报了)—— 只有完整拉取
            # 才走到这儿发布哨兵
            publish_file(held, sentinel)
            if content_state(sentinel, "json") != "ok":
                failure_note = {"failed_stage": "哨兵回验"}
                keep_staging = True
                print(f"[fetch] 拷入后哨兵回验失败:{sentinel} 读不回可解析的 "
                      "info.json,这份入库不可信,需要重跑", file=sys.stderr)
                return 1
            with open(sentinel, encoding="utf-8") as fp:
                info = json.load(fp)
            vnote = unsupported_visual_note(info, full_manifest)
            if vnote:
                notes.append(vnote)

        print(f"[fetch] 完成:{source.get('display_name', source_name)} 的 "
              f"{ref} → {target}({len(manifest)} 个文件,{_fmt_bytes(need)})")
        for note in notes:
            print(f"[fetch] {note}")
        return 0
    except FetchError as e:
        print(f"[fetch 失败] {e}", file=sys.stderr)
        return 1
    finally:
        if _marker_run_id(marker) != run_id:
            # 标记已不是本次运行写的(被另一个运行覆盖,或被删):绝不动它,
            # 只警告(理由见 _marker_run_id;这仍然不是锁,只是把静默互相覆盖
            # 降级成"大概率能发现并说明")。
            print(f"[fetch] ⚠️ 进行中标记已被另一个运行覆盖或删除({marker}),"
                  "本次不动它 —— 很可能有两个运行在并发写同一个目标目录,"
                  "请确认没有别的实例在下同一个数据集", file=sys.stderr)
        elif failure_note is not None:
            # ⑤ 失败现场保全 —— 为什么不"全删重来":
            #   · 已进 TOS 的文件**留着**:下次按文件级续传直接跳过,省一遍下载;
            #   · 哨兵没写:数据集清单不会把半份数据当完整数据集列出,不存在误用;
            #   · 标记改写成"断在哪儿"的失败记录:给人看的现场描述(续传逻辑
            #     绝不读它),且 marker_verdict 判 failed 不算占用,重跑不被挡;
            #   · 本地失败那批保留供排查(keep_staging 已置位)。
            with contextlib.suppress(OSError):
                write_json(marker, {**marker_payload, **failure_note,
                                    "failed_at": datetime.datetime.now(
                                        datetime.timezone.utc
                                    ).isoformat(timespec="seconds")})
        else:
            with contextlib.suppress(OSError):
                os.unlink(marker)
        if staging and not (keep_staging or failure_note):
            shutil.rmtree(staging, ignore_errors=True)
