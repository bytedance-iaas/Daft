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
reap 会变成僵尸,而僵尸的 `os.kill(pid, 0)` 照样成功——只看进程死活会让任务
永远显示"运行中"。所以真正开的是 `bash -c '<命令> >> log 2>&1; echo $? > rc'`,
rc 文件在则任务已终结(且重启后依然作数),进程死活只用来判"是不是被打断了"。

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

#: 任务目录挂在交付根下。用点开头:交付根同时是 UI 的交付扫描根,`.runs` 不会被
#: 误当成一份交付(discover_deliveries 找的是 passed.json)。
RUNS_DIRNAME = ".runs"

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
        if r.get("state") == "failed" and r.get("exit_code") is not None:
            state = f"{state}(退出码 {r['exit_code']})"
        out.append([r.get("started_at") or "—",
                    r.get("label") or COMMAND_LABELS.get(r.get("command"), "—"),
                    state,
                    duration_text(r.get("started_at"), r.get("finished_at"), now),
                    r.get("run_id") or ""])
    return out


def list_datasets(data_root: str) -> list[str]:
    """数据集根下**一层**里的有效数据集名(不是路径)。

    判据与 cli._list_datasets 同款:有 `meta/info.json`(LeRobot)或目录里有
    `*.rrd`(rerun)。目录不存在/读不了 → 空列表(界面显示"没扫到",不崩)。
    """
    try:
        names = os.listdir(data_root)
    except OSError:
        return []
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
    return sorted(out)


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


def suggest_delivery_name(dataset: str, now: datetime.datetime | None = None) -> str:
    """交付名建议:`<数据集名>-<月日>`。重名由 CLI 的输出目录冲突检查兜(它会
    要求换目录或加覆盖),这里不去猜用户想不想覆盖。"""
    now = now or datetime.datetime.now()
    base = re.sub(r"[^A-Za-z0-9._-]", "-", str(dataset or "dataset")).strip("-.") or "dataset"
    return f"{base[:60]}-{now.strftime('%m%d')}"


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
        "overwrite": None, "batch": None, "lite": None, "report_only": None,
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
_FLAGS = {"overwrite": "--overwrite", "batch": "--batch", "lite": "--lite",
          "report_only": "--report-only"}

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


# ── 任务目录:落盘的状态机 ────────────────────────────────────────────────

def runs_root_of(delivery_root: str) -> str:
    return os.path.join(delivery_root, RUNS_DIRNAME)


def deliveries_root_of(delivery_arg: str) -> str:
    """`curation ui --delivery` 收的可能是**一份交付**,也可能是**装着多份交付的
    父目录**。新交付与任务目录都该落在父目录下——塞进某一份交付里面,下次扫描就会
    把它当成那份交付的一部分。"""
    p = os.path.abspath(str(delivery_arg or "."))
    if os.path.exists(os.path.join(p, "passed.json")):
        return os.path.dirname(p)
    return p


def _paths(runs_root: str, run_id: str) -> dict:
    d = os.path.join(runs_root, run_id)
    return {"dir": d, "cmd": os.path.join(d, "cmd.json"),
            "log": os.path.join(d, "run.log"), "status": os.path.join(d, "status.json"),
            "rc": os.path.join(d, "exit_code")}


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


def _write_json(path: str, payload: dict) -> None:
    """整文件写。⚠️ 交付根可能在 TOS 的 FSX 挂载上——那里 O_APPEND 直接 EINVAL,
    多次写/回填也不行,只有"顺序整写"是安全的(与 decisions.py 同一批坑)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    _WRITE_CACHE[os.path.abspath(path)] = dict(payload)


def _new_run_id(runs_root: str, command: str, now: datetime.datetime) -> str:
    base = f"{now.strftime('%Y%m%d-%H%M%S')}-{command}"
    rid, k = base, 2
    while os.path.exists(os.path.join(runs_root, rid)):
        rid = f"{base}-{k}"
        k += 1
    return rid


def _alive(pid: int) -> bool:
    """进程还在不在。⚠️ 只用来判"是不是被打断了" —— 子进程是本进程 fork 出来的,
    没 reap 时会变僵尸,而僵尸的 kill(pid, 0) 照样成功。完成与否一律以退出码
    文件为准(见模块 docstring)。"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def start(runs_root: str, command: str, argv: list, label: str = "", *,
          cwd: str | None = None, now: datetime.datetime | None = None,
          popen=subprocess.Popen, alive=_alive,
          then_argv: list | None = None) -> str:
    """起一个后台任务,返回 run_id。已有任务在跑 → RunBusyError。

    真正 exec 的是 `bash -c '<命令> >> run.log 2>&1; echo $? > exit_code'`:
    ①日志重定向交给 shell,父进程不必守着管道(UI 是常驻进程,守管道等于自找
    阻塞);②退出码落盘,任务终结与否在 pod 重启后依然作数(见模块 docstring)。
    `start_new_session=True` 让它自成进程组——停止时才能整组 kill,不会漏掉
    子孙进程(跑批会 fork 出解码/VLM 线程池)。
    """
    now = now or datetime.datetime.now()
    # 互斥只挡"真的还在跑"的任务:被 pod 重启带走的(interrupted)不该永远占着位子
    active = active_run(runs_root, alive=alive)
    if active:
        raise RunBusyError(active)
    run_id = _new_run_id(runs_root, command, now)
    p = _paths(runs_root, run_id)
    os.makedirs(p["dir"], exist_ok=True)
    started = now.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(p["cmd"], {"run_id": run_id, "command": command,
                           "argv": [str(a) for a in argv],
                           "label": label or COMMAND_LABELS.get(command, command),
                           "started_at": started, "cwd": cwd or os.getcwd(),
                           **({"then_argv": [str(a) for a in then_argv]}
                              if then_argv else {})})
    # 串命令(2026-08-13):质检完顺便切视频片段这类"一次点击两步活"用它。
    # `{ a && b; }` 语义:前一步失败就不做后一步,退出码取整组的——用户看到的仍是
    # 一个任务、一条日志、一个结果,不必理解我们内部跑了几条命令。
    steps = [argv] + ([then_argv] if then_argv else [])
    joined = " && ".join(shlex.join(str(a) for a in step) for step in steps)
    shell = (f"{{ {joined}; }} >> {shlex.quote(p['log'])} 2>&1; "
             f"echo $? > {shlex.quote(p['rc'])}")
    proc = popen(["/bin/bash", "-c", shell], cwd=cwd,
                 stdin=subprocess.DEVNULL, start_new_session=True)
    _write_json(p["status"], {"state": "running", "pid": int(getattr(proc, "pid", 0) or 0),
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
    st = _read_json(p["status"])
    if not st:
        return {"run_id": run_id, "state": "unknown", "pid": 0,
                "started_at": None, "finished_at": None, "exit_code": None,
                "note": "找不到该任务的状态文件"}
    cmd = _read_json(p["cmd"])
    out = {"run_id": run_id, "command": cmd.get("command", ""),
           "label": cmd.get("label", ""), "argv": cmd.get("argv", []),
           **st}
    if st.get("state") in ("done", "failed", "stopped", "interrupted"):
        return out                                   # 已固化的终态,不再改写

    rc = _read_rc(p["rc"])
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
    out["finished_at"] = (_rc_finished_at(p["rc"]) if rc is not None else None) \
        or (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    _write_json(p["status"], {k: out[k] for k in
                              ("state", "pid", "started_at", "finished_at",
                               "exit_code", "note")})
    return out


def list_runs(runs_root: str, limit: int = 50, *, alive=_alive) -> list[dict]:
    """任务列表,新的在前(run_id 以时间戳打头,字典序即时间序)。"""
    try:
        seen = set(os.listdir(runs_root))
    except OSError:
        seen = set()
    seen |= set(_STARTED.get(os.path.abspath(runs_root), []))   # 见上:FSX 目录可见延迟
    ids = sorted(seen, reverse=True)
    out = []
    for rid in ids:
        if not (os.path.isdir(os.path.join(runs_root, rid))
                or rid in _STARTED.get(os.path.abspath(runs_root), [])):
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


def tail_log(runs_root: str, run_id: str, max_bytes: int = 64_000) -> str:
    """日志尾部。**只读尾部**:跑批日志能到几十 MB,整个读进内存会把常驻的 UI
    进程拖垮(而界面上也只看得下最后几十行)。"""
    path = _paths(runs_root, run_id)["log"]
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


def stop(runs_root: str, run_id: str, *, killer=os.killpg,
         sig: int = signal.SIGTERM) -> dict:
    """停止任务:杀**整个进程组**(跑批会 fork 出解码/VLM 线程与子进程,只杀
    组长会留下一地孤儿继续烧方舟的钱)。

    先落 `stopping`,让随后的 status() 把 bash 写下的退出码(被 TERM 打断通常
    是 143)解读成"人停的"而不是"跑失败了"——这两者在界面上必须分得开。
    """
    p = _paths(runs_root, run_id)
    st = _read_json(p["status"])
    if not st or st.get("state") not in ("running", "stopping"):
        return status(runs_root, run_id)
    st["state"] = "stopping"
    st["note"] = "已请求停止"
    _write_json(p["status"], st)
    pid = int(st.get("pid") or 0)
    if pid:
        try:
            killer(pid, sig)                   # start_new_session ⇒ pid 即 pgid
        except (OSError, ProcessLookupError):
            pass                               # 已经自己退了:交给 status() 判定
    return status(runs_root, run_id)


# ── 进度:解析管道自己打的那些行 ──────────────────────────────────────────

def source_dataset_of(delivery_dir: str) -> str | None:
    """交付目录 → 它是从哪个源数据集跑出来的(绝对路径)。

    `rejudge` 必须回源重读画面(交付里只有报告没有视频),所以面板要能自动回填这个
    输入。管道从 2026-08-13 起把 `源数据集路径` 写进 passed.json;老交付没有这个
    字段 → 返回 None,由界面退回"让用户从数据集下拉里选",绝不猜一个路径。
    """
    p = _read_json(os.path.join(delivery_dir, "passed.json"))
    src = str(p.get("源数据集路径") or "").strip()
    return src if src and os.path.isdir(src) else None


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
    import yaml

    presets: dict = {}
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
        label = " · ".join(x for x in (kind, model) if x)
        if label in out and hardware:
            # 同类型同模型的两台机器(实测:8B 同时跑在两种卡上)——**用配置声明的
            # 硬件区分**,而不是加序号或露预设代号。硬件本来就是给界面看的描述字段。
            label = " · ".join(x for x in (kind, model, hardware) if x)
            prev = next((k for k in out if k == label.rsplit(" · ", 1)[0]), None)
            prev_hw = str((presets.get(out[prev]) or {}).get("hardware") or "") if prev else ""
            if prev and prev_hw:           # 把先来的那条也补上硬件,免得一个带一个不带
                out[" · ".join((prev, prev_hw))] = out.pop(prev)
        while label in out:                # 仍撞车(硬件也没声明):补空格保序不露代号
            label += " "
        out[label] = name
    return out


# ── 渲染:给界面用的纯字符串函数(不 import gradio,可单测)──────────────────

#: 状态 → (中文, 主色, 底色)。停止与失败必须一眼分得开:一个是"你自己停的",
#: 一个是"它崩了",混为一谈会让人以为系统不稳。
STATE_STYLES = {
    "running": ("运行中", "#1d4ed8", "#dbeafe"),
    "stopping": ("正在停止", "#92400e", "#fef3c7"),
    "done": ("已完成", "#166534", "#dcfce7"),
    "failed": ("失败", "#991b1b", "#fee2e2"),
    "stopped": ("已停止(人工)", "#3730a3", "#e0e7ff"),
    "interrupted": ("被中断(服务重启)", "#92400e", "#fef3c7"),
    "unknown": ("未知", "#374151", "#f3f4f6"),
}


def status_html(st: dict | None, progress: dict | None = None) -> str:
    """当前任务的状态条 + 进度条。没有任务 → 一句提示,不留空白。"""
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
        note = note or f"退出码 {st['exit_code']};原因见下方日志末尾"
    bar = ""
    if progress:
        pct = progress.get("pct")
        width = f"{max(0, min(100, int(pct)))}%" if pct is not None else "100%"
        stripe = "" if pct is not None else ";opacity:.45"      # 无百分比 = 不装样子
        detail = " · ".join(x for x in (
            progress.get("stage"),
            f"{progress['n']}/{progress['total']}" if progress.get("total") else None,
            f"已用 {progress['elapsed']}" if progress.get("elapsed") else None,
            f"剩余 ~{progress['eta']}" if progress.get("eta") else None) if x)
        bar = (f'<div style="margin-top:8px;height:10px;background:#e2e8f0;'
               f'border-radius:999px;overflow:hidden">'
               f'<div style="height:100%;width:{width};background:#2563eb{stripe}"></div>'
               f'</div><div style="margin-top:4px;color:#475569;font-size:12px">'
               f'{_esc(detail)}</div>')
    tail = (f'<div style="margin-top:6px;color:#64748b;font-size:12px">{_esc(note)}</div>'
            if note else "")
    return (f'<div style="padding:10px 12px;border:1px solid #e2e8f0;border-radius:8px">'
            f'{head}{bar}{tail}</div>')


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
