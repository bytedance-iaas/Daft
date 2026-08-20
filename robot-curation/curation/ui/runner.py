"""UI 任务台的执行层:把面板上的点选变成一次 CLI 子进程,并把任务状态落盘。

定位(2026-08-13 用户拍板):UI 此前只是"只读渲染交付目录"的报告台,要跑质检必须
开终端敲 CLI。而终端页签对外部客户是最大的权限口子——拿到 UI 密码 = 拿到 pod 的
root shell(全量客户数据 + TOS/方舟密钥都在里面)。客户真正要的是"跑一次质检",
不是"拿一个 shell"。所以做一块面板,**CLI 能跑的功能面板都要能跑**;终端保留,
但对外部署直接不开(--terminal 开关照旧)。

三条设计红线,每条都有代价明确的理由:

**① 永不 import 管道代码,一律起子进程调 CLI。**
UI 与管道之间的接口只有两样:argv 与日志文本。这样"UI 只读交付目录、不 import
管道"的老红线一个字不用改;CLI 仍是参数校验/配置深合并/格式嗅探/降级逻辑的唯一
事实源,面板不会长出第二套判断;还白捡了管道现成的进度行(见 parse_progress)。
调用一律走 `sys.executable -m curation.cli`,不赌 PATH 里有没有 `curation`
(pod 里有,别的部署未必有)。

**② 路径受限:前端只传"名字",绝对路径由本模块用白名单根拼接。**
一个"随便输路径"的输入框等于把整个 pod 文件系统开给任何拿到 UI 密码的人。终端
那边无所谓(它本来就是 shell),但这块面板是给客户看的,受限与否性质完全不同。
校验见 safe_name/resolve_under:拒绝路径分隔符、`..`、隐藏名、怪字符,拼完还要
用 realpath 再确认一次仍在根下(挡符号链接绕过)。

**③ 任务状态落盘,不放 Gradio 的 session state。**
跑批动辄几小时。页面刷新、换个人打开、pod 重启(PID 1 就是 UI 进程)都不能丢
状态。落盘之后 pod 重启回来能如实标成 `interrupted`,而不是假装还在跑。

**完成态靠退出码文件而不是"进程还在不在"**:子进程由 UI 进程 fork 出来,若不
reap 会变成僵尸。所以真正开的是 `bash -c '<命令> >> log 2>&1; echo $? > rc'`,
rc 文件在则任务已终结(且重启后依然作数),进程死活只用来判"是不是被打断了"。

⚠️ 2026-08-14 用户实见的事故,把上面这条的两个前提都打穿了,四处一起改才治得住:
用户点停止 → 状态卡在「正在停止」**七分钟**,两个进程都是 Z(僵尸)且 ppid=1,
`exit_code` 文件**根本不存在**。
① 停止时 `killpg` 把外层那个 bash 也砍了,它还没走到 `echo $? > rc` 就没了 ⇒
   那个"唯一依据"的文件永远不会出现。对策:外层 shell 装 `trap`(见 start)。
② 本 pod 的 PID 1 就是这个 UI(一个 Python 进程),Python 不会 wait() 领养来的
   子进程 ⇒ 僵尸**永远不会被回收**,而僵尸的 `kill(pid, 0)` 照样成功。对策:
   `_alive()` 读 `/proc/<pid>/stat` 的状态位,Z 一律当死。
③ `stop()` 不能干等一个注定不出现的文件:进程组里没有活着的成员时自己写终态。
④ 归档回挂载的 `status.json` 那次变成了 164 字节全 `\0` 的坏文件(与 savefig
   零填充同一家族)⇒ 写完回读校验一次(见 _write_json)。

同一时刻只允许一个任务在跑(见 RunBusyError):VLM 并发已按单跑批调到
32 × 16,两个批叠加会砸穿方舟配额;同一输出目录更会因 daft 的 write_parquet
**追加**语义产出脏数据。

本模块只做"跑"这一侧的读写,不碰 manifest 的任何东西——报告页那套是已经交付给
客户在用的,新功能一律纯新增(2026-08-13 用户红线)。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

#: 任务目录挂在交付根下。用点开头:交付根同时是 UI 的交付扫描根,`.runs` 不会被
#: 误当成一份交付(discover_deliveries 找的是 passed.json)。
RUNS_DIRNAME = ".runs"

#: 活跃期的 run.log 与退出码文件落在**容器本地盘**,不在挂载上。
#: ⚠️ 为什么(2026-08-13 实测):交付根在 TOS 的 FSX 挂载上,那里"一边追加一边读"
#: 拿不到内容 —— 任务跑了很久 run.log 仍是 0 字节,手工 `echo hi >> /mnt/tos/x`
#: 再 cat 直接 `Stale file handle`。结果是 UI 的 2 秒轮询与进度条全程空手,用户
#: 点了开始只看见"运行中"。_write_json 里"FSX 上只有顺序整写是安全的"这条纪律
#: 早就写着,漏的是 start() 给 shell 的那句 `>> run.log` 重定向。
#: 任务结束时再把这两个文件**整份 cp** 回挂载(顺序整写),pod 重启后历史仍在。
LOCAL_RUNS_ROOT = os.environ.get("CURATION_RUNS_LOCAL", "/tmp/curation-runs")

#: 面板能发起的命令(与 curation/cli.py 的子命令一一对应)。backends 只是探活,
#: 秒级返回,放进来是为了"CLI 能跑的面板都能跑"这条要求的完整性。
COMMANDS = ("run", "rejudge", "review-page", "backends")

#: 可选模块的**语义名 → 界面中文名**。⚠️ M 编号(m4a 之类)绝不许出现在这里或
#: 任何用户可见处(项目红线);这也是读端的常量副本——UI 不 import 管道代码,
#: 所以不能 `from ..export.report import CHECK_CN`(manifest.py 里同样办法)。
CHECK_LABELS = {
    "timestamp_check": "时间戳检查",
    "kinematic_limits": "运动学极限",
    "motion_quality": "运动质量",
    "visual_quality": "视觉质量",
    "video_action_sync": "视频-动作同步",
    "task_success": "任务成败判定",
    "skill_profile": "技能画像",
    "dedup": "精确去重",
}

#: 名字白名单:字母数字打头,其后允许字母数字与 . _ -,最长 80。
#: 刻意不允许空格与中文——这些名字会变成目录名,还要进 shell 命令行。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: 管道打的进度行。两种格式都要认(见 pipeline/progress.py):
#:   条目式 `[curation] VLM 任务成败判定 12/199 (6%) | 已用 3.2min | 剩余 ~48min`
#:   阶段式 `[curation] 技能画像 3/5 归纳技能体系(LLM)… | 已用 1.2min`
#: 另外 review-page 与 rejudge 用自己的前缀且可能没有阶段名,故 stage 允许为空。
_PROGRESS_RE = re.compile(
    r"^\[(?P<src>[\w-]+)\]\s*(?P<stage>.*?)\s*(?P<n>\d+)\s*/\s*(?P<total>\d+|\?)"
    r"(?:\s*\((?P<pct>\d+)%\))?(?P<rest>.*)$")
_ELAPSED_RE = re.compile(r"已用\s*(\S+)")
_ETA_RE = re.compile(r"剩余\s*~?\s*(\S+)")

#: 数据集之间的分隔行(与 cli 的 `--batch` 输出同款)。任务台顺序跑多个数据集时
#: 每个开跑前打一行,否则三份日志糊成一片分不出谁是谁;进度解析也靠它断句。
_SECTION_RE = re.compile(r"^={3,}\s*(?P<name>.+?)\s*={3,}$")

#: 命令名 → 该命令在界面上的说法(日志与历史列表用,别让客户看见英文子命令)。
COMMAND_LABELS = {"run": "质检跑批", "rejudge": "执行裁决",
                  "review-page": "生成审片站", "backends": "后端探活"}


class RunBusyError(RuntimeError):
    """已有任务在跑。带上正在跑的那个,界面才能说清"在等谁"。"""

    def __init__(self, active: dict):
        self.active = active
        super().__init__(
            f"当前已有任务在跑({active.get('run_id')}:{active.get('label') or ''}),"
            "同一时刻只允许一个任务,请等它跑完或先停止它")


# ── 路径:名字进,绝对路径出 ────────────────────────────────────────────────

def safe_name(name: str) -> str:
    """校验一个"名字"(数据集名/交付名/审片站名)。不合法就抛,消息说人话。

    这是安全边界不是易用性:面板只收名字,路径由 resolve_under 拼。任何带路径
    分隔符、`..`、以点开头的输入一律拒绝——它们的存在本身就说明有人在试探。
    """
    s = str(name or "").strip()
    if not s:
        raise ValueError("名字不能为空")
    if "/" in s or "\\" in s:
        raise ValueError(f"名字里不能带路径分隔符:{s!r}(只填名字,目录由系统拼)")
    if s in (".", "..") or s.startswith("."):
        raise ValueError(f"名字不能以点开头:{s!r}")
    if not _NAME_RE.match(s):
        raise ValueError(
            f"名字只能用字母、数字、点、下划线、连字符,且以字母或数字开头,"
            f"最长 80 个字符:{s!r}")
    return s


def resolve_under(root: str, name: str) -> str:
    """<root>/<name> 的绝对路径。名字先过 safe_name,拼完再用 realpath 复核。

    为什么拼完还要复核:safe_name 挡得住 `../etc`,挡不住 root 下已存在的**符号
    链接**指向别处。realpath 之后再比一次前缀,这类绕过就没了。
    """
    n = safe_name(name)
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, n))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise ValueError(f"路径越界:{n!r} 解析后不在 {root} 之下")
    return target


#: 允许面板直接填路径的**唯一**区域:TOS 挂载(用户 2026-08-13 定)。
#: 配置文件不像数据集那样能靠扫目录列出来(客户的站点配置可能放在任意子目录),
#: 所以这一处开了个口子——但只开在 TOS 上:那是客户自己的数据面,不是容器的
#: 系统盘。容器根目录、/etc、/app、/tmp 一律进不来。
TOS_ROOT = os.environ.get("CURATION_TOS_ROOT", "/mnt/tos")

#: 配置文件认的后缀。挡的不是恶意(那由 TOS 前缀挡),是手滑填了个 parquet 进来。
CONFIG_SUFFIXES = (".yaml", ".yml")


def resolve_tos_path(path: str, *, root: str | None = None,
                     suffixes: tuple = CONFIG_SUFFIXES) -> str:
    """校验一个**用户手填的**文件路径:必须落在 TOS 挂载下、必须存在、后缀要对。

    与 resolve_under 的分工:那个是"只给名字、路径我拼"(数据集与交付走那条);
    这个是"你可以填路径,但只能填在 TOS 里"(配置文件走这条,因为它没法靠扫目录
    列出来)。两条都在 realpath 之后再比一次前缀 —— 符号链接是这类校验的经典绕过。
    """
    root = os.path.realpath(root or TOS_ROOT)
    s = str(path or "").strip()
    if not s:
        raise ValueError("路径不能为空")
    if not os.path.isabs(s):
        raise ValueError(f"请填绝对路径(以 {root} 开头):{s!r}")
    real = os.path.realpath(s)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError(f"只允许填 {root} 下的文件(这是安全边界):{s!r}")
    if not os.path.isfile(real):
        raise ValueError(f"文件不存在或不是一个文件:{s!r}")
    if suffixes and not real.lower().endswith(tuple(x.lower() for x in suffixes)):
        raise ValueError(f"扩展名应为 {'/'.join(suffixes)}:{s!r}")
    return real


# ── 跑批历史 ───────────────────────────────────────────────────────────────

HISTORY_HEADERS = ["开始时间", "任务", "状态", "耗时", "编号"]


def duration_text(started_at: str | None, finished_at: str | None,
                  now: datetime.datetime | None = None) -> str:
    """两个时间戳 → "3 分 12 秒"。还在跑的(没有结束时间)算到此刻。"""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        t0 = datetime.datetime.strptime(str(started_at), fmt)
    except (TypeError, ValueError):
        return "—"
    try:
        t1 = datetime.datetime.strptime(str(finished_at), fmt)
    except (TypeError, ValueError):
        t1 = now or datetime.datetime.now()
    sec = max(0, int((t1 - t0).total_seconds()))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"


def history_rows(runs: list, now: datetime.datetime | None = None) -> list[list]:
    """任务列表 → 表格行(状态用中文,与状态条同一套词,免得同一件事两种说法)。"""
    out = []
    for r in runs:
        state = STATE_STYLES.get(str(r.get("state")), STATE_STYLES["unknown"])[0]
        if r.get("state") == "failed":
            state += failed_suffix(r)
        out.append([r.get("started_at") or "—",
                    r.get("label") or COMMAND_LABELS.get(r.get("command"), "—"),
                    state,
                    duration_text(r.get("started_at"), r.get("finished_at"), now),
                    r.get("run_id") or ""])
    return out


def _scan_dataset_root(data_root: str) -> tuple[str, list[str]]:
    """扫一遍数据集根 → (状态, 有效数据集名列表)。状态三态:
    "unmounted"(目录不存在/读不了)/ "empty"(读得了但没有有效数据集)/ "ok"。

    为什么要分三态(2026-08-17):此前不存在和空目录都返回 [],界面上一律表现
    为"数据集下拉是空的"—— 前者是**部署事故**(FSX 没挂上),后者是正常状态,
    分不出来就没人去查部署。判据与 cli._list_datasets 同款:有 `meta/info.json`
    (LeRobot)或目录里有 `*.rrd`(rerun)。
    """
    try:
        names = os.listdir(data_root)
    except OSError:
        return "unmounted", []
    out = []
    for name in names:
        d = os.path.join(data_root, name)
        if not os.path.isdir(d):
            continue
        if os.path.exists(os.path.join(d, "meta", "info.json")):
            out.append(name)
            continue
        try:
            if any(f.endswith(".rrd") for f in os.listdir(d)):
                out.append(name)
        except OSError:
            continue
    return ("ok" if out else "empty"), sorted(out)


def list_datasets(data_root: str) -> list[str]:
    """数据集根下**一层**里的有效数据集名(不是路径)。

    目录不存在/读不了 → 空列表(界面显示"没扫到",不崩);"没挂上"和"空的"
    的区分交给 probe_dataset_root / dataset_root_note(本函数的老调用方只要
    名字清单,不该被迫改签名)。
    """
    return _scan_dataset_root(data_root)[1]


#: 根目录探测的缓存:{路径: (打点时刻, 状态)}。对象存储挂载上 stat/listdir 不
#: 便宜,而探测结果会出现在每次下拉渲染里 —— 不缓存等于每次渲染都去打 FSX。
#: 60 秒足够短:挂载修好后一分钟内界面自愈,不用重启。
_ROOT_PROBE_TTL = 60.0
_root_probe_cache: dict[str, tuple[float, str]] = {}


def probe_dataset_root(path: str, *, ttl: float = _ROOT_PROBE_TTL) -> str:
    """数据集根目录的三态探测(带 TTL 缓存):"ok" / "empty" / "unmounted"。

    ⚠️ 探测失败绝不抛:未挂载是要**标出来**的状态,不是要让 UI 崩的异常 ——
    别的桶可能是好的,炸掉整个界面是过度反应(与旧配置键那条"响亮报错"不同,
    这里的错误随时可能自愈)。测试要绕缓存就传 ttl=0。
    """
    key = str(path or "")
    now = time.monotonic()
    hit = _root_probe_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    status = _scan_dataset_root(key)[0]
    _root_probe_cache[key] = (now, status)
    return status


def dataset_root_note(path: str, *, ttl: float = _ROOT_PROBE_TTL) -> str:
    """选中某个根目录时,「数据集」下拉底下那句状态说明(正常时空串不打扰)。

    防的事故(2026-08-17):FSX 没挂上时界面只是"下拉是空的",和"目录确实还
    没数据"长得一模一样 —— 部署事故被当成正常状态,没人去查。
    """
    status = probe_dataset_root(path, ttl=ttl)
    if status == "unmounted":
        return "⚠️ 这个根目录没挂上,请检查部署(目录不存在或读不了)"
    if status == "empty":
        return "挂载正常,里面还没有数据集"
    return ""


def log_unmounted_roots(buckets: list) -> None:
    """启动时把没挂上的根目录逐个点名到日志(部署事故要在日志里留痕,不能只
    藏在界面的一个 ⚠️ 角标里)。只点名不抛错:别的桶可能是好的。"""
    for b in buckets:
        path = b.get("datasets_path") or ""
        if probe_dataset_root(path) == "unmounted":
            print(f"[curation-ui] 数据集根目录没挂上:{path}"
                  f"(TOS 桶 {b.get('bucket') or '未声明'})—— 目录不存在或"
                  "读不了,请检查 FSX 挂载/部署", file=sys.stderr)


def dataset_format(dataset_dir: str) -> dict:
    """数据集格式 → {kind, version, needs_clips}。**只读 meta 文件,不碰视频。**

    needs_clips = 这份数据"要在 Episodes 页逐条回看画面,得先切出片段吗":
    - LeRobot **v3**:多条 episode 合并进同一个 mp4,盘上没有逐条文件 → 要;
    - **rrd**:视频字节封在 .rrd 里,同样没有逐条 mp4 → 要;
    - LeRobot v2:每条本来就是独立 mp4,交付集里直接能播 → 不要。
    认不出格式一律 False —— 不给用户弹一个我们自己都没把握的询问框。
    """
    data = _read_json(os.path.join(dataset_dir, "meta", "info.json"))
    ver = str(data.get("codebase_version") or "")
    if ver:
        return {"kind": "lerobot", "version": ver,
                "needs_clips": ver.startswith("v3")}
    try:
        if any(f.endswith(".rrd") for f in os.listdir(dataset_dir)):
            return {"kind": "rrd", "version": "", "needs_clips": True}
    except OSError:
        pass
    return {"kind": "unknown", "version": "", "needs_clips": False}


def datasets_needing_clips(data_root: str, datasets: list) -> list[str]:
    """选中的这些数据集里,哪几个要先切片才能在 Episodes 页看画面(保持选中顺序)。

    2026-08-14 用户定:多选时**问一次、覆盖全部**。此前只有单数据集才问,多选直接
    跳过 —— 于是多选跑完的 v3/rrd 交付在 Episodes 页全是"没有画面",而用户压根没
    被问过。逐个弹窗更糟(选十个弹十次),所以这里只回答"有几个、是哪几个",
    问句和串步骤由界面那边一次做完。
    名字过不了校验的直接跳过:那种输入本来就跑不起来,不该在这里先炸一次。
    """
    out = []
    for name in picked_datasets(datasets):
        try:
            d = resolve_under(data_root, name)
        except ValueError:
            continue
        if dataset_format(d).get("needs_clips"):
            out.append(name)
    return out


#: 追问里最多点名几个数据集(再多就折成"…等 N 个",问句不该滚屏)。
_CLIPS_NAMES_CAP = 6


def clips_prompt(names: list, fmt: dict | None = None) -> str:
    """切片追问的那段话(纯函数,措辞钉在这儿好单测)。

    一个数据集时仍按格式说清**为什么**(v3 是多条合并进同一个 mp4,rrd 是视频封在
    数据文件内部)—— 那句话本来就在,客户读了才知道不是我们偷懒;多个数据集时改成
    "这 N 个"的统一问句并点名是哪几个,**只问一次、覆盖全部**(逐个弹窗选十个弹
    十次,没人会挨个读)。
    """
    names = picked_datasets(names)
    if not names:
        return ""
    if len(names) == 1 and fmt:
        kind = ("这份数据是 LeRobot v3 格式,多条轨迹合并存放在同一个视频文件里"
                if fmt.get("kind") == "lerobot" else
                "这份数据是 rerun(.rrd)格式,视频封装在数据文件内部")
        head = (f"**{kind}** —— 质检本身不受影响,但要在 Episodes 页逐条回看画面,"
                f"得先切出可播片段。")
        tail = "跳过不影响质检结果,只是 Episodes 页暂时看不到画面。"
    else:
        shown = "、".join(names[:_CLIPS_NAMES_CAP])
        more = f" 等 {len(names)} 个" if len(names) > _CLIPS_NAMES_CAP else ""
        head = (f"**这 {len(names)} 个数据集需要先切出可播片段,才能在 Episodes 页"
                f"看画面**:{shown}{more}。")
        tail = "跳过不影响质检结果,只是这几份的 Episodes 页暂时看不到画面。"
    return (f"{head}\n\n要在质检之后一起生成吗?会多花几分钟到十几分钟"
            f"(取决于条数);{tail}")


def suggest_delivery_name(dataset: str, now: datetime.datetime | None = None) -> str:
    """交付名建议:`<数据集名>-<月日>`。只管起名不管占用 —— 撞上老布局交付时由
    delivery_name_error 在**点按钮之前**拦下,并用 free_delivery_name 给出空闲的
    替代(此前指望 CLI 的输出目录冲突检查兜底,结果是任务起来几秒就"未完成
    (退出码 3)",原因埋在日志里,2026-08-14 用户实见)。"""
    now = now or datetime.datetime.now()
    base = re.sub(r"[^A-Za-z0-9._-]", "-", str(dataset or "dataset")).strip("-.") or "dataset"
    return f"{base[:60]}-{now.strftime('%m%d')}"


# ── 多 TOS 桶 + 深链解析(2026-08-17)──────────────────────────────────────
#
# 背景(测试同事提出、用户拍板):「数据集」下拉只扫一个 --data-root,它落在唯一
# 挂载的 TOS 桶里;而 rerun 深链传来的本是完整 tos://桶/前缀/数据集 URL,桶名被
# 丢掉只剩最后一段 —— 两个桶各有一个同名数据集时,会在默认桶里找到同名的那个、
# 一声不响跑错数据(不报错不提示,最坏的一类 bug)。这一节做两件事:
# ① TOS 桶从站点配置的白名单里选(tos_buckets/bucket_path):界面只传桶的内部
#    标识,永不传路径,resolve_under 那条安全边界一个字不松;
# ② 深链解析保留桶名并严格对表(parse_dataset_ref/prefill_plan):桶对不上就
#    明说,**绝不回落到默认桶里找同名的**。
# 目前只有一个桶,挂载层不动:配置没写 tos_buckets 时合成单桶,单桶部署的界面
# 与行为和只有 --data-root 的今天一致。
# 叫法演进(2026-08-17 用户两次当面纠偏,都别回退):
# ① 「数据源」→「TOS 桶」:抽象过头零信息量;配置键同步改 tos_buckets
#   (当天新加、尚未部署,零迁移成本;旧键 data_sources 出现要明确报错,不静默)。
# ② 「TOS 桶」→「数据集根目录」(界面标签):下拉实际选的是"到哪个根目录下去
#   列数据集",桶名含在显示串里;配置键 **tos_buckets 不再改** —— 那一段声明的
#   确实是桶及其数据集根,键名按声明对象命名、界面标签按用户所选对象命名。


def _bucket_label(alias: str, bucket, prefix, path: str) -> str:
    """「数据集根目录」下拉的显示文本(2026-08-17 用户两次纠偏,别回退:
    ① 显示「默认」零信息量,要看见数据在哪;② 早先的 `桶名:本地挂载路径` 把
    两套地址空间混成一串 —— `curation` 是桶、`datasets` 是桶内前缀,而
    `/mnt/tos/datasets` 是容器内挂载路径,冒号拼一起等于说 datasets 是桶名)。
    规则:
    - 基底 = `tos://桶名/桶内前缀`(前缀空 = 挂桶根,只印 `tos://桶名`);
      本地挂载路径**不进显示串**,它挪到下拉底下的只读说明行里;
    - 桶名声明了但前缀推导不出(配置缺 mount_root/tos_prefix)→ 如实写
      `tos://桶名(桶内前缀未知)`,绝不编一个前缀顶上(诚实弃权);
    - 桶名没声明 → 沿用 `(未声明桶):本地目录`:tos:// 地址一半都凑不出来,
      本地目录是唯一真话;
    - 别名只在带**额外信息量**时前缀成 `别名 · 基底`,三种情况不印:
      ① 配置没显式给 name(名是从路径末段推导的,零增量);
      ② 别名等于桶名(大小写不敏感,重复);
      ③ 别名是「默认」/「default」这类占位词(零信息,正是用户吐槽的那个)。
    显示文本**永远不当选中标识用**:下拉的 value 走 name,白名单查表在
    bucket_path —— 显示串传进去会被当伪造标识拒掉,这是有意的。"""
    if not bucket:
        base = f"(未声明桶):{path}"
    elif prefix is None:
        base = f"tos://{bucket}(桶内前缀未知)"
    else:
        base = f"tos://{bucket}/{prefix}" if prefix else f"tos://{bucket}"
    a = str(alias or "").strip()
    if (not a or (bucket and a.casefold() == str(bucket).casefold())
            or a.casefold() in {"默认", "default"}):
        return base
    return f"{a} · {base}"


def _bucket_prefix(item: dict, path: str) -> tuple:
    """一条 tos_buckets 配置 → (桶内前缀|None, 启动日志要点名的警告|None)。

    优先级(2026-08-17 定):① 显式 `tos_prefix`;② 由 `datasets_path` 相对
    `mount_root` 推导 —— 推导优于再写一遍,两处各写一份迟早漂移;③ 都没有 →
    None(前缀**未知**,不许猜:深链对表对它按"没法核对"拒,见 prefill_plan)。
    datasets_path 不在 mount_root 之下 = 配置写错 → 同样按未知处理并点名,
    别算出个负数层级的鬼东西。纯字符串比较,不碰文件系统(配置解析要能在
    没挂载的机器上跑)。
    """
    if "tos_prefix" in item:
        return str(item.get("tos_prefix") or "").strip().strip("/"), None
    mount = str(item.get("mount_root") or "").strip().rstrip("/")
    if not mount:
        return None, None
    p = str(path or "").rstrip("/")
    if p == mount:
        return "", None
    if p.startswith(mount + "/"):
        return p[len(mount) + 1:].strip("/"), None
    return None, (f"datasets_path {path!r} 不在 mount_root {mount!r} 之下,"
                  "推导不出桶内前缀 —— 按前缀未知处理,请改配置")


def bucket_info_line(b: dict) -> str:
    """根目录下拉底下的只读说明,两行(\\n 分隔,界面负责换行渲染):
    `端点:主机名` 与 `挂载路径:本地目录`。

    - 「(内网)/(公网)」标注已删(2026-08-17 用户实机点名):它描述的是
      **pod→TOS 的读取路径**,不是用户→界面的连接方式 —— 用户的浏览器根本
      不碰这个端点,看到「内网」会误以为在说自己怎么连的界面,回答了一个
      没人问的问题。别"顺手补全"再加回来。
    - 端点那行保留(2026-08-17 用户拍板):它是 tos://桶/前缀
      里唯一缺的那半截信息 —— 地域(主机名里含着 cn-beijing),挂载路径答
      不了"数据物理上从哪个地域读"。地域不单存字段:端点主机名已是事实来源,
      再存一份就是第二真相。
    - 端点**不做成可选下拉**(现在不需要,不是永远不):挂载是 PV 在 pod
      启动前定死的,运行时换端点不生效 —— 一个点了没用的控件比没有更糟
      (rerun 那边做成可选,是因为它每次现连 TOS 流式播放)。等做了"下载
      数据集"这类真在运行时发请求的功能,端点才变成真的选项,到时再改。
    - 配置没给 endpoint → 只印挂载那行(挂载路径始终是真话),**不编端点**
      (tos:// 里不含地域,没有就是没有);连挂载路径都没有 → 整段空。
    """
    ep = str(b.get("endpoint") or "").strip()
    mount = str(b.get("datasets_path") or "").strip()
    lines = []
    if ep:
        host = ep.split("://", 1)[-1].split("/", 1)[0]
        lines.append(f"端点:{host}")
    if mount:
        lines.append(f"挂载路径:{mount}")
    return "\n".join(lines)


def bucket_dropdown_choices(buckets: list) -> list[tuple]:
    """「数据集根目录」下拉的 (显示文本, 内部标识) 清单。

    没挂上的根在显示文本末尾标「⚠️ 未挂载」—— 部署事故要在选择时就看得见,
    不能等选中后发现下拉空了才猜。探测走 probe_dataset_root(带缓存)。
    """
    out = []
    for b in buckets:
        label = bucket_label(b)
        if probe_dataset_root(b.get("datasets_path") or "") == "unmounted":
            label += " ⚠️ 未挂载"
        out.append((label, b.get("name")))
    return out


def bucket_label(b: dict) -> str:
    """给人看的显示文本;字典没带 label(手搓的测试夹具/旧数据)回落 name。"""
    return str(b.get("label") or b.get("name") or "")


def tos_buckets(config_path: str | None = None, fallback_root: str | None = None,
                given_root: str | None = None) -> list[dict]:
    """站点配置的 `tos_buckets` 段 →
    [{"name", "bucket", "datasets_path", "tos_prefix", "endpoint", "label"}, …]。

    命名(2026-08-17 定,别再折腾):**键名按声明对象命名、界面标签按用户所选
    对象命名** —— 这一段声明的确实是"桶及其数据集根",键叫 tos_buckets 没错;
    而下拉实际选的是"到哪个根目录下去列数据集",界面标签叫「数据集根目录」。
    两个名字各说各的对象,不是不一致。

    tos_prefix = 桶内前缀(推导规则见 _bucket_prefix;None = 未知,深链对表
    对它不放行)。endpoint 只读展示用(bucket_info_line),不参与任何请求。

    配置没写这段 → 用 fallback_root 合成单桶(内部标识「默认」—— 那是 key
    不是给人看的,显示走 label)。合成桶的 bucket=None:桶名我们**不知道**,
    深链的 tos:// 校验对它按"认不出这个桶"处理并如实说明 —— 绝不假装挂载的
    就是链接里那个桶(诚实弃权,不硬猜)。

    第一个桶就是「默认桶」:裸名字深链与报告页兜底都落在它上面。
    given_root = 调用方显式给的 --data-root / CURATION_DATA_ROOT;配置里同时
    声明了 tos_buckets 时以配置为准,但两边对不上要在启动日志里点名 ——
    静默吃掉用户显式给的参数是另一种事故(只补空缺不覆盖那条纪律的镜像面)。

    旧键 `data_sources` 出现直接抛错点名新键名:静默当没配置会退化成合成单桶,
    进而让所有 tos:// 深链被拒为"未接入",那种错最难查(2026-08-17 改名时定)。
    """
    section = None
    for data in _config_layers(config_path):
        if "data_sources" in data:
            raise ValueError(
                "配置里出现旧键 data_sources:该段已改名 tos_buckets(2026-08-17),"
                "请改键名后重试 —— 不做静默兼容,旧键被当没配置会让所有 tos:// "
                "深链被拒,那种错最难查")
        if isinstance(data.get("tos_buckets"), list):
            section = data["tos_buckets"]        # 站点文件整段覆盖出厂(若有)
    out: list[dict] = []
    seen: set = set()
    for i, item in enumerate(section or []):
        if not isinstance(item, dict):
            print(f"[curation-ui] tos_buckets 第 {i + 1} 项不是键值映射,跳过",
                  file=sys.stderr)
            continue
        path = str(item.get("datasets_path") or "").strip()
        if not path:
            print(f"[curation-ui] tos_buckets 第 {i + 1} 项缺 datasets_path,跳过",
                  file=sys.stderr)
            continue
        alias = str(item.get("name") or "").strip()
        name = (alias or os.path.basename(path.rstrip("/")) or f"TOS 桶 {i + 1}")
        base, k = name, 2
        while name in seen:            # 名字是下拉的选中标识,必须唯一
            name, k = f"{base} ({k})", k + 1
        seen.add(name)
        bucket = str(item.get("bucket") or "").strip() or None
        prefix, warn = _bucket_prefix(item, path)
        if warn:
            print(f"[curation-ui] tos_buckets 第 {i + 1} 项:{warn}",
                  file=sys.stderr)
        endpoint = str(item.get("endpoint") or "").strip() or None
        out.append({"name": name, "bucket": bucket, "datasets_path": path,
                    "tos_prefix": prefix, "endpoint": endpoint,
                    "label": _bucket_label(alias, bucket, prefix, path)})
    if not out:
        root = str(fallback_root or "")
        return [{"name": "默认", "bucket": None, "datasets_path": root,
                 "tos_prefix": None, "endpoint": None,
                 "label": _bucket_label("", None, None, root)}]
    if given_root and given_root not in {b["datasets_path"] for b in out}:
        print(f"[curation-ui] 配置声明了 tos_buckets,而 --data-root/"
              f"CURATION_DATA_ROOT 给的 {given_root!r} 不在其中 —— 以配置为准,"
              f"该参数这次不生效", file=sys.stderr)
    return out


def bucket_path(buckets: list, name) -> str:
    """TOS 桶内部标识 → 该桶的 datasets_path。**这是安全边界**:界面传的是配置
    白名单里的标识,不是路径 —— 伪造成路径(`../../etc` 之类)或把下拉的显示
    文本当 key 传,在这里都对不上表,直接拒;数据集名照旧过 safe_name,路径仍
    由 resolve_under 拼。"""
    key = str(name or "").strip()
    for b in buckets:
        if b.get("name") == key:
            return b["datasets_path"]
    raise ValueError(f"未知 TOS 桶:{key!r}(只能从配置声明的清单里选)")


#: 深链认的 query 键,**全部走同一条解析路径**(裸名字 / 完整 tos:// URL 都吃)。
#: 只认 `dataset` 一个键太脆:rerun 侧换个键名,这边就一声不响什么都不做,
#: "深链没生效"和"深链生效但选错"一样难查。
DEEPLINK_KEYS = ("dataset", "dataset_url", "url")


def deeplink_values(qp) -> tuple:
    """query 参数 → (要预选的字符串列表, 有没有出现深链键)。

    多键并存时**按 DEEPLINK_KEYS 的顺序合并去重**,不是取其一 —— 取一丢一在
    两键并存时必丢信息,合并是唯一不丢东西的确定规则。第二个返回值给"不许
    静默无动作"用:键出现了(哪怕值是空的)界面就必须给出下文。
    qp 兼容 starlette 的 QueryParams(getlist)与普通 dict(测试用)。
    """
    out: list[str] = []
    present = False
    for key in DEEPLINK_KEYS:
        vals = (qp.getlist(key) if hasattr(qp, "getlist")
                else ([qp[key]] if key in qp else []))
        if vals:
            present = True
        for raw in vals:
            for s in str(raw or "").split(","):
                s = s.strip()
                if s and s not in out:
                    out.append(s)
    return out, present


#: 深链的端点键(2026-08-17 用户拍板:rerun 侧把「Open from Volcengine TOS」
#: 对话框里选的端点一并传过来)。认两个键名,教训同 DEEPLINK_KEYS:只认一个,
#: 对方换个写法这边就静默什么都不做 ——"深链没生效"和"深链选错"一样难查。
#: 可选参数:带不带都要能跑(rerun 侧什么时候加都不会坏这边)。
ENDPOINT_KEYS = ("endpoint", "tos_endpoint")

#: 消毒后主机名的字符集:字母数字与 . -,首尾必须字母数字(主机名合法字符,
#: 全小写后比)。长度上限 253 = DNS 主机名上限。
_ENDPOINT_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")

#: 主机名里认得出的地域段:cn-<xxx>(可带序号,如 cn-north-1)与
#: ap/us/eu-<xxx>-<n> 这类。认不出就是 None,调用方**跳过地域比对**,不许硬凑。
_REGION_RE = re.compile(
    r"(?<![a-z0-9])((?:cn|ap|us|eu)-[a-z]+(?:-\d+)?)(?![a-z0-9])")


def sanitize_endpoint(raw) -> str | None:
    """不可信的端点串 → 干净的主机名;消不干净返回 None(当没给)。

    ⚠️ 这个值来自 URL query,是**不可信输入**,且下游的提示语走 Markdown 组件
    渲染 —— 原样回显就是注入面。所以入界即消毒:只取主机名(丢 scheme/凭据/
    端口/路径/query),全小写,长度 ≤253,字符集只允许主机名合法字符;任何一步
    过不了都返回 None,**绝不把原样串透传出去**。

    ⚠️ 消毒后的主机名也**只用于显示与诊断,绝不用于读取任何数据**:本系统的
    读取路径只有"已挂载的本地目录"这一条(resolve_under 白名单拼路径)。谁要
    把这个值接进任何 fetch/请求,等于让 URL 参数指挥我们去连任意主机(SSRF),
    停手。
    """
    s = str(raw or "").strip()
    if not s or len(s) > 1024:          # 先掐总长,别拿超长串喂解析器
        return None
    from urllib.parse import urlsplit
    try:
        # 没写 scheme 的裸 host(rerun 端点下拉里就是裸主机名)补 // 走同一条
        # netloc 解析;.hostname 顺手丢掉 user:pass@ 与 :端口
        host = urlsplit(s if "://" in s else "//" + s).hostname
    except ValueError:                  # 形如 [x](… 的串会被当坏 IPv6 字面量
        return None
    host = str(host or "").strip().lower()
    if not host or len(host) > 253 or not _ENDPOINT_HOST_RE.fullmatch(host):
        return None
    return host


def endpoint_region(host) -> str | None:
    """主机名 → 地域段(如 tos-cn-beijing.ivolces.com → cn-beijing);认不出
    返回 None。宽容且不猜:只认 _REGION_RE 那几族写法,别的形态一律 None。"""
    m = _REGION_RE.search(str(host or "").lower())
    return m.group(1) if m else None


def deeplink_endpoint(qp) -> tuple:
    """query 参数 → (消毒后的端点主机名|None, 有没有出现端点键)。

    端点是标量不是清单,多键/多值并存时按 ENDPOINT_KEYS 的顺序取**第一个消得
    干净的** —— 确定规则,且前面的值脏、后面的干净时不丢可用信息。第二个返回
    值给界面提示"链接里的端点看不懂,已忽略"用(键出现了就不许静默无动作)。
    qp 兼容 starlette 的 QueryParams(getlist)与普通 dict(测试用)。
    """
    present = False
    for key in ENDPOINT_KEYS:
        vals = (qp.getlist(key) if hasattr(qp, "getlist")
                else ([qp[key]] if key in qp else []))
        if vals:
            present = True
        for raw in vals:
            host = sanitize_endpoint(raw)
            if host:
                return host, True
    return None, present


def _endpoint_conflict_note(src: dict, link_host: str) -> str | None:
    """链接端点 vs 本实例配置端点的地域比对;没矛盾(或没法比)返回 None。

    - 桶名全局唯一,同名桶不可能在两个地域 → 地域不同 = 链接与本站配置**必有
      一错**,要点名并把两个地域都印出来;但**只提示,不改变选中行为**;
    - 同地域只是内外网域不同(volces vs ivolces)是良性的:同一个桶、同一份
      数据,只是网络路径不同,不提示;
    - 任一侧地域认不出(endpoint_region → None)→ 跳过比对,不硬凑。
    """
    cfg_host = sanitize_endpoint(src.get("endpoint"))
    if not cfg_host or cfg_host == link_host:
        return None
    link_region, cfg_region = endpoint_region(link_host), endpoint_region(cfg_host)
    if not link_region or not cfg_region or link_region == cfg_region:
        return None
    return (f"链接给的端点({link_host},地域 {link_region})与本实例配置的"
            f"端点({cfg_host},地域 {cfg_region})不在同一地域 —— 桶名全局"
            "唯一,同名桶不可能同时在两个地域,链接和本站配置必有一处有误。"
            "本次仍按本实例配置读取,预选不受影响")


def parse_dataset_ref(raw: str) -> dict:
    """深链里的一个数据集引用 → {"bucket": 桶名|None, "prefix": 桶内前缀|None,
    "dataset": 名} 或 {"error": 给用户看的那句话}。

    两种写法都吃(rerun 侧改造前后各一种):裸名字(桶未知 → 回默认桶找,
    兼容今天的行为;prefix 一并 None)与完整 `tos://桶/前缀/数据集`。
    解析要稳且**不许猜**:
    - 桶名按 URL host 规则大小写不敏感(统一小写再比);前缀与数据集名大小写
      敏感(对象存储的 key 就是大小写敏感的);
    - prefix = 去掉最后一段后的中间路径,规范化成无首尾斜杠;数据集直接放在
      桶根时是空串 —— 空串是"确知在桶根",None 是"根本没给 URL",两回事;
    - 结尾多斜杠容忍(rerun 发来的就是带尾斜杠的;最后一个非空段才是数据集名);
    - %20 之类的 URL 编码先解码;解码后过不了 safe_name(空格、藏进来的 `/`)
      一律当无法解析并如实提示 —— 绝不猜一个"差不多"的名字。
    """
    s = str(raw or "").strip()
    if "://" not in s:
        return {"bucket": None, "prefix": None, "dataset": s}
    if not s.lower().startswith("tos://"):
        return {"error": f"只认识 tos:// 开头的数据集链接,这个解析不了:{s}"}
    from urllib.parse import unquote, urlsplit
    try:
        parts = urlsplit(s)
    except ValueError:
        return {"error": f"数据集链接解析不了:{s}"}
    bucket = unquote(parts.netloc or "").strip().lower()
    if not bucket:
        return {"error": f"链接里没有桶名,解析不了:{s}"}
    segs = [unquote(x).strip() for x in (parts.path or "").split("/") if x.strip()]
    if not segs:
        return {"error": f"链接只有桶名、没有数据集段,不知道要选哪个数据集:{s}"}
    name = segs[-1]
    try:
        safe_name(name)
    except ValueError as e:
        return {"error": f"链接里的数据集名不合法({e}),这个链接不处理:{s}"}
    return {"bucket": bucket, "prefix": "/".join(segs[:-1]), "dataset": name}


def prefill_plan(wanted: list, sources: list, lister=list_datasets,
                 link_endpoint: str | None = None) -> dict:
    """深链参数 → 预选计划(纯函数,界面只照着渲染)。sources = tos_buckets()
    的产出(参数名保留 sources:对本函数它就是"可选中的清单")。

    返回 {"source": 桶内部标识|None, "datasets": 预选列表, "choices": 该桶的
    最新数据集列表, "info": 预选成功的提示语, "notices": 逐条如实说明的警告}。
    文案里的桶一律用显示文本(bucket_label)—— 用户在下拉里看见的就是它,
    提示里印内部标识会对不上号。

    三条铁律:
    ① **桶不认识绝不回落到默认桶里找同名的** —— 本函数存在的理由:两个桶各有
       一个同名数据集时,静默跑错数据是最坏的一类 bug;
    ② 裸名字按今天的行为回默认桶(第一个)找,措辞逐字不变(兼容红线);
    ③ 没预选上任何东西时 notices 必不为空(不许静默无动作)。
    比对是**桶 + 桶内前缀两级**(2026-08-17 补第二级):只比桶时,
    `tos://curation/raw/x` 会静默匹配到我们挂的 `datasets/x` —— 和"桶不认识
    就回落"是同一族的错,只低一层(客户桶里 raw/ 与 curated/ 同名数据集很
    常见)。本实例前缀未知(配置缺 mount_root/tos_prefix)同样不放行:那是
    **本站配置不全**,不许因为"不知道"就当对上了。
    引用分属不同桶时同样不预选并明说:一个下拉一次只能停在一个桶上,
    替用户挑其中一个就是猜。

    link_endpoint(2026-08-17 加,可选,rerun 深链可能带 `endpoint`/
    `tos_endpoint`):**必须是 sanitize_endpoint 消毒过的主机名**,且只进提示
    文案 —— 绝不用于读取任何数据(读取路径只有已挂载的本地目录一条,见
    sanitize_endpoint 的 SSRF 注记)。三条语义:① 不带 → 措辞与旧版逐字
    一致;② 桶已接入且与本实例端点地域不同 → 补一条矛盾提示(只提示,不改
    选中);③ 桶未接入 → "未接入"那句后面补上链接给的端点,让人知道该去哪儿
    要权限。说明行的「端点:」永远印本实例配置的(bucket_info_line),链接的
    端点只出现在这两种提示里 —— 那一行回答的是"**我们**从哪儿读",链接给的是
    "**他们**从哪儿读",混淆就是新的一类假信息。
    """
    notices: list[str] = []
    if not sources:
        return {"source": None, "datasets": [], "choices": [], "info": "",
                "notices": ["本站没有配置任何 TOS 桶,链接无法预选"]}
    listing_cache: dict = {}

    def _list(i: int) -> list:
        if i not in listing_cache:
            listing_cache[i] = lister(sources[i]["datasets_path"])
        return listing_cache[i]

    pairs: list[tuple] = []              # (桶下标, 数据集名)
    bare_missing: list[str] = []
    endpoint_checked: set = set()        # 已做过端点地域比对的桶下标
    for raw in wanted:
        ref = parse_dataset_ref(raw)
        if ref.get("error"):
            notices.append(ref["error"])
            continue
        if ref["bucket"] is None:
            if ref["dataset"] in _list(0):
                pairs.append((0, ref["dataset"]))
            else:
                bare_missing.append(ref["dataset"])
            continue
        idx = next((i for i, s in enumerate(sources)
                    if str(s.get("bucket") or "").strip().lower() == ref["bucket"]),
                   None)
        if idx is None:
            note = (f"链接指向的桶「{ref['bucket']}」未接入本实例,没有预选"
                    f"「{ref['dataset']}」(不会在默认桶里找同名数据集)")
            if link_endpoint:
                # 未接入 + 链接给了端点:补上它,让人知道该去哪个地域要权限。
                # 没给端点时上面那句逐字不变(兼容红线)。
                note += f";链接标注的端点是 {link_endpoint}"
            notices.append(note)
            continue
        if link_endpoint and idx not in endpoint_checked:
            # 桶已接入:链接端点与本实例端点的地域比对,每个桶只提一次
            # (一条链接带多个同桶数据集时,矛盾是桶级的,不逐条刷屏)
            endpoint_checked.add(idx)
            conflict = _endpoint_conflict_note(sources[idx], link_endpoint)
            if conflict:
                notices.append(conflict)
        # 第二级对表:桶内前缀(空串=桶根也是一个明确的前缀,照比)
        cfg_prefix = sources[idx].get("tos_prefix")
        _p = lambda p: (f"{p}/" if p else "桶根")   # noqa: E731  文案里的前缀写法
        if cfg_prefix is None:
            notices.append(
                f"桶「{ref['bucket']}」已接入本实例,但本站配置没写清它的桶内"
                "前缀(tos_buckets 缺 mount_root 或 tos_prefix),没法核对链接"
                f"指向的「{_p(ref['prefix'])}」—— 没有预选「{ref['dataset']}」。"
                "这是本站配置不全,请补配置")
            continue
        if ref["prefix"] != cfg_prefix:
            notices.append(
                f"桶「{ref['bucket']}」已接入本实例,但只接入了"
                f"「{_p(cfg_prefix)}」,链接指向的「{_p(ref['prefix'])}」没有"
                f"接入 —— 没有预选「{ref['dataset']}」")
            continue
        if ref["dataset"] not in _list(idx):
            notices.append(f"桶「{ref['bucket']}」的数据集目录里没有"
                           f"「{ref['dataset']}」(TOS 桶「{bucket_label(sources[idx])}」)")
            continue
        pairs.append((idx, ref["dataset"]))
    if bare_missing:
        # 裸名字找不到的措辞与旧版逐字一致(数据集根 = 默认桶的目录)
        notices.append("链接里的数据集在本站找不到:"
                       f"{', '.join(bare_missing)}"
                       f"(数据集根 {sources[0]['datasets_path']})")
    picked = sorted({i for i, _ in pairs})
    if len(picked) > 1:
        span = "、".join(bucket_label(sources[i]) for i in picked)
        notices.append(f"链接里的数据集分属不同 TOS 桶({span}),一次只能预选"
                       "一个桶 —— 这次没有预选,请手动选择")
        pairs, picked = [], []
    if not pairs:
        if not notices:                  # 兜底:键出现了就不许一声不吭
            notices.append("链接带了数据集参数但没有可用的值,没有预选")
        return {"source": None, "datasets": [], "choices": [], "info": "",
                "notices": notices}
    idx = picked[0]
    hits: list[str] = []
    for _i, n in pairs:
        if n not in hits:
            hits.append(n)
    if idx == 0:                          # 默认桶:提示语与旧版逐字一致
        info = (f"已按链接选中数据集:{', '.join(hits)}。"
                "确认参数后点「开始质检」。")
    else:
        info = (f"已按链接选中 TOS 桶「{bucket_label(sources[idx])}」的数据集:"
                f"{', '.join(hits)}。确认参数后点「开始质检」。")
    return {"source": sources[idx]["name"], "datasets": hits,
            "choices": _list(idx), "info": info, "notices": notices}


# ── argv 构造:面板参数 → CLI 命令行 ──────────────────────────────────────

#: 每条命令允许的参数 → CLI 旗标。值为 None 表示"这是开关"(True 才发)。
#: 与 curation/cli.py 的 build_parser 逐条对齐;**run 的每一个参数都在**,
#: 因为用户要的是"CLI 能跑的面板都能跑",不是简化版。
_ARG_SPECS: dict[str, dict[str, str | None]] = {
    "run": {
        "config": "--config", "input": "--input", "output": "--output",
        "embodiment_id": "--embodiment-id", "max_episodes": "--max-episodes",
        "episodes": "--episodes", "only": "--only", "skip": "--skip",
        "vlm_backend": "--vlm-backend", "vlm_endpoint": "--vlm-endpoint",
        "vlm_model": "--vlm-model", "vlm_api_key_env": "--vlm-api-key-env",
        "run_name": "--run-name",
        "batch": None, "lite": None, "report_only": None,
    },
    "rejudge": {
        "delivery": "--delivery", "input": "--input", "config": "--config",
        "vlm_backend": "--vlm-backend",
    },
    "review-page": {
        "input": "--input", "output": "--output", "episodes": "--episodes",
        "max_episodes": "--max-episodes", "title": "--title", "rrd_fps": "--rrd-fps",
    },
    "backends": {"config": "--config", "timeout": "--timeout"},
}

#: 开关参数 → 旗标(单独一张表,因为 None 值在上表里表示"开关")。
#: ⚠️ 2026-08-14 起没有 `--overwrite` 了:每次跑批各进各的时间戳子目录,不存在
#: 撞车,也就不该有一个"覆盖"开关(覆盖曾把人工裁决一起 rmtree 掉)。
_FLAGS = {"batch": "--batch", "lite": "--lite", "report_only": "--report-only"}

#: 每条命令的必填项(缺了直接抛,不让用户等到子进程起来才看见报错)。
_REQUIRED = {"run": ("input", "output"), "rejudge": ("delivery", "input"),
             "review-page": ("input", "output"), "backends": ()}


def _check_module_list(value: str, field: str) -> str:
    """--only/--skip 的值:逗号分隔的**语义名**,逐个查表。

    提前校验的意义有两层:①拼错能当场说清可选项,不必等子进程报错;
    ②守住"M 编号不进用户界面"这条红线——m4a 之类在这里就会被拒。
    """
    names = [t.strip() for t in str(value).split(",") if t.strip()]
    if not names:
        raise ValueError(f"{field} 不能是空列表")
    bad = [n for n in names if n not in CHECK_LABELS]
    if bad:
        raise ValueError(
            f"{field} 里有未知模块名 {bad};可选:{sorted(CHECK_LABELS)}"
            "(只认语义名,M 编号不是合法输入)")
    return ",".join(names)


def build_argv(command: str, **params) -> list[str]:
    """面板参数 → 完整 argv(纯函数,本模块最该被测试钉死的一个)。

    规则:只发用户真给了的参数(None/空串/False 一律不出现);`set_overrides`
    是列表,展开成多个 `--set`;`--only` 与 `--skip` 互斥(CLI 也会拒,但那时
    子进程已经起来了,报错信息埋在日志里没人看见)。未知参数名直接抛——面板与
    CLI 的参数表迟早会漂,漂了要当场炸而不是静默丢掉一个用户勾了的选项。
    """
    if command not in COMMANDS:
        raise ValueError(f"未知命令 {command!r};可用:{list(COMMANDS)}")
    spec = _ARG_SPECS[command]
    allowed = set(spec) | ({"set_overrides"} if command == "run" else set())
    unknown = [k for k in params if k not in allowed]
    if unknown:
        raise ValueError(f"{command} 不认识这些参数:{sorted(unknown)};"
                         f"可用:{sorted(allowed)}")
    for req in _REQUIRED[command]:
        if not str(params.get(req) or "").strip():
            raise ValueError(f"{command} 缺必填参数 {req}")
    if params.get("only") and params.get("skip"):
        raise ValueError("--only 与 --skip 互斥,只能给一个")

    argv = [sys.executable, "-m", "curation.cli", command]
    for key in sorted(spec):                      # 排序:同样的输入永远得到同样的 argv
        value = params.get(key)
        if key in _FLAGS:
            if value:
                argv.append(_FLAGS[key])
            continue
        if value is None or str(value).strip() == "":
            continue
        if key in ("only", "skip"):
            value = _check_module_list(value, f"--{key}")
        argv += [spec[key], str(value).strip()]
    for item in (params.get("set_overrides") or []):
        s = str(item).strip()
        if not s:
            continue
        if "=" not in s:
            raise ValueError(f"--set 需要 '路径=值' 形式,got {s!r}")
        argv += ["--set", s]
    return argv


# ── 多数据集:一次点击顺序跑几个 ──────────────────────────────────────────

#: 多数据集串跑时,"有失败"这件事最多累计到这个退出码。
#: ⚠️ 为什么封顶在 125:shell 的退出码只有 0-255,而 126 / 127 / 128+n 是保留语义
#: (不可执行 / 命令找不到 / 被信号杀)。选了 10 个数据集全挂,退出码要是变成 128
#: 就会被读成"被信号杀了",与"人点了停止"(143)混在一起,那两件事在界面上必须
#: 分得开(见 STATE_STYLES 那段)。
MULTI_RUN_MAX_RC = 125


def _sq(s) -> str:
    """总是加单引号的 shell 转义。

    不用 shlex.quote:它对"看着安全"的串原样返回(`shlex.quote("abc") == "abc"`),
    而这里要把几段字面量与 `"$_rc"` 拼在一起,少一对引号就会粘成另一个词。
    """
    return "'" + str(s).replace("'", "'\\''") + "'"


def picked_datasets(value) -> list[str]:
    """下拉的值 → 数据集名列表。multiselect 给 list,单选给字符串,空值给 []。

    界面那边 Gradio 的 Dropdown 在 multiselect 前后返回类型不同,而回调有好几处要
    用它。归一化放在这里,免得每个回调各写一遍 isinstance 判断(漏一处就是"选了
    却没跑")。
    """
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(x).strip() for x in items if str(x or "").strip()]


def dataset_selection_error(value, batch: bool = False) -> str:
    """开跑前的数据集选择校验。通过返回空串,不通过返回给用户看的那句话。

    2026-08-13 数据集下拉改成多选之后**默认一个都没选**。此前是单选、默认落在
    第一个数据集上,所以"没选"这个状态根本不存在;改多选之后点「开始质检」会一路
    走到 resolve_under(root, "") —— 那解析出来正好是数据集根目录本身,等于悄悄
    拿整个根当一份数据集去跑。必须在这里拦住并说清楚。
    勾了「跑全部」时下拉本来就被忽略(既有行为),不拦。
    """
    if batch or picked_datasets(value):
        return ""
    return "还没选数据集:在上面的「数据集」里选至少一个,或勾上「跑全部」"


def build_run_script(jobs: list[dict]) -> str:
    """作业表 → 一条 shell 命令串(纯函数;退出码语义就钉在这里)。

    jobs = `[{"title": 分隔行上的名字(可为空), "steps": [argv, argv, …]}, …]`

    两种连接语义,**不是随便挑的**:
    - **一个 job 内部的多步用 `&&`**:后一步依赖前一步的产出(质检完顺便切片 ——
      质检没跑成,切片没有意义),前一步失败必须掐断。
    - **job 与 job 之间是"一个失败不挡后面"**:多数据集场景里一个坏数据集不该
      让排在后面的都不跑(与 CLI `--batch` 的"单集失败不拖垮整批"同一条道理)。

    **退出码:全成功 = 0;有失败 = 没跑完的数据集个数**(封顶 MULTI_RUN_MAX_RC)。
    定成个数而不是固定 1,是因为界面上那句"没跑到底(退出码 N)"里的 N 就直接是
    "几个数据集没跑完",不必再翻日志去数;是哪几个,日志里每个失败的数据集都有
    一行明说,末尾还有一行汇总。

    单 job 且没有名字(单数据集、含"质检+切片"那种)**一律走老路径**:退出码就是
    命令自己的。多数据集的语义不该倒灌回单数据集 —— 那是既有行为,报告页、历史
    列表、`test_start_spawns_bash_with_redirect_and_exit_code` 都按它写着。
    """
    if not jobs:
        raise ValueError("作业表不能为空")
    steps_of = (lambda job: " && ".join(shlex.join(str(a) for a in step)
                                        for step in job["steps"]))
    if len(jobs) == 1 and not str(jobs[0].get("title") or "").strip():
        return steps_of(jobs[0])

    parts = ["_failed=0"]
    for job in jobs:
        title = str(job.get("title") or "").strip()
        parts.append("echo " + _sq(f"===== {title} ====="))
        # `A || { …; }`:$? 在这里就是 A 的退出码(取完再自增计数,顺序不能反)
        parts.append(
            f"{{ {steps_of(job)}; }} || {{ _rc=$?; _failed=$((_failed+1)); "
            + "echo " + _sq(f"===== {title} 没跑完(退出码 ") + '"$_rc"'
            + _sq("),跳过它继续下一个 =====") + "; }")
    parts.append("echo " + _sq(f"===== {len(jobs)} 个数据集跑完,")
                 + '"$_failed"' + _sq(" 个没跑完 ====="))
    # 用子 shell 的 `exit` 而不是直接 `exit`:整串是被 `{ …; } >> 日志` 包着的,
    # 直接 exit 会把外层 bash 一起带走,后面那句"把退出码写进文件"就再也不执行了
    # —— 而"退出码文件在不在"正是本模块判定任务终结的唯一依据(见模块 docstring)。
    parts.append(f"( exit $(( _failed > {MULTI_RUN_MAX_RC} "
                 f"? {MULTI_RUN_MAX_RC} : _failed )) )")
    return "; ".join(parts)


def build_dataset_jobs(data_root: str, delivery_root: str, datasets: list,
                       delivery_name: str, *, clips_root: str | None = None,
                       clips_for: list | None = None, **params) -> list[dict]:
    """多个数据集 → 作业表(纯函数,路径校验与 argv 装配都在这儿一次做完)。

    落盘形状:**交付名当父文件夹**,每个数据集一份子交付 `<交付名>/<数据集名>/`
    —— 与 CLI `--batch` 完全一致,报告页的递归发现本来就找得到这种形状,不必为
    多选再造一套目录约定。

    路径仍然是"只收名字、由这里拼"(resolve_under 那条安全边界一个字没松):
    数据集名与交付名各过一次校验,子交付再在父目录下拼一次。

    clips_for 里的数据集额外串一步切片(同 job 内用 `&&`:质检没跑成,切片没有
    意义)。片段站按**数据集名**建目录,不按交付名:站点认领本来就看 site.json 里
    的源数据集路径(review_clip_paths),按数据集名建的话同一份数据跑第二次交付时
    还能直接复用,不必重编一遍码。
    """
    names = picked_datasets(datasets)
    if not names:
        raise ValueError("至少要选一个数据集")
    want_clips = set(picked_datasets(clips_for or []))
    if want_clips and not clips_root:
        raise ValueError("要串切片就得给片段目录(本实例没配 --review-dir)")
    parent = resolve_under(delivery_root, delivery_name or "")
    jobs = []
    for name in names:
        src = resolve_under(data_root, name)
        steps = [build_argv("run", input=src, output=resolve_under(parent, name),
                            **params)]
        if name in want_clips:
            steps.append(build_argv("review-page", input=src,
                                    output=resolve_under(clips_root, name)))
        jobs.append({"title": name, "steps": steps})
    return jobs


# ── 开跑前的交付名校验(2026-08-14 布局切换的善后)──────────────────────

def delivery_targets(delivery_root: str, name: str, datasets,
                     batch: bool = False) -> list[str]:
    """这次「开始质检」会往哪些交付目录里落结果(纯函数)。

    形状必须与真正要跑的一致:单数据集/勾「跑全部」= 交付名本身;多选 = 交付名当
    父文件夹,每个数据集一份子交付(与 build_dataset_jobs 拼的 `--output` 逐一
    对应)。单独拎出来是因为"检查的路径"与"真正写的路径"两套算法各写各的迟早
    漂移 —— 这里是检查侧的唯一出口,与作业侧的一致性由测试钉死
    (test_delivery_name_error_multi_select_checks_exactly_the_job_outputs)。
    """
    picked = picked_datasets(datasets)
    if batch or len(picked) <= 1:
        return [resolve_under(delivery_root, name or "")]
    parent = resolve_under(delivery_root, name or "")
    return [resolve_under(parent, n) for n in picked]


def free_delivery_name(delivery_root: str, base: str, datasets=None,
                       batch: bool = False,
                       now: datetime.datetime | None = None) -> str:
    """基于 suggest_delivery_name 找一个**当前没被占用**的交付名(用户照抄就能跑)。

    "空闲"从严:这次要落的每一个目标目录都不存在才算(只挑掉老布局还不够 ——
    建议一个已有内容的名字,等于把用户往另一个意外里送)。同名被占就往后试
    `-2`、`-3`……;封顶之后退到带时分秒的名字,那基本不可能撞(封顶是为了别让
    一个病态目录树把界面卡死在死循环里)。
    """
    now = now or datetime.datetime.now()
    first = suggest_delivery_name(base, now)
    for k in range(1, 100):
        cand = first if k == 1 else f"{first}-{k}"
        if not any(os.path.exists(t)
                   for t in delivery_targets(delivery_root, cand, datasets, batch)):
            return cand
    return f"{first}-{now.strftime('%H%M%S')}"


def delivery_name_error(delivery_root: str, name: str, datasets,
                        batch: bool = False) -> str:
    """开跑前的交付名校验(同 dataset_selection_error:通过返回空串)。

    2026-08-14 用户实见:交付名填了一份老布局交付(passed.json 直接在目录根上),
    任务真的起来了,几秒后「未完成(退出码 3)」,原因要翻日志才看得到,且说的是
    `--output` —— 界面上那个框叫「交付名」,用户从没见过这个旗标。这个判断在点
    按钮之前完全做得出来,就该在这里拦;CLI 里那道同名检查(pipeline/run.py)是
    CLI 直用者的最后一道,界面这条路走不到它。

    老布局判据借 curation/delivery.py 的 is_legacy_delivery(布局契约的单一事实源,
    与 deliveries_root_of 同一条口子,不是 import 管道代码)。名字本身不合法时
    返回空串 —— 那由既有的 argv 校验去报,这里不抢话、不重复造一套措辞。
    """
    from ..delivery import is_legacy_delivery
    try:
        targets = delivery_targets(delivery_root, name, datasets, batch)
        parent = resolve_under(delivery_root, name or "")
    except ValueError:
        return ""
    bad = [t for t in targets if is_legacy_delivery(t)]
    # 多选时 targets 是各个**子**交付,父目录不在其中 —— 但父目录本身要是一份老
    # 交付,几份新交付就会嵌进它肚子里(报告页把父目录当老交付读,嵌在里面的反倒
    # 成了它的一部分)。CLI 那道也拦不住:它查的是 `<父>/<数据集>/passed.json`。
    # 所以这里要**额外**查一次父目录 —— 它不是落盘目标,只是必须干净的容器。
    parent_bad = len(targets) > 1 and is_legacy_delivery(parent)
    if not bad and not parent_bad:
        return ""
    n = str(name or "").strip()
    if len(targets) == 1 or parent_bad:
        what = (f"交付名「{n}」是 2026-08-14 之前老布局的一份交付"
                f"(结果直接放在目录根上)")
    else:
        hit = "、".join(os.path.basename(t) for t in bad)
        what = (f"交付名「{n}」下的子交付撞上了老布局交付:{hit}"
                f"(2026-08-14 之前跑出来的,结果直接放在目录根上)")
    sugg = free_delivery_name(delivery_root, n, datasets, batch)
    return (f"{what}。新版每次跑批各进一个时间戳子目录,往老交付里跑会和它打架,"
            f"这次的结果也不会出现在报告页的「运行」列表里。老交付原样留着,"
            f"报告页照常打得开 —— 换个新交付名即可,比如「{sugg}」。")


# ── 任务目录:落盘的状态机 ────────────────────────────────────────────────

def runs_root_of(delivery_root: str) -> str:
    return os.path.join(delivery_root, RUNS_DIRNAME)


def deliveries_root_of(delivery_arg: str) -> str:
    """`curation ui --delivery` 收的可能是**一份交付**,也可能是**装着多份交付的
    父目录**。新交付与任务目录都该落在父目录下——塞进某一份交付里面,下次扫描就会
    把它当成那份交付的一部分。

    "一份交付"两种形态都算(2026-08-14 布局变更):三件套直接在里面的老交付,
    以及底下摆着几次跑批子目录的新交付。判据借 curation/delivery.py 那一份
    (它是布局契约的单一事实源,不是管道代码,UI 那条红线不破)。
    """
    from ..delivery import is_delivery
    p = os.path.abspath(str(delivery_arg or "."))
    return os.path.dirname(p) if is_delivery(p) else p


def _paths(runs_root: str, run_id: str) -> dict:
    """一个 run 的全部文件路径:挂载那份(存档)+ 本地盘那份(活跃期)。

    两套路径必须在**同一处**算出来,读端、写端、互斥判断才不会各走各的 ——
    "本地看没有、挂载看有"会让界面显示空闲、一点开始却被拒(更糟的是反过来:
    两个批同时开跑,砸穿方舟配额)。
    """
    d = os.path.join(runs_root, run_id)
    ld = os.path.join(LOCAL_RUNS_ROOT, run_id)
    return {"dir": d, "cmd": os.path.join(d, "cmd.json"),
            "log": os.path.join(d, "run.log"), "status": os.path.join(d, "status.json"),
            "rc": os.path.join(d, "exit_code"),
            "local_dir": ld, "local_cmd": os.path.join(ld, "cmd.json"),
            "local_log": os.path.join(ld, "run.log"),
            "local_status": os.path.join(ld, "status.json"),
            "local_rc": os.path.join(ld, "exit_code")}


def _prefer_local(p: dict, key: str) -> str:
    """读哪一份:本地优先、挂载兜底。

    本地有 = 这个 run 是本机这轮起的,活跃期的日志/退出码只在本地(挂载上那份要
    等任务结束才归档过去);本地没有 = 历史 run,或换了 pod / 重建了容器,那就读
    挂载上的存档。两边都有时以本地为准 —— 它是活的那份。
    """
    local = p.get("local_" + key)
    if local and os.path.exists(local):
        return local
    return p[key]


#: 进程内写缓存 {绝对路径: 刚写下的内容} 与 {runs_root: 本进程起过的 run_id}。
#: ⚠️ 为什么必须有:交付根在 TOS 的 FSX 挂载上,**刚写完的文件读回来可能是空的**
#: (可见延迟约 20-60 秒,已在 decisions.py 上咬过一次)。任务台的节奏恰好最容易
#: 中招——写完 status.json 立刻就要渲染状态条,2026-08-13 首次真机点按钮时,界面
#: 就报了"找不到该任务的状态文件"(而任务其实跑得好好的)。读不到就退回缓存,
#: 窗口期内自愈;跨进程(新起的 rejudge/CLI)那时文件早已可见。
_WRITE_CACHE: dict = {}
_STARTED: dict = {}


def _read_json(path: str) -> dict:
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    if not data:                       # 文件缺席/空/半截 → 用本进程刚写下的那份
        return dict(_WRITE_CACHE.get(os.path.abspath(path), {}))
    return data


def _copy_text(path: str, blob: str) -> None:
    """先写容器本地临时文件,再**原子发布**到目标。

    ⚠️ 目标可能在 TOS 的 FSX 挂载上,那里**不能被库直写**(已咬过七次;savefig
    那次静默产出了零填充的坏文件)。临时文件落在系统临时目录 = 容器本地盘。

    ⚠️ **最后一步必须是 `os.replace`,不是 `shutil.copyfile`**(2026-08-19 第八例,
    也是同一个家族的第二次复发)。copyfile 用 `'wb'` 打开目标 = **就地截断再从头
    写**,而 TOS 的对象不支持就地覆盖 —— 于是第一次创建成功、之后每次更新都
    `PermissionError: [Errno 1] Operation not permitted`。现场:
    `/mnt/tos/deliveries/.runs/*/status.json` 归档失败,异常一路冒到任务面板的
    定时刷新里把它打死,界面就永远停在最后一帧(同事报的 issue #57「一直停在
    运行中」正是这么来的)。
    2026-08-14 第七次事故时 `export/safe_write._publish` 已经改对了,**这一处漏了**
    —— 所以这里直接复用那条通道,不再各写一份(重复实现正是漏修的原因)。
    """
    from ..export.safe_write import publish_file   # 惰性 import:避免模块级环依赖

    fd, tmp = tempfile.mkstemp(prefix=".curation-run-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        publish_file(tmp, path)      # 目标目录内临时名 + os.replace(原子)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_json(path: str, payload: dict, *, verify: bool = False) -> None:
    """整文件写。⚠️ 交付根可能在 TOS 的 FSX 挂载上——那里 O_APPEND 直接 EINVAL,
    多次写/回填也不行,只有"顺序整写"是安全的(与 decisions.py 同一批坑)。

    verify=True 用在**归档**那一下(任务落终态时写回挂载的那份):2026-08-14 实测,
    挂载上的 status.json 变成了 164 字节全 `\\0` 的坏文件,而它正是 pod 重启之后
    唯一的历史。所以写完回读一次,读不回或解析不了就重写一次,并往 stderr 留一行
    —— 归档静默坏掉是最不能忍的那种坏法。读取端本来就是本地优先(_prefer_local),
    这条只为保历史。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False, indent=1)
    _WRITE_CACHE[os.path.abspath(path)] = dict(payload)
    for attempt in (1, 2):
        _copy_text(path, blob)
        if not verify:
            return
        try:
            with open(path, encoding="utf-8") as f:
                if json.load(f):
                    return
            why = "读回来是空的"
        except (OSError, ValueError) as e:
            why = f"{type(e).__name__}: {e}"
        if attempt == 1:
            print(f"[curation-ui] 归档到 {path} 后读不回来({why});"
                  f"可能是挂载可见延迟,也可能是零填充坏文件 —— 重写一次",
                  file=sys.stderr, flush=True)
    print(f"[curation-ui] 归档到 {path} 重写后仍读不回来 —— 这个任务的历史"
          f"目前只在容器本地盘上", file=sys.stderr, flush=True)


def _write_state(p: dict, key: str, payload: dict, *, verify: bool = False) -> None:
    """cmd.json / status.json **两份都写**(挂载 + 本地),都是整文件写。

    为什么两份:①挂载那份是 pod 重启后的历史,不能少;②本地那份给 2 秒一次的
    轮询和互斥判断读 —— 不必每次都去问 FSX,也不吃它 20-60 秒的可见延迟(那正是
    _WRITE_CACHE 当初要存在的原因,本地盘把这个窗口彻底关掉)。
    两份都是顺序整写,FSX 那条纪律一个字没破;写日志才是必须挪走的那个(追加)。
    verify 只加在挂载那份、且只在归档(落终态)那一下 —— 见 _write_json。
    """
    # ⚠️ **两份都要兜住,尤其是挂载那份**(2026-08-19 现场教训)。原来只兜了本地
    # 那份,挂载那份的异常一路裸奔:status() 在任务落终态时会写归档,而它是被
    # **读**路径调用的(任务面板 2 秒一次的 _tk_tick → active_run → list_runs →
    # status)。于是挂载一写不动,整个刷新函数就崩 —— 面板永远停在最后一帧,
    # 界面表现就是「一直停在运行中」(issue #57)。
    # 判据很清楚:轮询读的是**本地那份**(见上面「为什么两份」),挂载那份纯粹是
    # 跨 pod 的历史 —— 历史写不下去是要报的事,但绝不该让人看不见当前状态。
    # ⚠️ 兜住不等于咽掉:两处失败都往 stderr 留一行,归档静默坏掉是最不能忍的坏法。
    try:
        _write_json(p[key], payload, verify=verify)
    except OSError as e:
        print(f"[curation-ui] 归档 {p[key]} 写不下去({type(e).__name__}: {e})"
              f" —— 这个任务的历史目前只在容器本地盘上,界面状态不受影响",
              file=sys.stderr, flush=True)
    try:
        _write_json(p["local_" + key], payload)
    except OSError as e:
        print(f"[curation-ui] 本地状态 {p['local_' + key]} 写不下去"
              f"({type(e).__name__}: {e}) —— 挂载那份仍在,读端会兜底",
              file=sys.stderr, flush=True)


def _read_state(p: dict, key: str) -> dict:
    return _read_json(_prefer_local(p, key))


def _local_ids_for(runs_root: str) -> set:
    """本地盘上属于**这个** runs_root 的 run_id。

    本地目录按 run_id 平铺(不按交付分层),光看目录名分不出谁是谁 —— 同一个 pod
    上开两份交付时会串味。所以认 cmd.json 里记着的 runs_root,不认目录名。
    """
    root = os.path.abspath(runs_root)
    out: set = set()
    try:
        names = os.listdir(LOCAL_RUNS_ROOT)
    except OSError:
        return out
    for rid in names:
        owner = str(_read_json(os.path.join(LOCAL_RUNS_ROOT, rid,
                                            "cmd.json")).get("runs_root") or "")
        if owner and os.path.abspath(owner) == root:
            out.add(rid)
    return out


def _new_run_id(runs_root: str, command: str, now: datetime.datetime) -> str:
    """run_id 格式不变。本地目录也要一起查重:两个 run 撞上同一个号会共用一份
    本地日志,那比挂载上目录重名更难看出来。"""
    base = f"{now.strftime('%Y%m%d-%H%M%S')}-{command}"
    rid, k = base, 2
    while (os.path.exists(os.path.join(runs_root, rid))
           or os.path.exists(os.path.join(LOCAL_RUNS_ROOT, rid))):
        rid = f"{base}-{k}"
        k += 1
    return rid


def new_run_id(runs_root: str, command: str,
               now: datetime.datetime | None = None) -> str:
    """预先要一个任务编号(给"跑批目录名要跟任务编号对得上"用)。

    2026-08-14 起 `curation run` 的结果落在 `<交付>/<时间戳>/`,而这个时间戳就取自
    任务编号的时间戳部分(见 delivery.run_name_of_run_id)—— 于是面板必须**先**拿到
    编号才能拼 argv,不能等 start() 里再生成。start() 收下这个编号照用。
    """
    return _new_run_id(runs_root, command, now or datetime.datetime.now())


#: /proc 的根。单独拎成常量只为一个理由:用例里造不出真僵尸,得能把它指到假目录。
PROC_ROOT = "/proc"


def _proc_stat_fields(pid: int) -> list:
    """`/proc/<pid>/stat` 里 comm 之后的字段(state, ppid, pgrp, …);读不到给 []。

    ⚠️ 第二个字段 comm 是**带括号的进程名,里面可能有空格甚至右括号**,所以只能从
    **最后一个** `)` 之后开始切,`split()[2]` 那种写法在进程名带空格时会读错位。
    """
    try:
        with open(os.path.join(PROC_ROOT, str(int(pid)), "stat"),
                  encoding="utf-8", errors="replace") as f:
            line = f.read()
    except (OSError, ValueError, TypeError):
        return []
    return line[line.rfind(")") + 1:].split()


def _alive(pid: int) -> bool:
    """进程还在不在。**僵尸(Z)一律算死的。**

    ⚠️ 2026-08-14 事故的第二根支柱:本 pod 的 PID 1 就是这个 UI(一个 Python
    进程),而 Python 不会 wait() 领养来的子进程 —— 父进程一死,子进程过继给 PID 1
    之后就**永远是僵尸**(实测重起 UI 也清不掉)。僵尸的 `kill(pid, 0)` 照样成功,
    于是"停止"按下去之后状态永远停在「正在停止」。
    读不到 /proc(非 Linux、或没挂 proc)才退回原来的 `kill(pid, 0)`。
    """
    if not pid:
        return False
    fields = _proc_stat_fields(pid)
    if fields:
        return fields[0].upper() != "Z"
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _group_alive(pgid: int) -> bool:
    """这个进程组里还有**活着(非僵尸)**的成员吗。

    停止时判的是"这一刀砍干净了没有":组长可能已经变僵尸,而跑批 fork 出来的解码/
    VLM 子进程还在烧方舟的钱 —— 只看组长会把"还在跑"读成"已经停了"。
    /proc 用不了就退回只看组长(_alive 自己还会再退一档到 kill)。
    """
    if not pgid:
        return False
    try:
        names = [n for n in os.listdir(PROC_ROOT) if n.isdigit()]
    except OSError:
        return _alive(pgid)
    seen = False
    for name in names:
        fields = _proc_stat_fields(int(name))
        if len(fields) < 3:
            continue
        seen = True
        if fields[0].upper() == "Z":
            continue
        try:
            if int(fields[2]) == int(pgid):
                return True
        except ValueError:
            continue
    return False if seen else _alive(pgid)


def start(runs_root: str, command: str, argv: list, label: str = "", *,
          cwd: str | None = None, now: datetime.datetime | None = None,
          popen=subprocess.Popen, alive=_alive,
          then_argv: list | None = None,
          jobs: list | None = None, run_id: str | None = None) -> str:
    """起一个后台任务,返回 run_id。已有任务在跑 → RunBusyError。

    真正 exec 的是 `bash -c '<命令> >> 本地/run.log 2>&1; echo $? > 本地/exit_code;
    cp 回挂载'`:①日志重定向交给 shell,父进程不必守着管道(UI 是常驻进程,守管道
    等于自找阻塞);②退出码落盘,任务终结与否在 pod 重启后依然作数(见模块
    docstring);③重定向**必须指向本地盘**——挂载上追加着写的文件读回来是空的
    (见 LOCAL_RUNS_ROOT),日志与进度条会全程空手。
    `start_new_session=True` 让它自成进程组——停止时才能整组 kill,不会漏掉
    子孙进程(跑批会 fork 出解码/VLM 线程池)。
    """
    now = now or datetime.datetime.now()
    # 互斥只挡"真的还在跑"的任务:被 pod 重启带走的(interrupted)不该永远占着位子
    active = active_run(runs_root, alive=alive)
    if active:
        raise RunBusyError(active)
    # run_id 可以由调用方预先要好(new_run_id):跑批目录名取的就是它的时间戳部分,
    # 得在拼 argv 之前定下来。给进来的编号万一已被占用(两个人同时点),照常另取一个。
    if not run_id or os.path.exists(os.path.join(runs_root, run_id)):
        run_id = _new_run_id(runs_root, command, now)
    p = _paths(runs_root, run_id)
    os.makedirs(p["dir"], exist_ok=True)
    os.makedirs(p["local_dir"], exist_ok=True)
    started = now.strftime("%Y-%m-%d %H:%M:%S")
    # runs_root 记进 cmd.json:本地目录按 run_id 平铺,靠这个字段认领主(_local_ids_for)
    _write_state(p, "cmd", {"run_id": run_id, "command": command,
                            "argv": [str(a) for a in argv],
                            "label": label or COMMAND_LABELS.get(command, command),
                            "started_at": started, "cwd": cwd or os.getcwd(),
                            "runs_root": os.path.abspath(runs_root),
                            **({"then_argv": [str(a) for a in then_argv]}
                               if then_argv else {}),
                            **({"jobs": [{"title": j.get("title") or "",
                                          "steps": [[str(a) for a in s]
                                                    for s in j["steps"]]}
                                         for j in jobs]} if jobs else {})})
    # 串命令(2026-08-13):一次点击可能是"质检完顺便切视频片段"(两步一个 job),
    # 也可能是"顺序跑几个数据集"(几个 job)。两种连接语义的差别与理由见
    # build_run_script —— 用户看到的仍是一个任务、一条日志、一个结果。
    jobs = jobs or [{"title": "",
                     "steps": [argv] + ([then_argv] if then_argv else [])}]
    joined = build_run_script(jobs)
    # 收尾(写退出码 + 归档回挂载)拎成一个函数,因为它有**两个**入口:正常跑完,
    # 以及被停止信号打断。归档是整份 cp(顺序整写)不是追加,**先日志后退出码** ——
    # 退出码文件是"任务已终结"的信号,它在挂载上出现时日志必须已经是完整的。
    # cp 的报错不吞:它会落到 UI 进程的 stderr(pod 日志),归档失败要看得见。
    #
    # ⚠️ trap 是 2026-08-14 那次"永远停不掉"的正解:停止时杀的是整个进程组(跑批会
    # fork 出解码/VLM 子进程,只杀组长会留下一地孤儿继续烧钱),而外层这个 bash 也
    # 在组里 —— 没有 trap 它收到 TERM 当场就退,`echo $? > exit_code` 永远执行不到,
    # 于是那个"任务已终结的唯一依据"永远不出现,界面就卡在「正在停止」。
    # bash 在等前台子进程时会把信号处理推迟到子进程结束之后,而子进程被同一刀砍掉,
    # 所以 trap 一定跑得到;跑完显式 exit,不让它接着往下跑下一个数据集。
    shell = (f"_fin() {{ echo \"$1\" > {shlex.quote(p['local_rc'])}; "
             f"cp -f {shlex.quote(p['local_log'])} {shlex.quote(p['log'])}; "
             f"cp -f {shlex.quote(p['local_rc'])} {shlex.quote(p['rc'])}; }}; "
             f"trap '_fin 143; exit 143' TERM INT; "
             f"{{ {joined}; }} >> {shlex.quote(p['local_log'])} 2>&1; _fin $?")
    proc = popen(["/bin/bash", "-c", shell], cwd=cwd,
                 stdin=subprocess.DEVNULL, start_new_session=True)
    _write_state(p, "status", {"state": "running",
                               "pid": int(getattr(proc, "pid", 0) or 0),
                               "started_at": started, "finished_at": None,
                               "exit_code": None, "note": ""})
    # 目录本身在 FSX 上也可能还没可见 → 记一笔,list_runs 会把它并进来。
    # 不记的话:刚发起的任务在列表里查无此人,互斥形同虚设(能连点两次发起)。
    _STARTED.setdefault(os.path.abspath(runs_root), []).append(run_id)
    return run_id


def _read_rc(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _rc_finished_at(path: str) -> str | None:
    """退出码文件的落盘时刻 = 任务**真正**结束的时刻。

    为什么不能用"发现它结束的那一刻":状态是被轮询出来的,谁先来看谁算数——
    2026-08-13 真机实测,一个几秒跑完的探活因为隔了一会儿才有人刷新,历史里写着
    "5 分 54 秒"。耗时是给人判断快慢的,不能取决于观众什么时候到场。
    """
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def status(runs_root: str, run_id: str, *, alive=_alive,
           now: datetime.datetime | None = None) -> dict:
    """任务当前状态(终态会就地固化回 status.json,历史列表因此便宜且稳定)。

    判定顺序是有讲究的:**退出码文件优先**,它在就说明任务真的结束了;之后才看
    进程死活。反过来的话,没被 reap 的僵尸会让任务永远显示"运行中"。
    状态里还带上 `pid 已消失但没有退出码` 这一种——那正是 pod 重启(PID 1 就是
    UI)把跑批带走的样子,如实叫 `interrupted`,不假装还在跑。
    """
    p = _paths(runs_root, run_id)
    st = _read_state(p, "status")
    if not st:
        return {"run_id": run_id, "state": "unknown", "pid": 0,
                "started_at": None, "finished_at": None, "exit_code": None,
                "note": "找不到该任务的状态文件"}
    cmd = _read_state(p, "cmd")
    # n_jobs 透出来,状态条才能把退出码翻成人话(多数据集时退出码 = 没跑完的个数,
    # 见 build_run_script)。只读不写盘:它是 cmd.json 的事实,不是状态的一部分。
    out = {"run_id": run_id, "command": cmd.get("command", ""),
           "label": cmd.get("label", ""), "argv": cmd.get("argv", []),
           "n_jobs": len(cmd.get("jobs") or []),
           **st}
    if st.get("state") in ("done", "failed", "stopped", "interrupted"):
        return out                                   # 已固化的终态,不再改写

    rc_path = _prefer_local(p, "rc")
    rc = _read_rc(rc_path)
    stopping = st.get("state") == "stopping"
    if rc is not None:
        out["state"] = "stopped" if stopping else ("done" if rc == 0 else "failed")
        out["exit_code"] = rc
    elif alive(st.get("pid")):
        out["state"] = "stopping" if stopping else "running"
        return out                                   # 还活着:不写盘,省得每次轮询都写
    else:
        out["state"] = "stopped" if stopping else "interrupted"
        if not stopping:
            out["note"] = "进程已不在但没有退出码,多半是服务重启把它带走了"
    out["finished_at"] = (_rc_finished_at(rc_path) if rc is not None else None) \
        or (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    # 落终态 = 归档:这一份是 pod 重启之后唯一的历史,写完必须回读校验一次
    # (2026-08-14 实测挂载上那份变成了 164 字节全 \0 的坏文件)。
    # ⚠️ 用 .get 不用 [] :status() 是**读**路径(任务面板 2 秒一跳会走到这儿),
    # 状态文件缺一个字段就抛 KeyError,异常一路冒到刷新函数把面板打死 —— 与
    # 归档写失败同一类失败,症状都是「一直停在运行中」。start() 现在写的是全字段,
    # 但老版本留下的、或写了一半的状态文件不该有这个能力把界面弄哑。
    _write_state(p, "status", {k: out.get(k) for k in
                               ("state", "pid", "started_at", "finished_at",
                                "exit_code", "note")}, verify=True)
    return out


def list_runs(runs_root: str, limit: int = 50, *, alive=_alive) -> list[dict]:
    """任务列表,新的在前(run_id 以时间戳打头,字典序即时间序)。

    三个来源并起来:挂载上的目录(历史,含别的 pod 跑的)、本地盘上认领了本
    runs_root 的目录(本机这轮的,挂载可能还没可见)、本进程起过的(连本地目录
    都还没落稳的那一瞬)。少任何一路都会出现"任务查无此人",而互斥就是查这张表。
    """
    started = _STARTED.get(os.path.abspath(runs_root), [])
    local_ids = _local_ids_for(runs_root)
    try:
        seen = set(os.listdir(runs_root))
    except OSError:
        seen = set()
    seen |= set(started) | local_ids            # 见上:FSX 目录可见延迟
    ids = sorted(seen, reverse=True)
    out = []
    for rid in ids:
        if not (os.path.isdir(os.path.join(runs_root, rid))
                or rid in started or rid in local_ids):
            continue
        out.append(status(runs_root, rid, alive=alive))
        if len(out) >= limit:
            break
    return out


def active_run(runs_root: str, *, alive=_alive) -> dict | None:
    """当前在跑的那个(没有则 None)。互斥与界面禁用按钮都用它。"""
    for r in list_runs(runs_root, limit=20, alive=alive):
        if r.get("state") in ("running", "stopping"):
            return r
    return None


def is_busy_state(st) -> bool:
    """这份任务状态算不算"正在占着跑道"(发起按钮该不该置灰,issue #55)。

    ⚠️ **fail-open**:形状不对、读出来是垃圾,一律算"不忙" —— 判忙的唯一后果是
    禁用发起按钮,而误禁比误放贵得多:误放最多让人点一下、被 start 的互斥闸拦下
    并拿到那句"在等谁"的黄字;误禁则是按钮死在灰色上,没有任何一条路能把它救活
    (#57 那条读路径上周刚断过一次,别再把界面的命押在它上面)。
    """
    try:
        return bool(st) and st.get("state") in ("running", "stopping")
    except Exception:  # noqa: BLE001  st 不是 dict 等一切意外:按不忙算
        return False


def tail_log(runs_root: str, run_id: str, max_bytes: int = 64_000) -> str:
    """日志尾部。**只读尾部**:跑批日志能到几十 MB,整个读进内存会把常驻的 UI
    进程拖垮(而界面上也只看得下最后几十行)。

    活跃期读的是本地盘那份(挂载上的存档要等任务结束才写);历史任务本地没有了,
    退回读挂载。见 _prefer_local。"""
    path = _prefer_local(_paths(runs_root, run_id), "log")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                  # 丢掉被切半的首行
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


#: stop() 发完信号后最多等这么久(秒),等的是**退出码文件出现** —— 那是外层
#: shell 的 trap 写的,正常情形下一两百毫秒就到。等不到就自己写终态(见 stop)。
STOP_GRACE_S = 3.0
_STOP_POLL_S = 0.1


def stop(runs_root: str, run_id: str, *, killer=os.killpg,
         sig: int = signal.SIGTERM, group_alive=_group_alive,
         grace_s: float = STOP_GRACE_S, sleep=time.sleep,
         now: datetime.datetime | None = None) -> dict:
    """停止任务:杀**整个进程组**(跑批会 fork 出解码/VLM 线程与子进程,只杀
    组长会留下一地孤儿继续烧方舟的钱)。

    先落 `stopping`,让随后的 status() 把 bash 写下的退出码(被 TERM 打断通常
    是 143)解读成"人停的"而不是"跑失败了"——这两者在界面上必须分得开。

    ⚠️ **不能干等退出码文件**(2026-08-14 现场:用户点了停止,七分钟后仍是「正在
    停止」,而 `exit_code` 根本不存在)。外层 shell 现在装了 trap,正常情况下它会
    把退出码写出来;但那是"应该",不是"保证"——SIGKILL、shell 被 OOM 掉、老版本
    留下的任务都不会有 trap。所以这里发完信号短暂等一下,**一旦进程组里没有活着的
    成员就自己写终态**,不指望一个可能永远不出现的文件。
    """
    p = _paths(runs_root, run_id)
    st = _read_state(p, "status")
    if not st or st.get("state") not in ("running", "stopping"):
        return status(runs_root, run_id)
    st["state"] = "stopping"
    st["note"] = "已请求停止"
    _write_state(p, "status", st)
    pid = int(st.get("pid") or 0)
    if pid:
        try:
            killer(pid, sig)                   # start_new_session ⇒ pid 即 pgid
        except (OSError, ProcessLookupError):
            pass                               # 已经自己退了:交给下面判定
    rc_path = _prefer_local(p, "rc")
    for _ in range(max(1, int(grace_s / _STOP_POLL_S))):
        if _read_rc(rc_path) is not None:
            break                              # trap 写下了退出码:正常路径
        if not group_alive(pid):
            sleep(_STOP_POLL_S)                # 再给一拍:组刚散,退出码可能正在落盘
            break
        sleep(_STOP_POLL_S)
    if _read_rc(rc_path) is None and not group_alive(pid):
        # 进程组已经没有活着的成员,退出码文件却不会再出现了 —— 自己落终态。
        # exit_code 记 None 而不是补一个 143:没拿到就是没拿到,note 里说清楚
        # (界面对「已停止(人工)」本来就不显示退出码,补一个数只会是假证据)。
        finished = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        _write_state(p, "status",
                     {"state": "stopped", "pid": pid,
                      "started_at": st.get("started_at"), "finished_at": finished,
                      "exit_code": None,
                      "note": "已停止:进程组里已经没有活着的进程了,"
                              "但这次没留下退出码(停止信号把外层 shell 一起带走了)"},
                     verify=True)
    return status(runs_root, run_id)


# ── 进度:解析管道自己打的那些行 ──────────────────────────────────────────

def run_output_dir(st: dict) -> str | None:
    """已结束的质检跑批任务 → 它产出的那次跑批目录(argv 里的 --output/--run-name)。

    给「跑完提醒还有裁决没应用」用:拿不准(老任务没记 --run-name、目录还没
    可见)就返回 None —— 宁可不提醒,也不能对着别的跑批说话。多数据集串跑时
    argv 是第一个作业的,提醒也只覆盖它:部分覆盖仍比闭嘴强,但绝不指错目录。
    """
    if str((st or {}).get("command") or "") != "run":
        return None
    argv = [str(x) for x in (st or {}).get("argv") or []]

    def _val(flag: str) -> str:
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return ""

    out = _val("--output")
    if not out:
        return None
    run_name = _val("--run-name")
    cand = os.path.join(out, run_name) if run_name else ""
    if cand and os.path.isdir(cand):
        return cand
    # run_name 撞车加了后缀 / 老布局交付:退回 latest 语义(任务刚 done,latest
    # 记的就是它);连 passed.json 都没有说明目录还没落稳,不提醒。
    from ..delivery import resolve_run
    p = resolve_run(out)
    return p if p and os.path.exists(os.path.join(p, "passed.json")) else None


def source_dataset_of(delivery_dir: str) -> str | None:
    """交付目录 → 它是从哪个源数据集跑出来的(绝对路径)。

    `rejudge` 必须回源重读画面(交付里只有报告没有视频),所以面板要能自动回填这个
    输入。管道从 2026-08-13 起把 `源数据集路径` 写进 passed.json;老交付没有这个
    字段 → 返回 None,由界面退回"让用户从数据集下拉里选",绝不猜一个路径。
    """
    from ..delivery import resolve_run
    p = _read_json(os.path.join(resolve_run(delivery_dir), "passed.json"))
    src = str(p.get("源数据集路径") or "").strip()
    return src if src and os.path.isdir(src) else None


def _config_layers(config_path: str | None = None) -> list[dict]:
    """生效配置的两层原始数据:出厂 `pipeline/default.yaml`,再叠站点文件
    (`--config` / `CURATION_CONFIG`),**后者覆盖前者**。

    读的是**配置文件这个数据**,不是 import 管道代码(UI 那条红线)。读不了/解析
    不了的那层直接跳过 —— 界面宁可少显示一点,也不要因为一个坏 YAML 整个打不开。
    """
    import yaml

    out: list[dict] = []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(here, "pipeline", "default.yaml"),
                 config_path or os.environ.get("CURATION_CONFIG") or ""):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


#: 三个并发旋钮 → 它们在配置里的位置。界面拿这三个数只做**占位符**("默认 32"),
#: 不做预填值 —— 预填等于把默认值抄进界面,以后改了 default.yaml 而界面还按老值
#: 发 `--set`,是静默的不一致(_sets 里"只发用户动过的"是同一条纪律的另一半)。
CONCURRENCY_PATHS = {
    "ep": ("pipeline", "vlm_episode_concurrency"),
    "fr": ("checks", "task_success", "vlm", "max_concurrency"),
    "cap": ("skill_profile", "caption_concurrency"),
}


def concurrency_defaults(config_path: str | None = None) -> dict:
    """{"ep"/"fr"/"cap": 生效配置里的并发默认值 | None}。

    读不到就是 None,界面退回"留空 = 用配置里的值"这句话。**绝不硬编码一个数字
    充数**:界面上印着 32 而配置其实是 8,用户会照着那个数做判断——不显示只是少
    一条信息,显示错的是给错信息(与"fps 猜 30"同一条禁令)。
    """
    out: dict = {k: None for k in CONCURRENCY_PATHS}
    for data in _config_layers(config_path):
        for key, path in CONCURRENCY_PATHS.items():
            node = data
            for step in path:
                node = node.get(step) if isinstance(node, dict) else None
                if node is None:
                    break
            if node is None:
                continue                 # 这一层没给这项 → 保留下层的值(整层覆盖是错的)
            try:
                out[key] = int(node)
            except (TypeError, ValueError):
                continue
    return out


#: 硬件字段当"型号"看的长度上限(去空白后)。最长的真型号也就 `NVIDIA H200 141GB`
#: 这个量级,超过它的多半是整句描述而不是型号。
_HARDWARE_MODEL_MAXLEN = 16

#: 整句描述的痕迹:型号里不会有逗号/分号/句号。
_SENTENCE_MARKS = re.compile(r"[,,。;;]")


def hardware_suffix(service_type: str, hardware: str) -> str:
    """(服务类型, 硬件字段) → 拼进下拉标签的那截硬件文字(不该拼就给空串)。

    声明了硬件就一律拼上(2026-08-13 用户点名:两台 8B 带着卡型、独一份的 32B 不带,
    同一排下拉里看着像漏了)—— 但**前提是它真是个型号**。方舟那条把
    `hardware` 写成「托管服务,硬件不可见(由服务商调度)」,直接拼出来就是
    「方舟 MaaS(托管服务) · doubao-… · 托管服务,硬件不可见(由服务商调度)」:
    "托管服务"说了两遍,后半句还是整句描述。两条判据各挡一种:
      ① 去空白后被 service_type 包含 = 只是把服务类型重说一遍;
      ② 太长 / 带句读 = 整句描述,不是型号。
    `NVIDIA H20` / `NVIDIA A30` 两条都不触发,照旧拼上 —— 那正是用户要的区分。
    """
    h = str(hardware or "").strip()
    if not h:
        return ""
    flat = re.sub(r"\s+", "", h)
    if flat and flat in re.sub(r"\s+", "", str(service_type or "")):
        return ""
    if len(flat) > _HARDWARE_MODEL_MAXLEN or _SENTENCE_MARKS.search(h):
        return ""
    return h


def vlm_backend_labels(config_path: str | None = None) -> dict:
    """{界面标签: 预设名} —— 面板下拉只显示**标签**,预设名留在后台。

    ⚠️ 预设名(ark / h20-8b / my-gpu-server 之类)是**内部代号,不进用户界面**
    (与"硬件型号不写死在代码里、由配置声明"同一条纪律;test_app_has_perf_tab
    直接钉着这条)。所以标签用配置里给的 service_type/hardware + 模型名拼,
    选中后由调用方映射回代号发给 CLI。

    读的是**配置文件这个数据**,不是 import 管道代码:出厂 default.yaml 一份,
    站点文件(--config / CURATION_CONFIG)一份,后者覆盖同名预设。占位示例
    (endpoint 里带 YOUR-)不进下拉——它注定不可达,列出来只会让人选错。
    """
    presets: dict = {}
    for data in _config_layers(config_path):
        for name, body in (data.get("vlm_backends") or {}).items():
            presets[str(name)] = body or {}

    out: dict = {}
    for name, body in presets.items():
        endpoint = str(body.get("endpoint") or "")
        if not endpoint or "YOUR-" in endpoint:
            continue
        model = str(body.get("model") or "").strip().split("/")[-1]
        kind = str(body.get("service_type") or "").strip()
        hardware = str(body.get("hardware") or "").strip()
        if not kind:                       # 配置没声明就退回端点主机名,仍不暴露代号
            kind = endpoint.split("//")[-1].split("/")[0]
        label = " · ".join(x for x in (kind, model,
                                       hardware_suffix(kind, hardware)) if x)
        while label in out:                # 仍撞车(硬件也没声明):补空格保序不露代号
            label += " "
        out[label] = name
    return out


# ── 渲染:给界面用的纯字符串函数(不 import gradio,可单测)──────────────────

#: 状态 → (中文, 主色, 底色)。人停的与没跑到底必须一眼分得开:一个是"你自己停的",
#: 一个是"它没跑完",混为一谈会让人以为系统不稳。
#: ⚠️ 非零退出码写「未完成」而不是「失败」(2026-08-13 用户定):质检这件事只有
#: 跑完与没跑完,"失败"这个词会让人以为数据判坏了、或者系统崩了 —— 实际最常见的
#: 情形是外部原因半路断了。颜色仍用红档,严重程度不降。
STATE_STYLES = {
    "running": ("运行中", "#1d4ed8", "#dbeafe"),
    "stopping": ("正在停止", "#92400e", "#fef3c7"),
    "done": ("已完成", "#166534", "#dcfce7"),
    "failed": ("未完成", "#991b1b", "#fee2e2"),
    "stopped": ("已停止(人工)", "#3730a3", "#e0e7ff"),
    "interrupted": ("被中断(服务重启)", "#92400e", "#fef3c7"),
    "unknown": ("未知", "#374151", "#f3f4f6"),
}


#: 进度条配色:进行中的阶段用主色,跑完的淡一档。
#: 淡一档而不是换个颜色:一列条里"哪根还在动"要一眼看出来,同时已完成的那几根
#: 也得留在原地 —— 它们就是"已经过了几关"这条信息本身(2026-08-13 用户要的)。
_BAR_LIVE, _BAR_DONE = "#2563eb", "#93c5fd"


def _progress_row(p: dict) -> str:
    """一个阶段一行:条 + 一行小字。宽高全随内容,不写死行数(阶段有几个画几行)。"""
    pct, done = p.get("pct"), bool(p.get("done"))
    if done:
        width, color, stripe = "100%", _BAR_DONE, ""
    elif pct is not None:
        width, color, stripe = f"{max(0, min(100, int(pct)))}%", _BAR_LIVE, ""
    else:
        # 无百分比 = 不装样子(phase_step 那种一步一次 LLM 大调用的阶段)
        width, color, stripe = "100%", _BAR_LIVE, ";opacity:.45"
    name = " · ".join(x for x in (p.get("section"), p.get("stage")) if x)
    detail = " · ".join(x for x in (
        name,
        f"{p['n']}/{p['total']}" if p.get("total") else None,
        (f"用时 {p['elapsed']}" if done else f"已用 {p['elapsed']}")
        if p.get("elapsed") else None,
        (None if done else (f"剩余 ~{p['eta']}" if p.get("eta") else None))) if x)
    return (f'<div style="margin-top:8px;height:10px;background:#e2e8f0;'
            f'border-radius:999px;overflow:hidden">'
            f'<div style="height:100%;width:{width};background:{color}{stripe}"></div>'
            f'</div><div style="margin-top:4px;color:#475569;font-size:12px">'
            f'{_esc(detail)}</div>')


def failed_suffix(st: dict) -> str:
    """历史表「状态」列跟在「未完成」后面的那截括号(纯函数)。

    与状态条的 failed_note 同源同话术:多数据集时退出码**就是**没跑完的数据集
    个数(build_run_script 定的语义),表里也要说人话 —— 状态条已经改口说「N 个
    数据集没跑完」,历史表却还写「未完成(退出码 3)」,同一件事两种说法,而"3"
    在多数据集语境下根本不是个错误码。退出码落在个数区间之外(整串被信号打断
    之类)时什么都不补:那时它不是个数,编一个出来比不说更糟。
    """
    rc = st.get("exit_code")
    if rc is None:
        return ""
    n = int(st.get("n_jobs") or 0)
    if n > 1:
        return f"({rc} 个数据集没跑完)" if isinstance(rc, int) and 1 <= rc <= n else ""
    return f"(退出码 {rc})"


def failed_note(st: dict) -> str:
    """未完成任务的那句说明(纯函数)。

    多数据集串跑时退出码**就是**没跑完的数据集个数(build_run_script 定的语义),
    可状态条照旧写「没跑到底(退出码 3)」—— 读起来像个神秘错误码,而它其实是
    条能直接看懂的信息。多数据集就说人话;单数据集维持原样(那里的退出码是
    CLI 自己的返回值,确实只有排障价值)。
    退出码落在个数区间之外(整串被信号打断之类)时不硬翻:那时它不是个数,
    编一个数字出来比留着码更糟。
    """
    rc = st.get("exit_code")
    n = int(st.get("n_jobs") or 0)
    if n > 1:
        if isinstance(rc, int) and 1 <= rc <= n:
            return f"{rc} 个数据集没跑完;是哪几个见下方日志"
        return "没跑到底;原因见下方日志末尾"
    return f"没跑到底(退出码 {rc});原因见下方日志末尾"


def status_html(st: dict | None, progress=None, extra: str = "") -> str:
    """当前任务的状态条 + **一列**进度条。没有任务 → 一句提示,不留空白。

    progress 收 parse_progress_all 的列表(每个阶段一条);也仍收单个 dict —— 那是
    parse_progress 的老形状,别处还在用,拆签名换不来任何东西。
    extra = 卡片末尾追加的一句提醒(2026-08-16:跑完还有人工裁决没应用时用),
    缺省空串 ⇒ 老调用一个字不变。
    """
    if not st:
        return ('<div style="padding:10px 12px;border-radius:8px;background:#f3f4f6;'
                'color:#374151">当前没有任务在跑。在下面选好参数,点「开始」即可。</div>')
    label, fg, bg = STATE_STYLES.get(str(st.get("state")), STATE_STYLES["unknown"])
    head = (f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
            f'background:{bg};color:{fg};font-weight:700">{label}</span>'
            f'<span style="margin-left:10px;color:#334155">'
            f'{_esc(st.get("label") or st.get("command") or "")}</span>'
            f'<span style="margin-left:10px;color:#64748b;font-size:12px">'
            f'{_esc(st.get("run_id") or "")}</span>')
    note = str(st.get("note") or "")
    if st.get("state") == "failed" and st.get("exit_code") is not None:
        note = note or failed_note(st)
    items = [progress] if isinstance(progress, dict) else list(progress or [])
    bar = "".join(_progress_row(p) for p in items if p)
    tail = (f'<div style="margin-top:6px;color:#64748b;font-size:12px">{_esc(note)}</div>'
            if note else "")
    # 提醒用琥珀色与 note 的灰区分开:note 是状态陈述,这句是要人去做一件事
    more = (f'<div style="margin-top:6px;color:#92400e;font-size:12px">{_esc(extra)}</div>'
            if extra else "")
    return (f'<div style="padding:10px 12px;border:1px solid #e2e8f0;border-radius:8px">'
            f'{head}{bar}{tail}{more}</div>')


def _esc(s) -> str:
    import html
    return html.escape(str(s if s is not None else ""))


def parse_progress(log_text: str) -> dict | None:
    """日志文本 → 最后一条能解析的进度。解析不出返回 None。

    这是"起子进程调 CLI"白捡的红利:管道本来就在打结构化进度行(见
    pipeline/progress.py),面板不必自己发明一套进度上报通道。

    **绝不编造**:认不出格式就返回 None,界面老老实实只滚日志。给不可预测的
    步骤配一个假进度条是骗人——卡住时用户还以为在动(这正是 progress.py 里
    阶段式显示不报百分比的同一条理由)。
    """
    best = None
    for line in str(log_text or "").splitlines():
        m = _PROGRESS_RE.match(line.strip())
        if not m:
            continue
        total = m.group("total")
        rest = m.group("rest") or ""
        stage = (m.group("stage") or "").strip(" :·") or m.group("src")
        pct = m.group("pct")
        n = int(m.group("n"))
        total_i = int(total) if total.isdigit() else None
        if pct is None and total_i:
            pct = str(round(100.0 * n / total_i)) if total_i else None
        el = _ELAPSED_RE.search(rest)
        eta = _ETA_RE.search(rest)
        best = {"stage": stage, "n": n, "total": total_i,
                "pct": int(pct) if pct is not None else None,
                "elapsed": el.group(1) if el else None,
                "eta": eta.group(1) if eta else None,
                "detail": rest.split("|")[0].strip()}
    return best


def parse_progress_all(log_text: str) -> list[dict]:
    """整段日志 → **按出现顺序**的阶段列表(一个阶段一条,同名取最后一次读数)。

    为什么要累积(2026-08-13 用户):只画最后一条进度的话,阶段一换进度条就归零
    重来 —— 等了半小时的人看到的是一根反复从头爬的条,判断不了"已经过了几关、
    还值不值得等"。累积之后跑完的阶段留在原地,新阶段追加一行。

    完成判定用两个信号,**硬的优先**:
    ① 条目式的 `n == total`(这是确定的);
    ② **后面出现了新阶段** —— 管道是顺序跑的,后一个阶段开打就说明前一个收工了。

    ⚠️ **百分比只认日志里明写的 `(N%)`,绝不由 n/total 反推**:phase_step 那种
    "一步 = 一次 LLM 大调用"的阶段步数可数、耗时却天差地别,反推等于给它编一条
    匀速前进的假条(progress.py 顶上那条纪律)。没有百分比的阶段照样占一行、照样
    显示 n/total,只是条不填色 —— 少说一句话,不说假话。

    分隔行(`===== 数据集名 =====`)当断句:此前的阶段全部收工,同名阶段从此另起
    一行。不断句的话,第二个数据集的「数值检查」会把第一个那行改回 5%,看着像
    跑完的活又倒回去了。
    """
    out: list[dict] = []
    index: dict[str, dict] = {}
    section = ""
    for raw in str(log_text or "").splitlines():
        line = raw.strip()
        sec = _SECTION_RE.match(line)
        if sec:
            for e in out:
                e["done"] = True
            index, section = {}, sec.group("name")
            continue
        m = _PROGRESS_RE.match(line)
        if not m:
            continue
        total = m.group("total")
        rest = m.group("rest") or ""
        stage = (m.group("stage") or "").strip(" :·") or m.group("src")
        n = int(m.group("n"))
        total_i = int(total) if total.isdigit() else None
        pct = m.group("pct")
        el = _ELAPSED_RE.search(rest)
        eta = _ETA_RE.search(rest)
        reading = {"stage": stage, "section": section, "n": n, "total": total_i,
                   "pct": int(pct) if pct is not None else None,
                   "elapsed": el.group(1) if el else None,
                   "eta": eta.group(1) if eta else None,
                   "detail": rest.split("|")[0].strip()}
        hit = index.get(stage)
        if hit is None:
            for e in out:
                e["done"] = True
            hit = dict(reading, done=False)
            out.append(hit)
            index[stage] = hit
        else:
            hit.update(reading)
        # 一旦满格就永远是完成态:后面若还有同名阶段的读数(重试/再跑一遍),
        # 也不该把一个已经跑满的条改回半截
        hit["done"] = hit["done"] or bool(total_i and n >= total_i)
    return out
