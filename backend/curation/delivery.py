"""交付目录的布局契约(2026-08-14 用户拍板的 breaking change,唯一事实源)。

**为什么改**:此前一份交付就是一个目录,同名重跑必须 `--overwrite`,而覆盖会
`rmtree` 掉整个 `details/` —— 人工裁决的三张 CSV 就住在里面。用户 2026-08-14 亲历:
只是想看看新报告长什么样而重跑一次,旧结果连同**人花时间产生的裁决**一起没了。
机器产出可以重跑,人的判断不能。

布局:

    <交付>/                     客户视角的一份成果
    ├── human-decisions/        人的产出(三张裁决 CSV),跨跑批累积
    ├── 20260814-074045/        一次跑批的全部机器产出(三件套/报告/details/数据集)
    ├── 20260814-131200/
    └── latest                  小文件,内容 = 最近一次跑批的目录名

三条纪律:
① 每次跑批各进各的子目录,**永不覆盖**;因此不再有 `--overwrite`,清理只走显式的
   `curation prune`(默认只列不删)。
② `latest` **只回答"最近跑的是哪一次"**,不含"你该用这份"的意思 —— 报告页默认
   打开它只是省一次点击。1812 跑 20 条不比 0530 跑 200 条更"对"。
③ 裁决 CSV 上提到交付根:人裁的是"这条 episode 到底怎么回事",数据没变时重跑
   一百次都还算数,不该和会被清理的机器产出混在一起。读取时新位置优先、老位置
   兜底(老交付不自动迁移 —— 不动别人的数据),写入一律写新位置。

本模块只依赖标准库:管道、UI、裁决三边都要用它,而"UI 不 import 管道代码"的红线
照旧(它不在 pipeline/ 下,也不 import 任何管道模块)。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import tempfile

#: 一次跑批的目录名 = 本地时区的 `YYYYMMDD-HHMMSS`(与任务台 run_id 同格式)。
#: ⚠️ 目录名里**不写时区缩写**:现在是夏令时,写死 "PST" 半年后就是错的。
RUN_NAME_FMT = "%Y%m%d-%H%M%S"

#: 目录名是不是"一次跑批"。允许尾巴(`-2` 是同秒撞车时的去重后缀)。
_RUN_NAME_RE = re.compile(r"^\d{8}-\d{6}(?:-[A-Za-z0-9._-]+)?$")

#: 最近一次跑批的记录文件(内容 = 子目录名,一行)。
LATEST_NAME = "latest"

#: 人工裁决 CSV 的新家(交付根下)。
HUMAN_DIR = "human-decisions"

#: 老位置:2026-08-14 之前裁决 CSV 与机器产出混在一起。只读不写。
LEGACY_DECISIONS_DIR = "details"

#: 一次跑批的事实卡(条数/是否导出数据集),给"选哪一次"的列表用。
#: 单独一个小文件是为了列清单时不必去读几 MB 的 passed.json(FSX 上一次几秒)。
RUN_FACTS_NAME = "run.json"

#: 认定"这是一份跑批结果"的标志文件(与 discover_deliveries 同一判据)。
MARKER = "passed.json"


def new_run_name(now: datetime.datetime | None = None) -> str:
    """现生成一个跑批目录名(CLI 单独跑时用)。本地时区。"""
    return (now or datetime.datetime.now()).strftime(RUN_NAME_FMT)


def run_name_of_run_id(run_id: str) -> str:
    """任务台的 run_id → 跑批目录名。

    run_id 形如 `20260814-074045-run`(尾巴是子命令名,同秒撞车时还会再加 `-2`)。
    跑批目录只取时间戳部分:目录名前缀与 run_id 前缀天然对得上,"哪次跑批产生了
    这份结果"不必再去翻日志;而 `-run` 这个内部词不该出现在客户看得见的目录名里。
    认不出格式的(测试造的假 id)原样返回,别悄悄编一个时间。
    """
    s = str(run_id or "").strip()
    m = re.match(r"^(\d{8}-\d{6})", s)
    return m.group(1) if m else s


def is_run_name(name: str) -> bool:
    return bool(_RUN_NAME_RE.match(str(name or "")))


def is_run_dir(path: str) -> bool:
    """这个目录是不是一次跑批的结果目录(名字对得上 + 里面有三件套之一)。"""
    return (is_run_name(os.path.basename(str(path or "").rstrip("/")))
            and os.path.exists(os.path.join(path, MARKER)))


def list_runs(delivery: str) -> list[str]:
    """交付下的全部跑批目录名,新的在后(名字以时间戳打头,字典序即时间序)。"""
    try:
        names = sorted(os.listdir(delivery))
    except OSError:
        return []
    return [n for n in names
            if is_run_name(n) and os.path.exists(os.path.join(delivery, n, MARKER))]


def is_legacy_delivery(path: str) -> bool:
    """老布局:三件套直接躺在交付目录里(2026-08-14 之前跑出来的交付)。"""
    return os.path.exists(os.path.join(str(path or ""), MARKER))


def is_delivery(path: str) -> bool:
    """这个目录是不是一份交付(老布局的本体,或新布局的交付根)。"""
    return bool(path) and (is_legacy_delivery(path) or bool(list_runs(path)))


def delivery_root_of(path: str) -> str:
    """一次跑批的目录 → 它属于哪份交付;传交付目录则原样返回。

    判据只看目录名格式(`20260814-074045`),因为调用方常常拿着一个**还不存在**的
    路径来问(裁决文件要写到哪儿)。副作用为零,可纯函数单测。
    """
    p = str(path or "").rstrip("/")
    return os.path.dirname(p) if is_run_name(os.path.basename(p)) else p


def run_dir(delivery: str, name: str) -> str:
    return os.path.join(delivery, name)


def allocate_run_dir(delivery: str, name: str | None = None,
                     now: datetime.datetime | None = None) -> str:
    """给本次跑批开一个新目录并返回绝对路径。**永不复用已存在的目录。**

    同名撞车(同一秒起了两次,或任务台重发了同一个 run_id)时加 `-2`/`-3` 后缀,
    而不是往里写 —— 往已有目录里写就又回到了"覆盖"的老路。
    """
    base = str(name or "").strip() or new_run_name(now)
    path = run_dir(delivery, base)
    k = 2
    while os.path.exists(path):
        path = run_dir(delivery, f"{base}-{k}")
        k += 1
    os.makedirs(path, exist_ok=True)
    return path


# ── latest:一条记录,不是一个推荐 ──────────────────────────────────────

def read_latest(delivery: str) -> str:
    """`latest` 记的那次跑批目录名;没有/读不到/指向不存在的目录 → 空串。"""
    path = os.path.join(str(delivery or ""), LATEST_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        return ""
    if not name or not os.path.isdir(os.path.join(delivery, name)):
        return ""
    return name


def write_latest(delivery: str, name: str) -> str:
    """记下最近一次跑批的目录名,返回写好的文件路径。

    写法 = 本地临时文件 + safe_write 原子发布(2026-08-20 改,FSX 直写坑家族
    第十例):此前是 copyfile,而 safe_write._publish 的实测早写明白了 ——
    **copyfile 发布的文件要 30-90s 以上才读得回来,os.replace 发布的立刻可读**。
    2026-08-14 修 _publish 时这里漏了,因为它自己抄了一份(与 runner._copy_text
    第八例同病);落盘回验里 latest/run.json 独慢那 10s~285s,病根就是它。

    仍不做立即回读校验:"读得回来且内容对"由调用方连同三件套一起走落盘回验
    (pipeline/run.py 的 _verify_delivery_visible),latest_matches() 是那关
    之后的最终对账。
    """
    from .export.safe_write import publish_file
    name = str(name or "").strip()
    if not name:
        raise ValueError("latest 要写的跑批目录名不能为空")
    dst = os.path.join(delivery, LATEST_NAME)
    os.makedirs(delivery, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="latest-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(name + "\n")
        publish_file(tmp, dst)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return dst


def latest_matches(delivery: str, name: str) -> bool:
    """latest 读回来是不是就是刚写的那个名字(读不到 = False,让调用方去警告)。"""
    path = os.path.join(str(delivery or ""), LATEST_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() == str(name or "").strip()
    except OSError:
        return False


def resolve_run(path: str, run: str | None = None) -> str:
    """要读哪个目录:交付根 → 某一次跑批;老布局交付 → 它自己。

    顺序:显式指定的那次 > `latest` 记的那次 > 最新的一次 > 老布局的本体。
    都没有就原样返回(调用方会挂"这不是一份交付"的错)。
    """
    p = str(path or "").strip()
    if not p:
        return p
    if run:
        return os.path.join(p, str(run))
    if is_legacy_delivery(p):
        return p
    latest = read_latest(p)
    if latest:
        return os.path.join(p, latest)
    runs = list_runs(p)
    return os.path.join(p, runs[-1]) if runs else p


# ── 人工裁决:新位置优先,老位置兜底 ──────────────────────────────────

def human_dir(path: str) -> str:
    """裁决 CSV 该写在哪个目录(传跑批目录或交付目录都行)。"""
    return os.path.join(delivery_root_of(path), HUMAN_DIR)


def decisions_write_path(path: str, filename: str) -> str:
    return os.path.join(human_dir(path), filename)


def decisions_read_path(path: str, filename: str) -> tuple:
    """读裁决 CSV 的路径 → (路径, 是不是老位置)。

    回退链:交付根 `human-decisions/`(新)→ 这一次跑批的 `details/`(老)→
    交付根的 `details/`(更老的布局)。老交付**不自动迁移**(不动别人的数据),
    只在真读到老位置时让调用方留一行日志。
    """
    new = decisions_write_path(path, filename)
    if os.path.exists(new):
        return new, False
    root = delivery_root_of(path)
    for legacy in (os.path.join(path, LEGACY_DECISIONS_DIR, filename),
                   os.path.join(root, LEGACY_DECISIONS_DIR, filename)):
        if os.path.exists(legacy):
            return legacy, True
    return new, False                       # 一个都不存在:按新位置返回(读端当空)


# ── 跑批清单:只陈述事实 ────────────────────────────────────────────────

def write_run_facts(run_path: str, facts: dict) -> str:
    """把这次跑批的事实卡写进跑批目录(条数/是否导出数据集/生成时间)。

    存在的理由是**列清单要快**:没有它,"选哪一次"的下拉得逐个去读几 MB 的
    passed.json,FSX 上一次就是几秒。写法同 latest(临时文件 + safe_write
    原子发布;2026-08-20 前是 copyfile —— FSX 直写坑家族第九例,copyfile 发布
    的文件要 30-90s 以上才读得回来,2026-08-20 那次落盘回验等 run.json 等了
    285 秒,病根就是它,详见 write_latest 的 docstring)。
    """
    from .export.safe_write import publish_file
    dst = os.path.join(run_path, RUN_FACTS_NAME)
    fd, tmp = tempfile.mkstemp(prefix="runfacts-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False, indent=1)
        publish_file(tmp, dst)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return dst


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


#: 导出数据集的三种落盘形态(有其一 = 这次跑批出了数据,不是"只出报告")。
_DATASET_DIRS = ("lerobot_curated", "rrd_curated", "episodes_parquet")


def run_facts(run_path: str) -> dict:
    """一次跑批 → {name, at, processed, dataset_total, exported}。

    事实卡缺席(老交付、被打断的跑批)时退回 passed.json 里已有的读数;两边都没有
    的字段一律给 None,**由渲染侧决定不说**,绝不编一个数字。
    """
    name = os.path.basename(str(run_path or "").rstrip("/"))
    f = _read_json(os.path.join(run_path, RUN_FACTS_NAME))
    out = {"name": name, "path": run_path,
           "at": str(f.get("生成时间") or ""),
           "processed": f.get("本次处理条数"),
           "dataset_total": f.get("数据集总条数"),
           "exported": f.get("导出数据集")}
    if out["processed"] is None or not out["at"] or out["exported"] is None:
        p = _read_json(os.path.join(run_path, MARKER))
        ds = p.get("dataset") or {}
        if out["processed"] is None:
            out["processed"] = ds.get("input_episodes")
        if out["dataset_total"] is None:
            out["dataset_total"] = ds.get("dataset_total_episodes")
        if not out["at"]:
            out["at"] = str(p.get("生成时间") or "")
        if out["exported"] is None:
            out["exported"] = any(os.path.exists(os.path.join(run_path, d))
                                  for d in _DATASET_DIRS)
    if not out["at"]:
        out["at"] = run_time_text(name)
    return out


def run_time_text(name: str) -> str:
    """跑批目录名 → 人读的时间;认不出格式就原样返回目录名。"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", str(name or ""))
    if not m:
        return str(name or "")
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def run_label(facts: dict, latest: str = "") -> str:
    """一次跑批在列表里显示成什么。**只陈述事实,不下判断。**

    ⚠️ 不许写"抽查"/"完整"这类词:用户只说了"跑前 20 条",没说他为什么 —— 系统
    不知道意图就不能替他下结论。数据集总条数拿得到才写"(数据集共 N 条)",拿不到
    就只写本次条数。"只出报告(无导出数据集)"是磁盘上看得见的事实,照说。
    """
    parts = [facts.get("at") or facts.get("name") or ""]
    n = facts.get("processed")
    if n is not None:
        total = facts.get("dataset_total")
        parts.append(f"本次处理 {n} 条" + (f"(数据集共 {total} 条)"
                                       if total not in (None, "") else ""))
    if facts.get("exported") is False:
        parts.append("只出报告(无导出数据集)")
    if latest and facts.get("name") == latest:
        parts.append("最近一次")
    return " · ".join(p for p in parts if p)


def run_choices(delivery: str) -> list:
    """交付 → 「运行」下拉的 [(显示文本, 目录路径)],**新的在前**。

    老布局的交付(三件套直接在交付目录里)只有一项 —— 它必须仍然打得开。
    """
    root = str(delivery or "").rstrip("/")
    names = list_runs(root)
    if not names:
        return [(run_label(run_facts(root)), root)] if is_legacy_delivery(root) else []
    latest = read_latest(root)
    out = [(run_label(run_facts(os.path.join(root, n)), latest),
            os.path.join(root, n)) for n in names]
    out.reverse()
    return out


# ── prune:先列出,默认不删 ─────────────────────────────────────────────

def dir_size(path: str) -> int:
    """目录占用字节数(符号链接不跟随,读不到的条目跳过)。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            total += st.st_size
    return total


def size_text(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"


def prune_plan(delivery: str, keep_latest: int | None = None) -> dict:
    """要删哪几次跑批的清单。**本函数只算不删**(删的动作在 CLI,且要 --yes)。

    保留口径只有一条(最简单那条):`--keep-latest N` = 留最新的 N 次。另外
    **`latest` 记的那次永远保留**,哪怕它不在最新 N 次里 —— 它是报告页默认打开的
    那份,悄悄删掉等于让人打开一个空页。

    keep_latest 为 None = 只列出,不做任何删除计划(默认行为)。
    """
    root = str(delivery or "").rstrip("/")
    names = list_runs(root)
    latest = read_latest(root)
    rows = []
    for n in names:
        p = os.path.join(root, n)
        f = run_facts(p)
        f["size"] = dir_size(p)
        f["is_latest"] = (n == latest)
        rows.append(f)
    rows.reverse()                                   # 新的在前
    delete: list = []
    if keep_latest is not None:
        if keep_latest < 1:
            raise ValueError("--keep-latest 至少是 1(不允许把一份交付删空)")
        for i, f in enumerate(rows):
            if i >= keep_latest and not f["is_latest"]:
                delete.append(f)
    keep_names = {f["name"] for f in delete}
    return {"delivery": root, "runs": rows,
            "delete": delete,
            "keep": [f for f in rows if f["name"] not in keep_names],
            "latest": latest}
