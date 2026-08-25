"""交付目录 → UI 数据模型(纯函数层,2026-07-27 U1)。

架构红线:UI 只读交付目录,不 import 管道代码——本模块是"运行清单"契约的
读端,管道换底座 UI 不动。所有函数无副作用、不碰网络,Gradio 层只做渲染。

读的文件(U0 盘点定型的交付 schema):
  passed.json   数据集元信息 + dataset 统计 + skills + label_audit +
                config_effective + runtime(后端/硬件/容器配额)+
                dataset.vlm_latency(分桶延时)+ 通过条目(checks 含双重编码 detail)
  reject.json   被拒条目(+原因)
  review.json   待人工裁决条目 + 标注-画面分歧复核队列(旧交付键名:标注审计复核队列)
  details/evidence/<ep>/*.jpg   task_success probe 证据帧
  details/plots/<ep>_sync.png   同步曲线证据图
"""
from __future__ import annotations

import glob
import json
import os


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_detail(detail) -> dict:
    """检查 detail:交付里是双重编码 JSON 字符串,UI 层统一解开。"""
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str) and detail.strip():
        try:
            d = json.loads(detail)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {"raw": detail}          # 解不开也不丢:原文进 raw
    return {}


def _norm_checks(checks: dict) -> dict:
    out = {}
    for name, c in (checks or {}).items():
        out[name] = {"state": c.get("结果", "?"), "score": c.get("score"),
                     "detail": parse_detail(c.get("detail"))}
    return out


# 路径压根不是一份交付时,manifest 里挂的那句话(渲染侧据此整页只说"读不到")。
# 2026-08-13 实测的事故:交付下拉允许手输,用户打了半截字("droid")再点选项,
# 输入框里留下的是那半截字 —— 当相对路径读,三个 JSON 全空,页面渲成一具壳子
# (机器人 None、交付 ?),看着像系统坏了。读不到就说读不到,不留半空的壳。
def _load_error(path: str) -> str:
    return (f"路径 `{path}` 下没有质检结果 —— 它不是一份交付,也不是一次跑批。\n\n"
            "从上面的下拉里挑一份;要填自己的路径,得填交付目录的**完整路径**"
            "(只在框里打半截字不算,那是搜索用的)。")


def resolve_delivery(value, candidates) -> str:
    """交付下拉里的值 → 真正要读的目录(纯函数)。

    2026-08-13 实测:下拉是可搜索的(allow_custom_value,手输自定义路径是既有
    能力,不许砍),而"打了半截字再点选项"之后,输入框里留下的是**打的那串**
    ("droid-200-full")而不是选中的完整路径 —— 当相对路径读就什么也读不到。
    这里只补一种情形:那串字**正好等于**某一份已发现交付的目录名,且全库**只有
    一份**同名。这不是猜(目录名精确相等 + 唯一性验过);对不上、或者有两份同名
    的,一律原样返回,交给 load_delivery 挂 load_error 明说读不到 —— 绝不从几个
    候选里挑一个"最像的"塞给用户,那会让人看着别人的报告以为是自己的。
    """
    from ..delivery import is_delivery
    v = str(value or "").strip()
    if not v or is_delivery(v):
        return v
    hits = [c for c in (candidates or [])
            if os.path.basename(str(c).rstrip("/")) == v]
    return hits[0] if len(hits) == 1 else v


def data_sig(run_path: str, delivery_root: str = "") -> str:
    """这次跑批的数据指纹:关键文件的 (mtime_ns, size) 串。

    给「切回报告页要不要整页重载」当判据(2026-08-25 droid-50 实战复盘 ②):
    CLI rejudge / 另一个会话改了交付后,页面里的 manifest 还是旧的,此前只能
    ⌘R 整页刷新。指纹只 stat 不读内容,十来个文件微秒级,页签切换随手一算。
    盖到的面 = manifest 会变的全部来源:三件套 JSON、三张裁决 CSV(新位置)、
    技能归属 CSV(rejudge 的画像同步会改它)。文件不存在记 (0,0) —— "从无到有"
    也是变化。
    """
    names = [os.path.join(run_path or "", n)
             for n in ("passed.json", "review.json", "reject.json")]
    names.append(os.path.join(run_path or "", "details", "skill_assignment.csv"))
    hd = os.path.join(delivery_root or "", "human-decisions")
    names += [os.path.join(hd, n) for n in
              ("label_decisions.csv", "task_verdicts.csv", "reject_appeals.csv")]
    parts = []
    for p in names:
        try:
            st = os.stat(p)
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("0:0")
    return ";".join(parts)


def load_delivery(path: str) -> dict:
    """一次跑批的目录 → 统一 manifest。缺文件按空处理(老交付也能打开)。

    传交付目录也行:按 `latest` 记的那次(没有记录就取最新的一次)解析 —— 默认
    打开最近一次只是省一次点击,不含"这份最好"的意思。老布局的交付(三件套直接
    在交付目录里)解析成它自己,一个字不用改就还能打开。

    ⚠️ 与"老交付缺字段"区别对待:目录里连 passed.json 都没有 = 这根本不是一份
    交付,挂 `load_error`(见 _load_error),渲染侧据此明说读不到。
    """
    from ..delivery import delivery_root_of, resolve_run
    path = resolve_run(str(path or "").strip())
    p = _load_json(os.path.join(path, "passed.json")) if path else {}
    r = _load_json(os.path.join(path, "reject.json")) if path else {}
    v = _load_json(os.path.join(path, "review.json")) if path else {}
    err = "" if path and os.path.exists(os.path.join(path, "passed.json")) \
        else _load_error(path or "(空)")

    episodes: dict = {}
    for eid, pe in (p.get("episodes") or {}).items():
        episodes[eid] = {"verdict": pe.get("判决", "通过"),
                         "soft_score": pe.get("综合软分"),
                         "reject_reason": None,
                         "checks": _norm_checks(pe.get("checks"))}
    for eid, re_ in (r.get("episodes") or {}).items():
        episodes[eid] = {"verdict": re_.get("判决", "拒绝"),
                         "soft_score": re_.get("综合软分"),
                         "reject_reason": re_.get("原因"),
                         "checks": _norm_checks(re_.get("checks"))}
    for eid, ve in (v.get("episodes") or {}).items():
        ep = episodes.setdefault(eid, {"verdict": ve.get("当前判决", "?"),
                                       "soft_score": None, "reject_reason": None,
                                       "checks": {}})
        ep["pending"] = ve.get("待裁决项") or []
        ep["abstain_reasons"] = ve.get("弃权原因") or {}
        # review 条目自带 checks 的情况(rejudge 搬移过的条目会写上):只在
        # passed/reject 那边没有时才用,不覆盖主视图的读数。
        if not ep.get("checks") and ve.get("checks"):
            ep["checks"] = _norm_checks(ve.get("checks"))

    det = os.path.join(path, "details")
    for eid, ep in episodes.items():
        ep.setdefault("pending", [])
        ep.setdefault("abstain_reasons", {})
        ep["evidence"] = sorted(glob.glob(os.path.join(det, "evidence", eid, "*.jpg")))
        plot = os.path.join(det, "plots", f"{eid}_sync.png")
        ep["plot"] = plot if os.path.exists(plot) else None

    # 人工溯源的瘦快照(2026-08-16):「裁决落库了没有 / 是不是沿用」的判据要比对
    # 三件套条目上的溯源块,而上面归一化的 episodes 把它丢了。只留四个溯源键 +
    # 条目在场证明(空 dict 也留 —— "该 episode 在不在本次跑批里"靠它),几百条
    # 也只有几 KB,判据函数(dataset_level/decisions.py)吃的就是这个形状。
    def _prov_slim(doc: dict) -> dict:
        keys = (_dec.PROV_RELABEL, _dec.PROV_LABEL, _dec.PROV_TASK,
                _dec.PROV_APPEAL)
        return {"episodes": {eid: {k: e[k] for k in keys
                                   if isinstance(e, dict) and k in e}
                             for eid, e in (doc.get("episodes") or {}).items()}}

    audit_queue = (v.get("标注-画面分歧复核队列")
                   or v.get("标注审计复核队列") or [])

    # 已裁决台账(2026-08-25 用户定):新交付读 review.json 的「已裁决存档」堆;
    # 老交付(存档机制之前被 rejudge 搬走的条目)从 passed/reject 条目上的裁决
    # 溯源块**派生**出等价记录兜底 —— droid-50 那批不用重跑就能看全台账。
    # 存档为准,派生只补缺(同 id 不覆盖存档)。
    archive = dict(v.get("已裁决存档") or {})
    for eid, e in (p.get("episodes") or {}).items():
        if eid in archive or not isinstance(e, dict):
            continue
        tp = e.get("人工裁决") or {}
        bp = e.get("弃权补判") or {}
        ap = e.get("人工复议") or {}
        if tp.get("裁决") == "判成功":
            archive[eid] = {"线": "成败", "结论": "判成功", "去向": "回交付",
                            "备注": tp.get("备注", ""),
                            "裁决时间": tp.get("裁决时间", ""), "应用时间": "",
                            "_derived": True}
        elif bp:
            archive[eid] = {"线": "补判", "结论": "补判转正", "去向": "回交付",
                            "补判判定": bp.get("补判判定", ""),
                            "裁决时间": bp.get("补判时间", ""), "应用时间": "",
                            "_derived": True}
        elif ap.get("复议结论") == "捞回":
            archive[eid] = {"线": "复议", "结论": "捞回", "去向": "回交付",
                            "备注": ap.get("备注", ""),
                            "裁决时间": ap.get("复议时间", ""), "应用时间": "",
                            "_derived": True}
    for eid, e in (r.get("episodes") or {}).items():
        if eid in archive or not isinstance(e, dict):
            continue
        tp = e.get("人工裁决") or {}
        bp = e.get("弃权补判") or {}
        if tp.get("裁决") == "判失败":
            archive[eid] = {"线": "成败", "结论": "判失败", "去向": "进拒绝",
                            "备注": tp.get("备注", ""),
                            "裁决时间": tp.get("裁决时间", ""), "应用时间": "",
                            "_derived": True}
        elif bp:
            archive[eid] = {"线": "补判", "结论": "补判判失败", "去向": "进拒绝",
                            "补判判定": bp.get("补判判定", ""),
                            "裁决时间": bp.get("补判时间", ""), "应用时间": "",
                            "_derived": True}
    prov_files = {"passed": _prov_slim(p), "reject": _prov_slim(r),
                  "review": {**_prov_slim(v),
                             "标注-画面分歧复核队列": audit_queue}}

    _droot = delivery_root_of(path) if path else ""
    return {"path": path,
            # 交付根:裁决 CSV 与「运行」列表都挂在它下面(path 是**某一次跑批**)
            "delivery": _droot,
            # 数据指纹:加载那一刻关键文件的 stat 快照,页签切换时对一下就知道
            # 盘上变没变(见 data_sig)
            "data_sig": data_sig(path, _droot) if path else "",
            "run": os.path.basename(path.rstrip("/")) if path else "",
            "load_error": err,
            "prov_files": prov_files,
            "name": p.get("数据集") or os.path.basename(path.rstrip("/")),
            "robot": p.get("机器人"),
            "generated_at": p.get("生成时间"), "code_version": p.get("代码版本"),
            "dataset": p.get("dataset") or {},
            "config_effective": p.get("config_effective"),
            "runtime": p.get("runtime") or {},
            "skills": p.get("skills") or {},
            "label_audit": p.get("label_audit"),
            # 双键兼容(2026-07-31 键名中性化):新交付写"标注-画面分歧复核队列",
            # 老交付写"标注审计复核队列"——两个都认,否则老交付打不开。
            "audit_queue": audit_queue,
            "adjudication_archive": archive,
            "task_review": task_review_queue(v, episodes),
            "reject_appeal": reject_appeal_queue(r, episodes),
            "episodes": episodes}


#: 任务成败检查在交付里的中文名(report.py 的 CHECK_CN 单一事实源;此处是读端的
#: 常量副本——UI 不 import 管道代码的红线,不许 from ..export.report import)。
TASK_CHECK_CN = "任务成败判定"

#: 「原始标注 vs 自产描述对不上」这类问题在**界面上的唯一叫法**(2026-08-13 用户
#: 定)。原来的「标注-画面分歧复核队列」里,复核/队列是我们的流程词,客户读不出
#: 这到底是什么问题;而「标注可能有误」那类说法先把客户判了有罪 —— 自产描述同样
#: 会错,两边都是嫌疑人。这句只说"哪两样东西对不上",不带流程词、不偏心。
#: ⚠️ 只管显示:review.json 里的键名(标注-画面分歧复核队列,以及老交付的
#: 标注审计复核队列)是数据契约,改一个字老交付就读不出来了。
AUDIT_TERM = "标注与视频内容分歧"

#: 任务成败弃权队列里,人要看的那两个读数(VLM 判定的中间量,不是我们现算的)。
#: voc = 打乱帧排序能否还原时序(任务在不在推进);末态分 = 终态完成度。
TASK_READING_KEYS = (("voc", "voc"), ("completion_final", "末态分"))


def _deep_detail(raw) -> dict:
    """detail 解码,容忍**双重编码**(JSON 字符串里又套一层 JSON 字符串)。

    交付里 detail 本来就是 JSON 字符串;经过 rejudge 搬移 / 老版本管道时,曾出现
    再被 json.dumps 一次的条目——只解一层拿到的是 str,读数就全丢了。这里最多剥
    两层,仍不是 dict 就交给 parse_detail 的原文兜底(不丢信息)。
    """
    d = raw
    for _ in range(2):
        if isinstance(d, dict):
            return d
        if isinstance(d, str) and d.strip():
            try:
                d = json.loads(d)
                continue
            except Exception:  # noqa: BLE001
                break
        break
    return parse_detail(d if isinstance(d, (dict, str)) else raw)


def task_readings(check: dict) -> dict:
    """任务成败检查条目 → {"voc":…, "末态分":…}(取不到的键不出现)。

    check 既吃 manifest 归一化后的 {"detail": dict},也吃交付原文 {"detail": "…"}。
    """
    d = check.get("detail") if isinstance(check, dict) else None
    d = _deep_detail(d)
    out = {}
    for key, label in TASK_READING_KEYS:
        if d.get(key) is not None:
            out[label] = d[key]
    return out


def task_review_queue(review_json: dict, episodes: dict) -> list:
    """review.json + 已合并的 episodes → 任务成败待裁决队列(纯函数)。

    只收**待裁决项含「任务成败判定」**的条目:review 里还有别的维度的弃权
    (如同步/运动学),那些不是人看视频就能拍板的,不该混进成败裁决面板。
    """
    out = []
    for eid, ve in sorted((review_json.get("episodes") or {}).items()):
        if TASK_CHECK_CN not in (ve.get("待裁决项") or []):
            continue
        ep = episodes.get(eid) or {}
        check = (ep.get("checks") or {}).get(TASK_CHECK_CN) or {}
        out.append({"id": eid,
                    "current": ve.get("当前判决", "?"),
                    "reason": (ve.get("弃权原因") or {}).get(TASK_CHECK_CN, ""),
                    "readings": task_readings(check),
                    "state": check.get("state", "")})
    return out


def reject_appeal_queue(reject_json: dict, episodes: dict) -> list:
    """reject.json + 已合并的 episodes → 被拒复议队列(纯函数)。

    准入判据只有一条:拒因归因于任务成败判定(is_task_success_reject,与 rejudge
    的落闭环共用)。物理与结构问题拒掉的条目**绝不出现在这里** —— 时间戳残段、
    运动学超限、同步判废都是测出来的事实,复议不了。
    拒因文本不在这里拼:显示时走 episode_reason_line(交付里那个唯一事实源)。
    """
    out = []
    for eid, re_ in sorted((reject_json.get("episodes") or {}).items()):
        if not is_task_success_reject(re_.get("原因")):
            continue
        check = ((episodes.get(eid) or {}).get("checks") or {}).get(TASK_CHECK_CN) or {}
        out.append({"id": eid, "readings": task_readings(check),
                    "state": check.get("state", "")})
    return out


# ───────── 表格整形(Gradio Dataframe 直接吃)─────────

OVERVIEW_HEADERS = ["项", "数量"]

#: 判废明细里检查名的中文。report.py 的 CHECK_CN 是单一事实源,这里是读端的常量
#: 副本(UI 不 import 管道代码的红线,与 TASK_CHECK_CN 同一条纪律)。表里没有的
#: 键原样透出:宁可显示 `foo_check`,也不按名字猜一个中文名。
HARD_FAIL_CHECK_CN = {
    "timestamp_check": "时间戳检查",
    "kinematic_limits": "运动学极限",
    "motion_quality": "运动质量",
    "visual_quality": "视觉质量",
    "video_action_sync": "视频-动作同步",
    "task_success": TASK_CHECK_CN,
}

#: 层级缩进用全角空格:单元格走 HTML 渲染,行首的半角空格会被折叠掉,缩进等于没写。
_IND = "　"

# ⚠️ 总览表**刻意不列**标注相关的两行(「标注与视频内容分歧」N 条、「标注缺失」
#    N 条)。2026-08-13 用户定,理由是人工真值集摆出来的数字:客户标注错 6/106
#    (5.7%),而我们自产描述错 27/94(28.7%)。这轮系统检出的 32 条"分歧"里,
#    真正是客户标错的只有个位数,其余多半是我们描述不准 —— 把「分歧 32 条」摆在
#    总览首屏,实际是在展示自家打标不准,还容易被读成"你的标注有 32 条有问题"。
#    等打标质量改进后再议。**撤的只是总览这两行的展示**:分歧队列在「人工裁决」
#    页照旧(用户要靠它逐条裁),review.json 的键名与 audit_labels 的产出一律没动。


#: 没有硬门名字的那类判废在表里叫什么。判决层的第二种判废理由是"综合加权分低于
#: 阈值"(见 pipeline/verdict.py),它不属于任何一项检查,所以子项里没有它的名字。
#: 措辞用界面既有说法:「软分」是内部机制名,2026-08-11 就统一叫「质量分」了。
SOFT_DROP_LABEL = "综合质量分不达标"

#: 交付内部标记那一行的名字。表下小字要**只**为它印"不参与加减"那句,所以它得是
#: 个常量 —— 判废子项在相加大于总数时也用「其中」措辞,两者不能靠字面撞在一起。
PENDING_ROW_LABEL = "待人工裁决"


def drop_breakdown(d: dict) -> tuple[list, str]:
    """判废的子项 → ([(名字, 条数), …], 模式)。模式 ∈ ""(没子项可列)/
    "decompose"(能加出总数,摆成 ├/└ 分解式)/ "overlap"(相加大于总数)。

    ⚠️ 为什么不能直接把 hard_fail_breakdown 摆成分解式(2026-08-14 用户点名):
    判决层有**两种**判废理由 —— 踩中硬门(有检查名),或者综合加权分 < 0.5
    (没有检查名)。只列前者的话,一条走质量分被判废的 episode 在子项里查无此人,
    表上就会出现「判废 16,子项 14+1」自己打自己脸。差额补一行 SOFT_DROP_LABEL,
    等式就重新闭合。(扫过 pod 上全部 49 份交付目前都没踩到,但机制上迟早会。)

    反过来,一条 episode 可能同时踩中两个硬门 —— 那时子项相加**大于**判废总数。
    这种情况下摆成 ├/└ 的分解式是错的(读者会去加),改用「其中」的措辞,并由
    overview_note_md 在表下说明"同一条可能同时踩中多项"。
    """
    hb = d.get("hard_fail_breakdown")
    drop = d.get("verdict_drop")
    if not isinstance(hb, dict) or not isinstance(drop, int):
        return [], ""                    # 老交付没这个字段 → 不占位、不猜
    items = [(HARD_FAIL_CHECK_CN.get(k, k), n) for k, n in hb.items()]
    # 人工裁决剔除的量(rejudge 平账时写回,见 pipeline/rejudge.py
    # _sync_dataset_summary):不列这一行的话,人工剔掉的条数会落进下面的
    # 差额兜底行,被冠上「质量分」的名头(2026-08-25 复盘 ①)。
    hd = d.get("human_drop")
    if isinstance(hd, int) and hd > 0:
        items.append(("人工裁决", hd))
    judged = sum(n for _, n in items if isinstance(n, int))
    if judged > drop:
        return items, "overlap"
    if judged < drop:
        items = items + [(SOFT_DROP_LABEL, drop - judged)]
    return (items, "decompose") if items else ([], "")


def overview_rows(m: dict) -> list[list]:
    """交付 → 质检总览那一张表(两列:项 / 数量)。

    2026-08-13 用户点名重做。此前是"上半部几行 bullet + 下半部一张表",同一批
    数字说两遍,而且两处的「不合格拦截」同名不同义:表里那行只算中途被硬门刷掉的
    (droid-200-full = 1),bullet 里那句是最终判废的全部(15)。合成一张表之后
    口径只剩一个,而且能一眼验:

        **输入 = 判废 + 精确去重删除 + 交付**(三者不重不漏)

    带「其中」的行是交付内部的标记(已经在交付里了,只是等着人看一眼),
    **不参与加减** —— 这句话由 overview_note_md 印在表下,不让人自己猜。
    老交付缺哪个字段就少哪一行:不占位、不写「?」、不猜默认值。
    标注相关的行为什么不在这里,见上面那段 ⚠️。
    """
    if m.get("load_error"):
        return []
    d = m.get("dataset") or {}
    ss = d.get("summary_stats") or {}
    fs = d.get("funnel_stats") or {}
    rows: list[list] = []

    def _add(label, value):
        if value not in (None, ""):
            rows.append([label, value])

    _add("输入 episode", d.get("input_episodes", fs.get("input", "")))
    drop = d.get("verdict_drop", "")
    _add("判废", drop)
    items, mode = drop_breakdown(d)
    for i, (label, n) in enumerate(items):
        if mode == "overlap":
            # 相加大于总数时不摆分解式:├/└ 是在邀请读者去加,而这里加不得
            rows.append([f"{_IND}其中 {label}", n])
        else:
            glyph = "└" if i == len(items) - 1 else "├"
            rows.append([f"{_IND}{glyph} {label}", n])
    _add("精确去重删除", d.get("dedup_removed", ""))
    delivered = d.get("delivered", "")
    if delivered not in (None, ""):
        pct = ss.get("pass_rate_pct")
        # 通过率与交付条数同格(用户定):它是交付这一行的注脚,另起一行就又变成
        # "同一件事说两遍"
        rows.append(["交付", f"{delivered}(通过率 {pct}%)"
                     if pct is not None else delivered])
        n_pending = sum(1 for e in (m.get("episodes") or {}).values()
                        if e.get("pending"))
        if n_pending:
            rows.append([f"{_IND}其中 {PENDING_ROW_LABEL}", n_pending])
    _add("平均质量分", ss.get("avg_soft_score", ""))
    return rows

# 表下的口径/加减法小字 2026-08-23 用户点名整个删掉(没必要说这么详细,客户懂);
# 表本身仍按「输入 = 判废 + 去重删除 + 交付」对账,只是不再印解释。


CHECK_HEADERS = ["检查", "结果", "分数", "要点"]

#: 检查结果的**界面用词**:交付里记的是实现记法,界面只说客户听得懂的话。
#: 「软分」是内部机制名(可补偿的打分维度),2026-08-11 用户点名统一叫「质量分」。
#: 没列进来的取值原样透出(拒绝/弃权本来就是人话)。
CHECK_STATE_TEXT = {"软分": "质量分"}


def check_rows(m: dict, eid: str) -> list[list]:
    ep = m["episodes"].get(eid) or {}
    rows = []
    for name, c in (ep.get("checks") or {}).items():
        d = c["detail"]
        gist = d.get("reason") or d.get("verdict") or ""
        if "voc" in d:
            gist = f"voc={d['voc']} 末态={d.get('completion_final')} {gist}"
        state = c["state"]
        rows.append([name, CHECK_STATE_TEXT.get(state, state),
                     c["score"] if c["score"] is not None else "", str(gist)[:120]])
    return rows


# 「判据」列 2026-08-23 用户点名撤下,换「episodes」:哪些条落在该子类,比归纳判据
# 有用得多(判据在分布图的悬浮提示里仍看得到)。episode 号去 ep 前缀,与裁决队列同款。
SKILL_HEADERS = ["技能族", "子技能", "条数", "占比%", "episodes"]


def _skill_members(m: dict) -> dict:
    """details/skill_assignment.csv → {(族, 子技能): "1, 4, 27…"}。老交付没这份文件给空。"""
    import csv
    path = os.path.join(str(m.get("path") or ""), "details", "skill_assignment.csv")
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out.setdefault((r.get("family", ""), r.get("subskill", "")), []).append(
                    _ep_num(r.get("episode_id", "")))
    except OSError:
        return {}
    return {k: ", ".join(v) for k, v in out.items()}


def _skill_name(entry, en: str) -> str:
    """显示名:交付里带中文名(2026-08-24 起生成)就用中文,老交付回落英文。"""
    return str((entry or {}).get("name_zh") or en)


def skill_rows(m: dict) -> list[list]:
    members = _skill_members(m)          # ⚠️ 成员映射按英文 slug 对号,显示才换中文
    rows = []
    for fam, f in (m["skills"].get("families") or {}).items():
        subs = f.get("subskills") or {}
        if not subs:
            rows.append([_skill_name(f, fam), "", f.get("count", ""), f.get("pct", ""),
                        members.get((fam, "-"), members.get((fam, ""), ""))])
        for sub, s in subs.items():
            rows.append([_skill_name(f, fam), _skill_name(s, sub),
                         s.get("count", ""), s.get("pct", ""),
                         members.get((fam, sub), "")])
    return rows


#: 按技能族轮换的极淡底色(2026-08-23 用户提议:大类分块,一眼分得开)。
#: 只做背景提示,不承载语义;未归类固定灰。Gradio Dataframe 做不了按行上色,
#: 这张表改自绘 HTML(与检查明细同一路数)。
_SKILL_ROW_TINTS = ["#F2F7FF", "#F1FAF3", "#FFF9EE", "#F7F4FF", "#EFFAF9", "#FDF3F7"]


def skill_table_html(m: dict) -> str:
    """两级技能体系表(HTML 版):同族的行同一淡底色,族间颜色轮换。"""
    rows = skill_rows(m)
    if not rows:
        return ""
    fams: list = []
    for r in rows:
        if r[0] not in fams:
            fams.append(r[0])
    tint = {f: ("#F5F6F7" if f == "未归类" else _SKILL_ROW_TINTS[i % len(_SKILL_ROW_TINTS)])
            for i, f in enumerate(fams)}
    head = "".join('<th style="padding:6px 10px;text-align:left;font-weight:700;'
                   'color:#4E5969;border-bottom:2px solid #C9CDD4;white-space:nowrap">'
                   f"{_esc(h)}</th>" for h in SKILL_HEADERS)
    body = []
    for i, r in enumerate(rows):
        # 分隔线要压得住淡底色(2026-08-23 用户实见:淡灰线在色块上看不出来):
        # 族内行间用 Arco 边框灰,族与族交界加粗一档
        last_in_fam = i + 1 >= len(rows) or rows[i + 1][0] != r[0]
        line = "2px solid #A9AEB8" if last_in_fam else "1px solid #C9CDD4"
        tds = []
        for j, c in enumerate(r):
            # 族/子技能列给足最小宽度,常规名字一行放得下,超长的才换行
            extra = ("min-width:200px;" if j == 0 else
                     "min-width:240px;" if j == 1 else
                     "white-space:nowrap;" if j in (2, 3) else "")
            mono = "font:12px/1.6 ui-monospace,Menlo,monospace;color:#4E5969;" if j == 4 else ""
            tds.append(f'<td style="padding:5px 10px;border-bottom:{line};'
                       f'vertical-align:top;{extra}{mono}">{_esc(c)}</td>')
        body.append(f'<tr style="background:{tint[r[0]]}">' + "".join(tds) + "</tr>")
    return ('<div style="font:13px/1.7 system-ui;font-weight:700;color:#4E5969;'
            'margin:10px 0 4px">两级技能体系</div>'
            '<table style="border-collapse:collapse;width:100%;font:13px/1.7 system-ui">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>")


# ───────── 技能分布图(2026-07-30):技能画像页的横向条形图 ─────────
#
# 设计定稿(用户拍板 + 两条实现纪律),改之前先读完:
# ① **不截断**:全部条目都画。画像的价值恰恰在长尾——bridge 那 130 项里绝大多数
#    只有 1 条数据,截掉尾巴看到的是"数据很集中"的假象。行高压紧(一条一行、
#    条间 2px)让长列表也能扫读。
# ② **单色**:所有条同一个蓝。所有条测的是同一个量(条数),按族上色是彩虹图
#    反模式——颜色不携带任何信息,只增加视觉噪声,还会被误读成"类别有好坏"。
#    单蓝已过调色板校验(亮度带 / 彩度下限 / CVD 分离 / 常视分离 / 对比度,五项
#    全过)。**别改成多色。**
# ③ **样本偏少**:名单直接用画像自带的 undersampled 字段(不是本图现算的,图下
#    注明出处)。标记方式 = 条尾一枚**带文字**的琥珀 chip,不靠颜色单独表意;
#    **不换条的填充色**——条本来就短,再变个色是冗余编码。
# ④ **下钻用原生 <details>/<summary>,零 JavaScript**:族条本身就是 summary
#    (行首 ▸/▾ 由 CSS 的 details[open] 切换,同样零脚本),展开露出子技能条。
#    只有 **≥2 个子技能**的族才折叠,单子技能族退化成普通行(点开只看到自己的
#    复读没有意义);普通行占一个等宽的空箭头位,保证所有条起点对齐。
# ⑤ **共用全局尺子**:子技能条与族条按**同一个**全局最大 count 归一。若按族内
#    最大值重缩放,只有 1 条数据的族其子技能会画得和 102 条的 Put 一样长——那是
#    骗人的。
#
#: 唯一的条色(见上 ②)。
SKILL_BAR_COLOR = "#165DFF"

#: 形状 B(VLM 不可用 → 退回按原始标注分组)必须挂的前提说明。不写清楚,客户会
#: 把一堆原始指令当成系统归纳出的技能体系。形状 A 不显示这句。
SKILL_FALLBACK_NOTE = ("未经 VLM 审计的原始标注分组(VLM 不可用时的降级路径),仅供参考")

#: 悬停详情里判据截断长度(判据是 LLM 写的一整句,全塞进 title 会糊一屏)。
_SKILL_CRIT_CAP = 60


def _esc(s) -> str:
    """进 HTML 前转义。技能名来自 LLM 归纳或数据集原始指令,不能当可信片段拼。"""
    import html
    return html.escape(str(s), quote=True)


def skill_chart_items(m: dict) -> tuple[str, list[dict]]:
    """skills 块 → (形状, 条目列表)。纯数据整形,不产 HTML(方便单测)。

    形状三选一:
      "two_level" 两级画像(正常路径):families → 每族带 subskills
      "flat"      扁平降级画像(VLM 不可用,按原始标注分组):skills 一层
      "empty"     未启用 / 空
    条目一律按条数降序;子技能同样降序。
    """
    sk = m.get("skills") or {}
    fams = sk.get("families") or {}
    flat = sk.get("skills") or {}
    if fams:
        items = [{"name": name, "zh": f.get("name_zh") or "",
                  "count": f.get("count") or 0, "pct": f.get("pct"),
                  "criterion": f.get("criterion") or "",
                  "subs": sorted(
                      ({"name": sn, "zh": s.get("name_zh") or "",
                        "count": s.get("count") or 0, "pct": s.get("pct"),
                        "criterion": s.get("criterion") or "", "subs": []}
                       for sn, s in (f.get("subskills") or {}).items()),
                      key=lambda x: (-x["count"], x["name"]))}
                 for name, f in fams.items()]
        shape = "two_level"
    elif flat:
        items = [{"name": name, "count": s.get("count") or 0, "pct": s.get("pct"),
                  "criterion": "", "subs": []} for name, s in flat.items()]
        shape = "flat"
    else:
        return "empty", []
    items.sort(key=lambda x: (-x["count"], x["name"]))
    return shape, items


#: 图内样式:只有"折叠箭头"这一件事非 CSS 不可(纯内联样式写不出 details[open]
#: 与伪元素)。选择器全部挂在 .sk-chart 下,不污染 Gradio 页面其余部分;零 JS。
_SKILL_CSS = """<style>
.sk-chart details > summary{list-style:none;cursor:pointer}
.sk-chart details > summary::-webkit-details-marker{display:none}
.sk-chart .sk-caret::before{content:"\\25B8";color:#888}
.sk-chart details[open] > summary .sk-caret::before{content:"\\25BE"}
</style>"""


def _skill_bar_row(it: dict, top: int, undersampled: set, *,
                   sub: bool = False, caret: bool = False) -> str:
    """一根条(族 / 子技能共用)。宽度按**全局** top 归一(见上 ⑤)。"""
    name, count, pct = it["name"], it["count"], it["pct"]
    shown = it.get("zh") or name             # 显示中文,英文 slug 留在悬浮提示里可对号
    width = max(0.6, count / top * 100) if top else 0.6
    meta = f"{count} 条" + (f" · {pct}%" if pct is not None else "")
    title = f"{name} · {meta}"
    if it.get("criterion"):
        title += f" · 判据:{str(it['criterion'])[:_SKILL_CRIT_CAP]}"
    # 「样本偏少」:带文字的 chip,颜色只是陪衬(色盲/黑白打印下仍读得出)。
    chip = ('<span style="background:#FFF7E8;color:#D25F00;border:1px solid #FFE4BA;'
            'border-radius:3px;padding:2px 8px;margin-left:10px;font-size:13px;'
            'white-space:nowrap">样本偏少</span>') if name in undersampled else ""
    return (
        f'<div title="{_esc(title)}" style="display:flex;align-items:center;gap:12px;'
        f'margin:5px 0">'
        f'<span class="{"sk-caret" if caret else ""}" style="flex:0 0 '
        f'{18 if not sub else 40}px;font-size:15px"></span>'
        f'<div style="flex:0 0 {300 if sub else 320}px;font:16px/1.5 system-ui;'
        f'color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
        f'{_esc(shown)}</div>'
        f'<div style="flex:1;min-width:60px;background:#F7F8FA;border-radius:4px;'
        f'height:26px">'
        f'<div style="width:{width:.2f}%;height:100%;background:{SKILL_BAR_COLOR};'
        f'border-radius:0 4px 4px 0"></div></div>'
        f'<div style="flex:0 0 210px;font:15px/1.5 system-ui;color:#666;'
        f'text-align:right">{_esc(meta)}{chip}</div></div>')


def skill_bar_html(m: dict) -> str:
    """技能画像 → 横向条形图 HTML(自包含、零 JS)。纯函数(可测)。

    两种画像形状都吃(见 skill_chart_items),没画像就一句说明、不占位。
    """
    shape, items = skill_chart_items(m)
    if shape == "empty":
        return ('<p style="color:#777">此交付未生成技能画像——需要跑过技能画像'
                '阶段(全员 caption → 归纳技能体系)的交付。</p>')
    sk = m.get("skills") or {}
    undersampled = set(sk.get("undersampled") or [])
    top = max((it["count"] for it in items), default=0) or 1
    n_eps = sk.get("n_episodes")

    head = []
    if shape == "flat":
        head.append(f'<p style="margin:0 0 6px 0;color:#D25F00;background:#FFF7E8;'
                    f'border-left:3px solid #FF7D00;padding:6px 10px">'
                    f'{SKILL_FALLBACK_NOTE}</p>')
    # 顶部“共 N 个技能族…点族名可展开”说明句 2026-08-23 用户红框点名删除

    rows = []
    for it in items:
        drill = len(it["subs"]) >= 2        # 只有 ≥2 子技能才值得折叠(见上 ④)
        bar = _skill_bar_row(it, top, undersampled, caret=drill)
        if drill:
            subs = "".join(_skill_bar_row(s, top, undersampled, sub=True)
                           for s in it["subs"])
            rows.append(f'<details><summary>{bar}</summary>{subs}</details>')
        else:
            rows.append(bar)

    # 底部“「样本偏少」标记来自画像自带的名单…”脚注 2026-08-23 用户红框点名删除
    return ('<div class="sk-chart" style="max-width:1280px">' + _SKILL_CSS
            + "".join(head) + "".join(rows) + '</div>')


# 待裁决队列单表(2026-08-23 用户拍板:分歧表 × 弃权表合一,一条 episode 一行)。
# 档位/成败线/分歧说明/弃权原因/当前判决/操作列全部不进表(同日用户点名删):
# 表只回答"哪条、什么问题、标注写了什么、系统看到什么、裁了没",细节在下方卡片;
# 点任意一格跳到该条的卡片。重点档仍排最前(顺序即档位,不再单列)。
QUEUE_HEADERS = ["episode", "待裁问题", "原始标注", "自产描述(VLM 生成)", "裁决结果"]


def _ep_num(eid: str) -> str:
    """"ep000017" → "17"(表里省掉 ep 前缀与前导零;认不出的原样给)。"""
    tail = eid[2:] if str(eid).startswith("ep") else str(eid)
    return str(int(tail)) if tail.isdigit() else str(eid)


#: 人工裁决队列的状态筛选(2026-08-25 用户定):默认全量 —— 队列是台账不是
#: 办完即焚的待办;裁决后条目留在表里,想只看欠的活或只翻旧账再切档。
MERGE_FILTER_ALL = "全部"
MERGE_FILTER_LABEL = "只看标注问题"
MERGE_FILTER_TASK = "只看成败问题"
MERGE_FILTERS = (MERGE_FILTER_ALL, MERGE_FILTER_LABEL, MERGE_FILTER_TASK)

QUEUE_STATUS_ALL = "全部"
QUEUE_STATUS_PENDING = "仅待裁决"
QUEUE_STATUS_DONE = "仅已裁决"
QUEUE_STATUS_CHOICES = (QUEUE_STATUS_ALL, QUEUE_STATUS_PENDING, QUEUE_STATUS_DONE)

#: 台账「线」→ 表格「待裁问题」列的叫法(台账条目已办结,列名沿用问题类型)。
_ARCHIVE_KIND = {"成败": "成败弃权", "补判": "成败弃权"}


def queue_status_mode(label) -> str:
    """状态档显示标签(带计数后缀)→ 档位常量;认不出按「全部」(不藏内容)。"""
    s = str(label or "")
    for mode in (QUEUE_STATUS_PENDING, QUEUE_STATUS_DONE):
        if s.startswith(mode):
            return mode
    return QUEUE_STATUS_ALL


def queue_status_choices(m: dict) -> list[str]:
    """状态档三项的显示标签(带计数):全部(N)/仅待裁决(n)/仅已裁决(N-n)。
    计数不随类型筛选变(单向联动:类型计数跟状态走,反过来不跟 —— 双向就
    循环了);随裁决进度变,落一条草稿两头的数就动。"""
    items = merged_table_queue(m)
    n_pend = sum(1 for i in items if i["pending"])
    return [f"{QUEUE_STATUS_ALL}({len(items)})",
            f"{QUEUE_STATUS_PENDING}({n_pend})",
            f"{QUEUE_STATUS_DONE}({len(items) - n_pend})"]


def _queue_task_text(m: dict, eid: str) -> tuple[str, str]:
    """成败条目在表里的两列文本(原始标注 / 自产描述,按来源分列不冒充)。"""
    text, src = episode_task_text(m, eid)
    text = text if len(text) <= 120 else text[:119] + "…"
    return ("", text) if src == TASK_SOURCE_CAPTION else (text, "")


def merged_table_queue(m: dict, status: str = QUEUE_STATUS_ALL,
                       mode: str = MERGE_FILTER_ALL) -> list[dict]:
    """人工裁决队列的表格条目(2026-08-25 重做:待办 + 台账两堆合一)。

    每条 = {"id","kind","label","cap","result","pending"}。构成与顺序:
    ①待裁的(还有问题没答)按原队列序置顶 —— 打开表第一眼是还欠的活;
    ②已裁的待办条目(草稿/已应用但还挂在队列,如分歧名册)按原序跟上;
    ③台账条目(办结离队的,存档堆 + 老交付溯源派生)按 episode 序垫底。
    裁决结果列纯文字:草稿带「(未应用)」后缀(判据=decisions_view,与横幅
    同源);台账条目显示办结结论,不带任何图标(2026-08-25 用户否掉打勾:
    判失败给绿勾是语义打架)。"""
    ldec = load_label_decisions(m)
    tdec = load_task_verdicts(m)
    q_left = question_pending_ids(m)
    st_map = {(r["line"], r["id"]): r["status"]
              for r in decision_status(m)["records"]}

    def _part(line: str, eid: str, text: str) -> str:
        if not text:
            return ""
        return text + ("(未应用)" if st_map.get((line, eid)) == "unapplied"
                       else "")

    undecided: list = []
    decided: list = []
    pending_ids: set = set()
    for it in merged_review_queue(m):
        eid, a, t = it["id"], it["audit"], it["task"]
        pending_ids.add(eid)
        kind = "标注+成败" if (a and t) else ("标注分歧" if a else "成败弃权")
        if a:
            label, cap = a.get("label", ""), a.get("caption", "")
        else:
            label, cap = _queue_task_text(m, eid)
        parts = [_part("label", eid, ldec.get(eid, {}).get("decision", "")),
                 _part("verdict", eid, tdec.get(eid, {}).get("verdict", ""))]
        got = [p for p in parts if p]
        # "待裁"判据 = question_pending_ids(单一事实源,轨迹 ⏳ 桶同款):
        # 拿不准不算结论;弃用该条封整条
        item = {"id": eid, "kind": kind, "label": label, "cap": cap,
                "result": " · ".join(got), "pending": eid in q_left}
        (undecided if item["pending"] else decided).append(item)

    archived: list = []
    for eid, ev in sorted((m.get("adjudication_archive") or {}).items()):
        line = ev.get("线", "")
        if eid in pending_ids or line not in _ARCHIVE_KIND:
            continue                     # 还挂在队列的以待办条目为准;复议进复议表
        label, cap = _queue_task_text(m, eid)
        res = ev.get("结论", "")
        if line == "补判" and ev.get("补判判定"):
            res = f"{res}({ev['补判判定']})"
        archived.append({"id": eid, "kind": _ARCHIVE_KIND[line],
                         "label": label, "cap": cap, "result": res,
                         "pending": False})

    items = undecided + decided + archived
    status = queue_status_mode(status)
    if status == QUEUE_STATUS_PENDING:
        items = [i for i in items if i["pending"]]
    elif status == QUEUE_STATUS_DONE:
        items = [i for i in items if not i["pending"]]
    # 类型档与状态档取交集(2026-08-25 用户点名两组筛选要联动):
    # 「标注+成败」两个单项档里都算
    mode = merge_filter_mode(mode)
    if mode == MERGE_FILTER_LABEL:
        items = [i for i in items if "标注" in i["kind"]]
    elif mode == MERGE_FILTER_TASK:
        items = [i for i in items if "成败" in i["kind"]]
    return items


def merged_queue_rows(m: dict, status: str = QUEUE_STATUS_ALL,
                      mode: str = MERGE_FILTER_ALL) -> list[list]:
    """人工裁决队列 → 表格行(行序与 merged_table_queue 一致,点行按下标取
    episode 再跳卡片;台账行没有对应卡片,点了保持当前卡)。"""
    return [[_ep_num(i["id"]), i["kind"], i["label"], i["cap"], i["result"]]
            for i in merged_table_queue(m, status, mode)]


#: 弃权原因是 VLM 写的一整句,表里已不列;复议表仍截断用,全文在卡片里给。
_REASON_CAP = 60


def readings_text(readings: dict) -> str:
    """{"voc":0.87,"末态分":0.3} → "voc=0.87 · 末态分=0.3"(没有读数给一句人话)。"""
    if not readings:
        return "(无读数)"
    return " · ".join(f"{k}={v}" for k, v in readings.items())


APPEAL_HEADERS = ["操作", "episode", "拒绝原因", "关键读数", "复议结论"]


def appeal_reason_text(m: dict, eid: str) -> str:
    """复议区显示的完整拒因 —— 直接引用详情页横幅那一句(单一事实源,不另拼)。"""
    return episode_reason_line(m, eid)


def appeal_rows(m: dict) -> list[list]:
    """被拒复议队列 → 表格行。复议列回显 details/reject_appeals.csv(空=待人工)。

    2026-08-25 起台账同款:被捞回的条目(已离开拒绝清单)从「已裁决存档」补进
    表尾留底 —— 此前捞回一执行,这张表就当它没存在过。行序 = 在案被拒条目
    原序 + 捞回台账(episode 序);点行跳卡只对前一段有效(台账行保持当前卡)。"""
    dec = load_reject_appeals(m)
    rows = []
    seen = set()
    for a in m.get("reject_appeal") or []:
        eid = a.get("id", "")
        seen.add(eid)
        reason = appeal_reason_text(m, eid)
        rows.append(["复议", eid,
                     reason[:_REASON_CAP] + ("…" if len(reason) > _REASON_CAP else ""),
                     readings_text(a.get("readings") or {}),
                     dec.get(eid, {}).get("appeal", "")])
    for eid, ev in sorted((m.get("adjudication_archive") or {}).items()):
        if ev.get("线") != "复议" or eid in seen:
            continue
        rows.append(["复议", eid, "(已捞回,条目已回交付)", "", ev.get("结论", "捞回")])
    return rows


def audit_pending_count(m: dict) -> int:
    """标注分歧队列里**还没裁**的条数(裁过的不该再催人去看)。"""
    dec = load_label_decisions(m)
    return sum(1 for a in (m.get("audit_queue") or [])
               if not dec.get(a.get("id", ""), {}).get("decision"))


def task_pending_count(m: dict) -> int:
    """任务成败弃权队列里还没裁的条数(「拿不准」**算未裁**:它是"待定"不是结论)。"""
    dec = load_task_verdicts(m)
    return sum(1 for t in (m.get("task_review") or [])
               if dec.get(t.get("id", ""), {}).get("verdict", "")
               in ("", VERDICT_HOLD))


def appeal_pending_count(m: dict) -> int:
    """被拒复议区里还没给结论的条数。"""
    dec = load_reject_appeals(m)
    return sum(1 for a in (m.get("reject_appeal") or [])
               if not dec.get(a.get("id", ""), {}).get("appeal"))


def appeal_hint_md(m: dict) -> str:
    """复议区标题下的提示。空队列时返回空串 —— 这一区整块不渲染(调用侧据此隐藏)。"""
    q = m.get("reject_appeal") or []
    if not q:
        return ""
    n = appeal_pending_count(m)
    # 措辞两处按 2026-08-16 用户定的改:①不用"捞回"这种口语,说"恢复为可用";
    # ②"只有这一项能复议"的范围说明不在正文重复 —— 页签名「任务失败复议」已经
    # 承担了这层意思,详细解释放在页内那个默认收起的折叠条里。
    return (f"系统按「任务成败判定」拒掉了 **{len(q)}** 条,其中 **{n}** 条还没人复核。"
            "看完视频如果认为判定有误,可将该条恢复为可用。")


#: 「待你裁决」页顶的工序引导(2026-08-16 合并队列重构后重写)。此前教的是
#: "先裁标注 → 执行 → 再裁成败 → 再执行"的两趟工序 —— 那是分区制的产物;
#: 现在两个问题在同一张卡上一次答完,rejudge 对"改标 + 人工成败结论"也不再重判,
#: 引导只需要防一件事:只改标、留空成败的条目重判后可能仍判不出,会回到队列
#: (第二轮),这句话让用户知道那不是系统坏了。
WORKFLOW_GUIDE = (
    "**怎么做**:逐条看视频,把该条的问题一次答完(标注与成败在同一张卡)→ "
    "在本页底部点「执行裁决」。改标时顺手给了成败结论的,机器直接采信;"
    "只改标、留空成败的,机器按新标注重判 —— 重判后仍判不出的会回到这里,"
    "补个结论再执行一次。")


# ───────── 「待你裁决」合并队列(2026-08-16 重构)─────────
#
# 起因(用户实见):「人工裁决」页按问题类型分三区,而人的工作是**按 episode
# 展开**的 —— 一条视频看一遍、该答的问题一次答完。droid-200-new 实测:标注分歧
# 29 条、成败弃权 37 条,7 条同时在两个队列里,用户要在两张不同卡片里各找一次、
# 各看一遍视频。分区是我们的实现方便,不是用户的工作方式。

# (类型档常量上移至状态档常量旁 —— merged_table_queue 的默认参数用到)


def merged_review_queue(m: dict) -> list[dict]:
    """标注分歧队列 × 成败弃权队列 → 按 episode 合并去重的「待你裁决」清单。

    每条 = {"id", "audit": 分歧队列条目|None, "task": 弃权队列条目|None};
    重叠的 episode 只出现一次,两个问题都挂在同一条上。
    顺序稳定:先按分歧队列原序(重点档排最前,与产出一致 —— 分区时代的顺序
    承诺不变),只有成败问题的条目按弃权队列原序(episode 升序)接在后面。
    """
    items: list[dict] = []
    seen: dict = {}
    for a in m.get("audit_queue") or []:
        it = {"id": a.get("id", ""), "audit": a, "task": None}
        items.append(it)
        seen[it["id"]] = it
    for t in m.get("task_review") or []:
        eid = t.get("id", "")
        if eid in seen:
            seen[eid]["task"] = t
        else:
            items.append({"id": eid, "audit": None, "task": t})
    return items


# ── 每张裁决卡的任务标注(2026-08-21 用户定:标注是每张卡的第一行,不是某个问题块的字段)──
#
# 只判成败的卡此前不显示任务文本 —— 等于让人对着视频猜题。数据现成:details/task_details.json
# 记着判定时用的那句 instruction 与来源(成败弃权队列的条目全部跑过判定,必有);兜底依次是
# 分歧队列里的原始标注、captions.json 的自产描述。都没有就明说"没记录",不猜。

_DETAILS_JSON_CACHE: dict = {}


def _details_json(run_path: str, name: str) -> dict:
    """details/<name> 的 JSON(按 mtime 缓存一份:每次渲染卡片都读几百 KB 不划算)。"""
    p = os.path.join(str(run_path or ""), "details", name)
    try:
        key = (p, os.path.getmtime(p))
    except OSError:
        return {}
    if key not in _DETAILS_JSON_CACHE:
        _DETAILS_JSON_CACHE.clear()                 # 只留最近一份,防内存积累
        try:
            _DETAILS_JSON_CACHE[key] = _load_json(p) or {}
        except Exception:  # noqa: BLE001 坏 JSON 按没有处理,不让卡片炸
            _DETAILS_JSON_CACHE[key] = {}
    d = _DETAILS_JSON_CACHE[key]
    return d if isinstance(d, dict) else {}


TASK_SOURCE_LABEL = "原始标注"
TASK_SOURCE_CAPTION = "自产描述(VLM)"
TASK_SOURCE_RELABEL = "人工改标"


def _task_source_label(src) -> str:
    """交付里的来源字样 → 界面叫法(内部写法五花八门:自产caption / caption / 原始标注…)。"""
    s = str(src or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "caption" in low or "自产" in s or "描述" in s:
        return TASK_SOURCE_CAPTION
    if "人工" in s or "改标" in s:
        return TASK_SOURCE_RELABEL
    return TASK_SOURCE_LABEL


def episode_task_text(m: dict, eid: str) -> tuple[str, str]:
    """一条 episode 的任务文本 → (文本, 来源叫法);交付里没记录 → ("", "")。"""
    path = (m or {}).get("path") or ""
    e = ((_details_json(path, "task_details.json").get("episodes") or {}).get(eid)
         or {})
    text = str(e.get("instruction") or "").strip()
    if text:
        return text, (_task_source_label(e.get("instruction_source")) or TASK_SOURCE_LABEL)
    for a in (m or {}).get("audit_queue") or []:
        if a.get("id") == eid and str(a.get("label") or "").strip():
            return str(a["label"]).strip(), TASK_SOURCE_LABEL
    cap = _details_json(path, "captions.json").get(eid)
    if str(cap or "").strip():
        return str(cap).strip(), TASK_SOURCE_CAPTION
    return "", ""


def _adopted_label(decision) -> str:
    """已采纳改标 → 改后的文本;否则空串。"""
    d = decision or {}
    if d.get("decision") == "采纳建议改标":
        return str(d.get("new_label") or "").strip()
    return ""


def task_text_short(m: dict, eid: str, n: int = 40) -> str:
    t = episode_task_text(m, eid)[0]
    return t if len(t) <= n else t[: n - 1] + "…"


def task_reference_md(m: dict, eid: str, decision: dict | None = None) -> str:
    """卡头那行:`任务:「…」· 来源:原始标注`。采纳改标后立刻换成改后的文本并标明,
    ② 判成败时看到的就是新标注(与 rejudge 按新标注重判的口径一致)。"""
    new = _adopted_label(decision)
    if new:
        return f"任务:「{new}」 · 来源:{TASK_SOURCE_RELABEL}(已改标)"
    text, src = episode_task_text(m, eid)
    if not text:
        return "任务:(交付里没有记录这条的任务文本)"
    return f"任务:「{text}」" + (f" · 来源:{src}" if src else "")


def task_reference_html(m: dict, eid: str, decision: dict | None = None) -> str:
    """卡头的任务标注**醒目版**(2026-08-21 用户点名:要 standout):Arco 蓝底左色条的
    提示块,任务文本加大加粗,来源灰小字。文案与 task_reference_md 同源。"""
    new = _adopted_label(decision)
    if new:
        text, src = new, f"{TASK_SOURCE_RELABEL}(已改标)"
    else:
        text, src = episode_task_text(m, eid)
    if not text:
        return ('<div style="background:#FFF7E8;border-left:4px solid #FF7D00;border-radius:6px;'
                'padding:10px 14px;margin:6px 0 10px;font:14px/1.6 system-ui;color:#4E5969">'
                '任务:交付里没有记录这条的任务文本,请结合画面判断</div>')
    return ('<div style="background:#E8F3FF;border-left:4px solid #165DFF;border-radius:6px;'
            'padding:10px 14px;margin:6px 0 10px;font:14px/1.6 system-ui">'
            '<span style="color:#4E5969;margin-right:8px">任务</span>'
            f'<span style="font-size:17px;font-weight:700;color:#1D2129">「{_esc(text)}」</span>'
            + (f'<span style="margin-left:12px;font-size:12px;color:#86909C">来源:{_esc(src)}</span>'
               if src else "")
            + '</div>')


def task_question_md(m: dict, eid: str, decision: dict | None = None) -> str:
    """② 三个按钮正上方那一句:把任务文本放在判断的正上方,眼睛不用在卡头和按钮之间来回跳。"""
    text = _adopted_label(decision) or episode_task_text(m, eid)[0]
    if text:
        return f"按任务「{text}」看,这条完成了吗?"
    return "这条完成了它的任务吗?(交付里没有记录任务文本,请结合画面判断)"


def merged_queue_view(m: dict, mode: str) -> list[dict]:
    """按筛选档过滤后的合并队列。重叠条目在两个单项档里都出现(它确实两种问题
    都有),但任何一档里都只出现一次 —— 去重是按 episode,不是按问题。"""
    q = merged_review_queue(m)
    if mode == MERGE_FILTER_LABEL:
        return [it for it in q if it["audit"] is not None]
    if mode == MERGE_FILTER_TASK:
        return [it for it in q if it["task"] is not None]
    return q


def merged_filter_choices(m: dict, status: str = QUEUE_STATUS_ALL) -> list[str]:
    """类型档三项的显示标签(带计数)。第一项是默认档「全部」。
    2026-08-25 起计数 = 当前状态档下的表行数(用户点名要与状态档联动:切到
    「仅已裁决」还报待办数,成败问题明明满表台账却写 0)。"""
    items = merged_table_queue(m, status)
    n_label = sum(1 for i in items if "标注" in i["kind"])
    n_task = sum(1 for i in items if "成败" in i["kind"])
    return [f"{MERGE_FILTER_ALL}({len(items)})",
            f"{MERGE_FILTER_LABEL}({n_label})",
            f"{MERGE_FILTER_TASK}({n_task})"]


def merge_filter_mode(label) -> str:
    """筛选器显示标签(带计数后缀)→ 档位常量。认不出的值按「全部」处理:
    筛选器只影响看哪些卡,宽档是唯一不会藏内容的降级。"""
    s = str(label or "")
    for mode in (MERGE_FILTER_LABEL, MERGE_FILTER_TASK):
        if s.startswith(mode):
            return mode
    return MERGE_FILTER_ALL


def question_pending_ids(m: dict) -> set:
    """还欠人工结论的 episode id(**单一判据**,2026-08-25 用户点名对齐:
    轨迹页 ⏳ 桶与人工裁决队列的待/已分档都以它为准 —— 名叫「待人工」就得
    真还欠着人)。「拿不准」不是结论;「弃用该条」封了整条(② 被矛盾拦截),
    啥都不欠。"""
    dec = load_label_decisions(m)
    ver = load_task_verdicts(m)
    out = set()
    for it in merged_review_queue(m):
        eid = it["id"]
        d = dec.get(eid, {}).get("decision", "")
        if d == "弃用该条":
            continue
        a_pending = it["audit"] is not None and d in ("", VERDICT_HOLD)
        t_pending = (it["task"] is not None
                     and ver.get(eid, {}).get("verdict", "")
                     in ("", VERDICT_HOLD))
        if a_pending or t_pending:
            out.add(eid)
    return out


def merged_pending_count(m: dict) -> int:
    """合并队列里还有问题没答完的卡数(判据 = question_pending_ids,单一事实源)。"""
    return len(question_pending_ids(m))


def merged_hint_md(m: dict) -> str:
    """「待你裁决」标题下的进度行。空队列时明说没有,不留一块空白让人猜。"""
    q = merged_review_queue(m)
    if not q:
        return "_本次没有待你裁决的条目(标注与成败,系统都给出了结论)。_"
    n_both = sum(1 for it in q if it["audit"] is not None
                 and it["task"] is not None)
    lines = [f"共 **{len(q)}** 条需要你看,其中 **{merged_pending_count(m)}** 条"
             "还有问题没答(「拿不准」算没答:它是「待定」不是结论)。"]
    if n_both:
        lines.append(f"其中 {n_both} 条标注与成败两个问题都有 —— 在同一张卡上"
                     "一起答,视频只用看一遍。")
    return "\n\n".join(lines)


#: 合并卡片上成败问题(②)的档位 → (说明文案, 按钮是否可用)。
#: hidden 档不在表里:整块不渲染,无文案可言。
SUCCESS_MODES = {
    "required": ("系统判不出这条的成败,**需要你给结论**。", True),
    "optional": ("你采纳了改标,**可以顺手给成败结论**(机器直接采信,不再重判);"
                 "留空则由机器按新标注重判。", True),
    "blocked": ("你在上面选了「弃用该条」—— 弃用的条目不再判成败"
                "(「这条不要了 + 判它成功」是自相矛盾的裁决)。"
                "要判成败,先把上面的裁决改掉。", False),
}


def success_block_mode(item: dict, label_decision: str) -> str:
    """成败问题(②)在合并卡片上的显隐档位(纯函数,UI 只照着渲染)。

    三条规矩 + 一条矛盾拦截(2026-08-16 用户定):
    - 机器弃权(条目在成败弃权队列里)→ required:必答,今天就是这样;
      ① 选了「维持原标注」也不降档 —— 标注没动,机器照旧判不出,问题还在;
    - 只有标注问题的条目,① 选了「采纳建议改标」→ optional:默认展开、可留空。
      留空 = 交给机器按新标注重判;答了 = 机器直接采信(rejudge 的对应规则),
      防的是第二轮:重判可能又判不出,用户下次还得重看同一段视频;
    - ① 选「维持原标注」/「弃用该条」或还没裁 → hidden:没有成败问题可答;
    - 矛盾拦截:① 选「弃用该条」时 ② 一律不可用。机器弃权的条目给 blocked
      (说明照显、按钮禁用)而不是 hidden —— 必答的问题凭空消失,用户会以为
      页面坏了。
    """
    if label_decision == "弃用该条":
        return "blocked" if item.get("task") is not None else "hidden"
    if item.get("task") is not None:
        return "required"
    if label_decision == "采纳建议改标":
        return "optional"
    return "hidden"


def record_task_verdict_checked(m: dict, episode_id: str, verdict: str,
                                note: str = "") -> str:
    """成败裁决落盘前的矛盾拦截。界面按钮已按 success_block_mode 禁用,这里再把
    一次门:按钮态是渲染出来的,连点竞态/陈旧页面都可能绕过它,而「弃用 + 判它
    成功」这种自相矛盾的裁决一旦落盘,rejudge 就会各按各的执行。"""
    dec = load_label_decisions(m).get(episode_id, {}).get("decision", "")
    if dec == "弃用该条":
        return (f"⚠️ 未记录:{episode_id} 已裁「弃用该条」,弃用的条目不再判成败"
                "(自相矛盾)。要判成败,先在标注问题里改掉「弃用」。")
    return record_task_verdict(m["path"], episode_id, verdict, note)


def audit_note_md(m: dict) -> str:
    """技能画像页留的一行指路(裁决面板已搬去「人工裁决」页)。"""
    q = m.get("audit_queue") or []
    if not q:
        return f"_本次没有「{AUDIT_TERM}」的条目。_"
    n = audit_pending_count(m)
    if not n:
        return (f"「{AUDIT_TERM}」共 **{len(q)}** 条,已全部裁决 → "
                "详见「**人工裁决**」页")
    return (f"**{n}** 条「{AUDIT_TERM}」待裁(共 {len(q)} 条)→ "
            "去「**人工裁决**」页处理")


# (task_review_hint_md 已删,2026-08-16:分区制的"先清标注再裁成败"工序提醒随
#  合并队列一起退役 —— 两个问题在同一张卡上一次答完,进度行见 merged_hint_md。)


def overview_markdown(m: dict) -> str:
    """总览页表格**之上**的那几行:身份 + 数据包完整性 + 一句导航。

    2026-08-13 起这里**一个数字都不说**:数字全在下面 overview_rows 那张表里。
    此前上半部是几行 bullet、下半部是一张表,同一批数字说两遍,还两处口径不同 ——
    用户原话"表格没有显示正确的质检结果信息"。
    """
    if m.get("load_error"):
        return "## 读不到这份交付\n\n" + m["load_error"]
    d = m.get("dataset") or {}
    # 机器人字段自 2026-07 起是 dict(型号+规格表+质量),概览一直在渲染原始
    # dict(2026-08-10 发现)——按报告身份行的同款人话格式化;老交付是纯字符串,原样。
    rb = m.get("robot")
    if isinstance(rb, dict):
        _q = f",质量 {rb.get('quality')}" if rb.get("quality") else ""
        rb = f"{rb.get('robot_type')}(规格表 {rb.get('registry_profile')}{_q})"
    # issue #58:裸数据集名不知道是什么 → 标题写明"数据集";"生成于"与
    # 「质检批次」下拉重复 → 删;"代码版本"没人看得懂 → 「质检程序版本」。
    lines = [f"# 数据集 {m['name']}",
             f"机器人 **{rb}** · 质检程序版本 {m['code_version']}",
             ""]
    # 数据包完整性(2026-08-10):容器缺了什么、按什么补的。它不是数字复读,是另一类
    # 信息(读任何数字之前该知道的前提),所以这一条留下。有 findings 才出,不占位。
    cf = (d.get("container") or {}).get("findings") or []
    if cf:
        _ic = {"正常": "✅", "缺失(已补)": "⚠️", "缺失(已溯源补全)": "✅",
               "降级": "⚠️", "缺失": "❌"}
        lines.append("- 数据包完整性:" + ";".join(
            f"{f.get('项')} {_ic.get(str(f.get('状态')), '')}{f.get('状态')}"
            for f in cf) + "(缺什么、按什么补的,详见质检报告)")
    # 唯一保留的导航(不带数字):有活要干才说,没有队列还催人去看等于骗人
    if any(e.get("pending") for e in (m.get("episodes") or {}).values()) \
            or m.get("audit_queue"):
        lines.append("- 需要人看一眼的条目都在「**人工裁决**」页,逐条看视频后裁定。")
    return "\n".join(lines)


#: discover_deliveries 的结果缓存:(根, 深度) → (过期时刻, 根目录 mtime_ns, 结果)。
#: 2026-08-20 公网实测:一次页面加载调它两遍(选交付一遍、加载内容又一遍),FSX 上
#: 55 份交付每遍 2.0-2.5 秒 —— 首屏那 5.5 秒的 SSE 流全耗在这。短 TTL + 根目录
#: mtime 双判据:顶层新交付一落盘 mtime 就变、立刻可见;嵌套交付最多晚 TTL 秒。
_DISCOVER_TTL_S = 5.0
_DISCOVER_CACHE: dict = {}


def clear_discover_cache() -> None:
    """清掉交付扫描缓存(测试夹具用;生产不需要——TTL 与 mtime 自己会失效)。"""
    _DISCOVER_CACHE.clear()


def discover_deliveries(root: str, max_depth: int = 3) -> list[str]:
    """root 本身是交付 → [root];否则递归扫子目录(默认 3 层)找出所有交付。

    "是不是一份交付"两种形态都算(2026-08-14 布局变更):新布局 = 目录下有一个或
    多个时间戳跑批子目录;老布局 = passed.json 直接躺在目录里。**返回的是交付目录,
    不是某一次跑批** —— 报告页顶部那个下拉列的是交付名,几十次跑批平铺成几十个
    条目正是这次要改掉的东西。

    2026-08-06 从"只扫一层"改递归:用户把交付放在嵌套目录(如 experiments/run1/)
    时曾整个不可见,看起来像 UI 坏了。找到交付即不再往其内部钻(跑批子目录、
    details/ 里不会再有交付)。

    2026-08-20 两处提速(公网首屏实测 5.5 秒的病根):①结果按 TTL+根 mtime 缓存
    (见 _DISCOVER_CACHE);②每个候选目录只 listdir 一次,用这份清单判老布局/
    新布局,跑批子目录**从新到旧**逐个验 passed.json、命中即停 —— 原来是
    isdir + exists(passed.json) 逐个 stat,FSX 上几百次元数据往返。
    """
    import time as _time

    from ..delivery import MARKER, is_run_name
    root = os.path.abspath(root)
    try:
        root_mt = os.stat(root).st_mtime_ns
    except OSError:
        root_mt = None
    key = (root, max_depth)
    now = _time.monotonic()
    hit = _DISCOVER_CACHE.get(key)
    if hit and hit[0] > now and hit[1] == root_mt:
        return list(hit[2])

    def _is_delivery_listing(d: str, names: list) -> bool:
        if MARKER in names:
            return True
        for n in sorted((x for x in names if is_run_name(x)), reverse=True):
            if os.path.exists(os.path.join(d, n, MARKER)):
                return True
        return False

    def _listing(d: str):
        try:
            with os.scandir(d) as it:
                return [(e.name, e.is_dir()) for e in it]
        except OSError:
            return None

    top = _listing(root)
    if top is not None and _is_delivery_listing(root, [n for n, _ in top]):
        out = [root]
    else:
        out = []

        def _walk(d: str, entries, depth: int):
            if depth > max_depth or entries is None:
                return
            for name, isdir in sorted(entries):
                if not isdir:
                    continue
                p = os.path.join(d, name)
                sub = _listing(p)
                if sub is None:
                    continue
                if _is_delivery_listing(p, [n for n, _ in sub]):
                    out.append(p)             # 是交付:收下,不再往里钻
                else:
                    _walk(p, sub, depth + 1)

        _walk(root, top, 1)
    _DISCOVER_CACHE[key] = (now + _DISCOVER_TTL_S, root_mt, list(out))
    return out


def most_recent_delivery(root: str, paths: list | None = None) -> str:
    """这些交付里最近跑过的那份(报告页下拉的默认选中,2026-08-25 复盘 ⑥)。

    此前默认取扫描序第一个 = 字母序(aloha-10 永远排前),交付一多,用户每次
    进报告页都要手动找刚跑完的那份。「质检批次」维度早已默认最近一次,交付
    维度同理。新旧判据 = 最新跑批子目录名(时间戳串,字典序即时间序);老布局
    (三件套直接在目录里)退回目录 mtime。只 listdir 一轮,几十份交付毫秒级。
    """
    from ..delivery import is_run_name
    best, best_key = "", ("", 0.0)
    for p in (paths if paths is not None else discover_deliveries(root)):
        p = str(p)
        try:
            runs = [n for n in os.listdir(p) if is_run_name(n)]
        except OSError:
            continue
        if runs:
            key = (max(runs), 0.0)
        else:
            try:
                key = ("", os.stat(p).st_mtime)
            except OSError:
                continue
        if key > best_key:
            best, best_key = p, key
    return best


def delivery_choices(root: str, paths: list | None = None) -> list:
    """交付下拉的 [(显示名, 目录路径)]。显示名 = 相对扫描根的路径(如 droid-200-full)。

    显示相对路径而不是绝对路径,是因为交付根前缀(/mnt/tos/deliveries/)每一项都
    一样,占满半个下拉却不提供任何区分度;嵌套摆放的交付显示成 experiments/run1,
    仍然唯一。值仍是绝对路径 —— 手输完整路径是既有能力,不许砍。
    """
    base = os.path.abspath(str(root or "."))
    out = []
    for p in (paths if paths is not None else discover_deliveries(root)):
        ap = os.path.abspath(str(p))
        rel = os.path.relpath(ap, base) if ap.startswith(base + os.sep) else ap
        out.append((rel, ap))
    return out


# ───────── 明细表(D1,2026-07-28):details/ 下 CSV 的只读渲染 ─────────

DETAIL_LABELS = {                      # 语义化标签(纪律:界面不出现实现名)
    "motion_details.csv": "运动质量明细(逐子项)",
    "visual_details.csv": "视觉质量明细(逐相机)",
    "kinematic_details.csv": "运动学违规明细",
    "stuck_details.csv": "卡死事件明细",
    # 老交付没有这张表 → list_detail_tables 只列实际存在的文件,自然降级不报错
    "skill_assignment.csv": "技能归属明细(逐 episode 属于哪个技能)",
    "vlm_latency.csv": "VLM 调用延时明细(逐请求)",
}


#: 逐相机的视觉质量表 = 「视频打分明细」子页的全部内容(2026-08-13 用户定)。
#: 它原先混在明细页那个下拉里,客户根本翻不到 —— 而"每台相机拍得清不清楚"是
#: 判断这份数据能不能用的直接证据,不该藏在一个要先点开才知道有什么的下拉后面。
#: 单独成页之后下拉里那一条同时撤掉:同一份数据不留两个入口。
VIDEO_DETAIL_TABLE = "visual_details.csv"

#: 子页标题一致的说法(下面那行行数说明用它)。DETAIL_LABELS 里那条老标签留着不动
#: —— load_detail_table 的白名单还认它,而白名单是安全边界。
VIDEO_DETAIL_TITLE = "视频打分明细(逐相机)"

#: 交付里没有这张表时的空态。**只说交付里缺什么**,不写配置键名 —— NO_PLOTS_NOTE
#: 那次的教训:开关名是我们的实现细节,客户既不知道去哪改、也不该被要求知道。
NO_VIDEO_DETAILS_NOTE = ("这次跑批没有视频打分明细(`details/visual_details.csv` "
                         "不存在)——这次没跑视觉质量检查,或者它是更早版本跑出来的交付。")


def list_detail_tables(m: dict) -> list[str]:
    """交付里实际存在的明细 CSV(按 DETAIL_LABELS 顺序;缺的不列)。"""
    det = os.path.join(m["path"], "details")
    return [f for f in DETAIL_LABELS if os.path.exists(os.path.join(det, f))]


def detail_table_choices(m: dict) -> list[str]:
    """明细页那个下拉里该出现哪几张表:实际存在的,**减去已经单独成页的那张**。

    同一份数据不留两个入口:两个入口就是两处要同步维护,迟早对不上(而对不上的
    那一侧客户还会以为是数据出了问题)。
    """
    return [t for t in list_detail_tables(m) if t != VIDEO_DETAIL_TABLE]


def video_detail_view(m: dict) -> tuple:
    """「视频打分明细」子页的三件套 (说明, 表头, 行)。没有这份 CSV → 空态一句话。

    空态照现有写法给一个"(无)"表头:Dataframe 的 headers 传空列表会渲染成一张
    没有列的空壳,看着像页面坏了。
    """
    if not m or not m.get("path"):
        return NO_VIDEO_DETAILS_NOTE, ["(无)"], []
    headers, rows, total = load_detail_table(m, VIDEO_DETAIL_TABLE)
    if not headers:
        return NO_VIDEO_DETAILS_NOTE, ["(无)"], []
    note = f"**{VIDEO_DETAIL_TITLE}** · 共 {total} 行" + (
        f"(仅显示前 {len(rows)} 行,完整文件见本次跑批目录下的 details/{VIDEO_DETAIL_TABLE})"
        if total > len(rows) else "")
    return note, headers, rows


def load_detail_table(m: dict, name: str, cap: int = 2000):
    """CSV → (表头, 行, 总行数)。行数封顶 cap 防大数据集拖垮页面(总数照报)。"""
    import csv as _csv
    path = os.path.join(m["path"], "details", name)
    if name not in DETAIL_LABELS or not os.path.exists(path):
        return [], [], 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = _csv.reader(f)
        headers = next(reader, [])
        rows, total = [], 0
        for row in reader:
            total += 1
            if total <= cap:
                rows.append(row)
    return headers, rows, total


# ───────── Stuck 时间线(D2,2026-07-28):三态彩条 HTML 渲染 ─────────

TL_COLORS = {"stuck": "#F53F3F", "idle": "#FF7D00", "normal": "#00B42A"}
TL_LABELS = {"stuck": "stuck(指令在推而不动)", "idle": "idle(无指令静止)",
             "normal": "正常(在干活)"}


def load_timeline(m: dict) -> dict:
    """details/episodes_timeline.json → {episodes, 口径, 数据集注记};
    无文件返回空(老交付)。dataset_note 来自数据集 profile 的 extras.note,原样
    透传(如 bridge 的 state 由 action 合成),老交付/无注记的数据集为空串。"""
    d = _load_json(os.path.join(m["path"], "details", "episodes_timeline.json"))
    return {"episodes": d.get("episodes") or {}, "note": d.get("口径", ""),
            "dataset_note": d.get("dataset_note", "")}


#: 时间线的筛选口径 → (界面说法, 判定函数)。默认 both = 有 stuck 或 idle 的都列。
TL_FILTERS = {
    "both": "stuck + idle",
    "stuck": "只看 stuck",
    "idle": "只看 idle",
    "all": "全部 episode",
}

#: 时间线的排序口径 → 界面说法。默认按 episode 序号(录制顺序,便于跟原始数据对照);
#: 按卡顿时长降序则把最该复查的顶到最前 = 图形化的人工复查队列。
TL_SORTS = {"episode": "episode 序号", "stuck": "卡顿时长(长的在前)"}


def timeline_html(tl: dict, cap: int = 200, show: str = "both",
                  sort: str = "episode") -> str:
    """时间线 → HTML 彩条列表。纯函数(可测)。

    2026-07-28 用户定稿:①段界时间直接标注在条下方(悬停仍有精确起止;标签
    间距 <4% 条宽的自动跳过防挤,右端时长恒标)。
    2026-08-13 用户定稿:②筛选与排序都交给用户 —— `show` 见 TL_FILTERS
    (默认 stuck+idle,全绿的不占屏),`sort` 见 TL_SORTS(**默认 episode 序号**,
    与原始数据的条目顺序一致;要当复查队列用就切「卡顿时长」)。"""
    eps = tl.get("episodes") or {}
    if not eps:
        return ("<p>此交付无时间线数据(episodes_timeline.json)——需要跑过"
                "运动质量检查的新版交付。</p>")
    # 数据集注记(2026-07-29 用户定):看彩条前必须知道的前提(如 bridge 的 state
    # 由 action 累加合成 → 指令-实际无独立信息,stuck 只能弃权,黄条的含义随之变)。
    # 有才渲染,没有不占位;内容原样来自数据集 profile 的 extras.note,UI 不做判断。
    note_html = (f'<p style="margin:0 0 6px 0;color:#D25F00;background:#FFF7E8;'
                 f'border-left:3px solid #FF7D00;padding:6px 10px">'
                 f'数据集注记:{tl["dataset_note"]}</p>'
                 if tl.get("dataset_note") else "")
    def _tot(e: str, k: str) -> float:
        return eps[e].get("totals", {}).get(k, 0) or 0

    keep = {"both": lambda e: _tot(e, "stuck") > 0 or _tot(e, "idle") > 0,
            "stuck": lambda e: _tot(e, "stuck") > 0,
            "idle": lambda e: _tot(e, "idle") > 0,
            "all": lambda e: True}.get(show, lambda e: True)
    shown_eps = [e for e in eps if keep(e)]
    if not shown_eps:
        empty = {"stuck": f"全部 {len(eps)} 条 episode 均无 stuck",
                 "idle": f"全部 {len(eps)} 条 episode 均无 idle"}.get(
                     show, f"全部 {len(eps)} 条 episode 均无 stuck/idle")
        return note_html + f"<p>{empty}——录制卫生良好 ✅</p>"
    if sort == "stuck":
        order = sorted(shown_eps, key=lambda e: (-_tot(e, "stuck"), -_tot(e, "idle"), e))
    else:
        order = sorted(shown_eps)      # episode 序号:id 是零填充的,字典序即序号序
    legend = " ".join(
        f'<span style="display:inline-block;width:12px;height:12px;'
        f'background:{TL_COLORS[s]};margin-right:4px;vertical-align:middle"></span>'
        f'<span style="margin-right:16px">{TL_LABELS[s]}</span>'
        for s in ("stuck", "idle", "normal"))
    rows = ([note_html] if note_html else []) + [
        f'<div style="margin:6px 0 14px 0">{legend}</div>']
    for eid in order[:cap]:
        t = eps[eid]
        dur = t.get("duration_s") or 0
        if dur <= 0:
            continue
        tot = t.get("totals", {})
        segs = t.get("segments") or []
        segs_html = "".join(
            f'<div title="{TL_LABELS.get(s["state"], s["state"])} '
            f'{s["start_s"]}–{s["end_s"]}s" '
            f'style="width:{max(0.2, (s["end_s"] - s["start_s"]) / dur * 100):.2f}%;'
            f'background:{TL_COLORS.get(s["state"], "#999")}"></div>'
            for s in segs)
        # 段界时间标注(2026-07-28 用户二次定稿:全部分界都标;**默认同一水平线
        # (条下方)**,与同行前一标签间距 <4% 条宽会重叠时,该标签放到 **bar 上方**
        # 的溢出行;上方也挤则挑更宽松的一行,宁可微叠不丢标)
        marks_below, marks_above = [], []
        last_below, last_above = -10.0, -10.0
        bounds = [0.0] + [seg["end_s"] for seg in segs]
        for j, b in enumerate(bounds):
            pct = min(b / dur * 100, 100.0)
            txt = f"{b:g}s" if j == len(bounds) - 1 else f"{b:g}"
            pos = ('right:0' if pct > 97 else
                   f'left:{pct:.2f}%;transform:translateX(-50%)')
            span = f'<span style="position:absolute;{pos}">{txt}</span>'
            if pct - last_below >= 4:
                marks_below.append(span); last_below = pct
            elif pct - last_above >= 4:
                marks_above.append(span); last_above = pct
            elif pct - last_below >= pct - last_above:
                marks_below.append(span); last_below = pct
            else:
                marks_above.append(span); last_above = pct
        above_html = (f'<div class="tl-above" style="position:relative;height:12px;'
                      f'font:10px monospace;color:#777">{"".join(marks_above)}</div>'
                      if marks_above else "")
        label = (f'{eid} · {dur:.1f}s'
                 + (f' · stuck {tot.get("stuck", 0)}s' if tot.get("stuck") else "")
                 + (f' · idle {tot.get("idle", 0)}s' if tot.get("idle") else ""))
        rows.append(
            f'<div style="margin:4px 0 10px 0">'
            f'<div style="font:12px monospace;margin-bottom:2px">{label}</div>'
            f'{above_html}'
            f'<div style="display:flex;height:16px;border-radius:3px;'
            f'overflow:hidden;border:1px solid #ddd">{segs_html}</div>'
            f'<div style="position:relative;height:13px;font:10px monospace;'
            f'color:#777">{"".join(marks_below)}</div></div>')
    if len(order) > cap:
        rows.append(f"<p>…共 {len(order)} 条,按当前排序只显示前 {cap} 条</p>")
    if len(eps) > len(order):
        rows.append(f'<p style="color:#777">另有 {len(eps) - len(order)} 条不在'
                    f'当前筛选范围内(筛选切到「{TL_FILTERS["all"]}」可见)</p>')
    return "\n".join(rows)


# ───────── 性能剖析(P1,2026-07-30):VLM 后端 / 运行环境 / 延时剖析 ─────────
#
# ★ 界面红线:**绝不出现预设代号**(那是机房黑话,客户看不懂也不该看懂)。
#   后端一律用「服务端点 URL + 模型名 + 服务类型 + 硬件型号」四件套表述。
#   硬件型号只能来自交付记录本身(runtime.vlm_backend.hardware,源头是站点配置的
#   vlm_backends.*.hardware)——本模块**没有也不许有**"端点→型号"的硬编码映射表:
#   那种表会在换机器后继续输出旧型号,拿 A 机的规格解释 B 机的延时。

#: 交付里没记这一项时的统一措辞(老交付走这条)。
NOT_RECORDED = "未记录(旧版本交付)"

#: VLM 调用类型 → 语义化中文标签。2026-08-15 起与客户报告共用 vlm_call_kinds
#: 单一事实源(此前界面/报告各一套名字,报告那套还是中英夹杂的内部说法)。
#: 那是零依赖常量模块,不破"UI 不 import 管道"红线(先例:dataset_level/decisions)。
#: 保留 LATENCY_LABELS 这个名字作别名:模块内多处及测试按此引用。
#: dict 顺序 = 流程顺序(方案 B):条形图与解释都按漏斗发生的先后排。
#: ⚠️ 新增一类调用时要加进 vlm_call_kinds,否则兜底逻辑把英文标签原样透出
#: (2026-08-14 arbitration 实翻过车)。
from ..vlm_call_kinds import (CALL_KIND_LABELS, CALL_KIND_NOTES,  # noqa: E402
                              CALL_KIND_ORDER)

LATENCY_LABELS = CALL_KIND_LABELS

#: 延时口径(一句话,跟着表格一起显示)——不写清楚会被当成服务端推理耗时。
LATENCY_NOTE = (
    "下表是**单次调用**的耗时分布(从发出请求到收完响应,含网络与排队)。"
    "整类调用实际占了多久看下面的条形图 —— 多个调用在时间上重叠,"
    "拿次数乘均值会严重高估。")

LATENCY_PCTL_NOTE = (
    "_P50 = 一半的调用不超过此耗时;P90/P99 同理,越靠后越反映最慢的少数调用。_")

#: 五类调用各是干什么的(表下说明,一行一条;文案在 vlm_call_kinds,与报告同一份)。
#: 语义化名字必须配得上解释,否则"任务完成度打分 1583 次"客户没法判断合不合理。
LATENCY_KIND_NOTE = "\n".join(
    ["**这五类调用各是什么**(按流程先后)"]
    + [f"- **{CALL_KIND_LABELS[t]}**:{CALL_KIND_NOTES[t]}" for t in CALL_KIND_ORDER])

#: 横条图配色(五类各一色;与判决用的红/绿色系错开,避免被误读成"好坏")。
_BAR_COLORS = {"probe": "#165DFF", "endstate": "#722ED1", "arbitration": "#14C9C9",
               "caption": "#00B42A", "llm": "#FF7D00"}

#: 说明文字用 Arco 次要文字灰。⚠️ 不用错误红 #F53F3F(2026-08-15 用户点名):
#: 超时补发是系统在正常兜底,红色"失败 N"读着像这一步整个失败了。
_MUTED = "#86909C"


def infer_service_type(endpoint: str | None) -> str:
    """endpoint → 服务类型(仅当交付里没显式声明 service_type 时的兜底)。

    纯字符串判断、零网络。只粗判"部署形态",判不出就老实写「OpenAI 兼容服务」——
    宁可说不知道,也不编一个具体的服务名。
    """
    if not endpoint:
        return "未记录"
    from urllib.parse import urlparse
    host = (urlparse(endpoint).hostname or "").lower()
    if not host:
        return "OpenAI 兼容服务"
    if (host.endswith(".svc.cluster.local") or host in ("localhost", "127.0.0.1")
            or host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18."))):
        return "自托管推理服务(集群内)"
    if host.endswith("volces.com"):
        return "方舟 MaaS(托管服务)"
    return "OpenAI 兼容服务"


def load_perf(m: dict) -> dict:
    """交付 → 性能剖析三块数据(后端 / 运行环境 / 延时)。纯函数。

    降级策略(老交付):runtime 块是 2026-07-30 之后的新交付才有。缺块时从
    config_effective **尽力**还原端点/模型/三个并发值(那是配置快照里本来就有的),
    硬件与运行环境则如实标"未记录"——绝不从端点反查型号。
    """
    rt = m.get("runtime") or {}
    be = dict(rt.get("vlm_backend") or {})
    env = dict(rt.get("environment") or {})
    legacy = not be
    if legacy:                                   # 老交付:退回配置快照能给的部分
        ce = m.get("config_effective") or {}
        vlm = ((ce.get("checks") or {}).get("task_success") or {}).get("vlm") or {}
        be = {"endpoint": vlm.get("endpoint"), "model": vlm.get("model"),
              "hardware": None, "service_type": None,
              "episode_concurrency": (ce.get("pipeline") or {}).get("vlm_episode_concurrency"),
              "frame_concurrency": vlm.get("max_concurrency"),
              "caption_concurrency": (ce.get("skill_profile") or {}).get("caption_concurrency")}
    if not be.get("service_type"):
        be["service_type"] = infer_service_type(be.get("endpoint"))
    lat = (m.get("dataset") or {}).get("vlm_latency") or {}
    fresh = _recompute_latency(os.path.join(m.get("path") or "", "details",
                                            "vlm_latency.csv"))
    if fresh:
        lat = fresh          # 逐请求明细在手就现场复算——快照口径旧了也能自愈
    return {"backend": be, "env": env, "legacy": legacy, "latency": lat,
            "total_wall_s": rt.get("total_wall_s")}


def _pctl_(xs: list, q: float) -> float:
    i = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[i]


def _recompute_latency(csv_path: str) -> dict:
    """details/vlm_latency.csv → 按类汇总(与 vlm_client.latency_summary 同款算法,
    在 UI 侧独立实现——UI 不 import 管道的红线;两实现有对拍测试钉住)。

    wall_s = 忙碌区间并集(空档不计):旧快照用"首发→末返"跨度,分段跑的类别
    (caption 补标+画像两波)会把中间隔的别的阶段全灌进来,条形图严重失真
    (2026-08-06 droid-30 实锤)。读不到/读坏 CSV → 返回 {},上层退回快照。
    """
    import csv as _csv
    try:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                st = (r.get("started_at") or "").strip()
                cid = (r.get("call_id") or "").strip()
                att = (r.get("attempt") or "").strip()
                rows.append((r["call_type"], float(r["seconds"]),
                             bool(int(r["ok"])), float(st) if st else None,
                             cid or None, int(att) if att else 0,
                             (r.get("fail_kind") or "").strip()))
    except (OSError, KeyError, ValueError):
        return {}
    out: dict = {}
    for tag in sorted({r[0] for r in rows}):
        mine = [r for r in rows if r[0] == tag]
        oks = sorted(r[1] for r in mine if r[2])
        entry = {"n": len(oks), "errors": sum(1 for r in mine if not r[2]),
                 "attempts": len(mine)}
        # 对冲口径(2026-08-15,与 vlm_client.latency_summary 同款算法):
        # 同 call_id 的两发 = 一次逻辑调用;老 CSV 没有 call_id → 每行自成一组,
        # 此时 unanswered==errors,老交付数字不变
        groups: dict = {}
        for i, r in enumerate(mine):
            groups.setdefault(r[4] if r[4] is not None else f"_row{i}", []).append(r)
        hedged = retried = unanswered = unanswered_timeout = 0
        for g in groups.values():
            first = min(g, key=lambda r: r[5])
            if any(r[5] > 0 for r in g):
                if first[2] or first[6] == "timeout":
                    hedged += 1
                else:
                    retried += 1
            if not any(r[2] for r in g):
                unanswered += 1
                if all(r[6] == "timeout" for r in g):
                    unanswered_timeout += 1
        entry.update({"hedged": hedged, "retried": retried,
                      "unanswered": unanswered,
                      "unanswered_timeout": unanswered_timeout})
        if oks:
            entry.update({"mean_s": round(sum(oks) / len(oks), 2),
                          "p50_s": round(_pctl_(oks, 0.50), 2),
                          "p90_s": round(_pctl_(oks, 0.90), 2),
                          "p99_s": round(_pctl_(oks, 0.99), 2),
                          "max_s": round(oks[-1], 2)})
        stamped = [r for r in mine if r[3] is not None]
        if stamped:
            ivs = sorted((r[3], r[3] + r[1]) for r in stamped)
            busy, cs, ce = 0.0, ivs[0][0], ivs[0][1]
            for s, e in ivs[1:]:
                if s > ce:
                    busy += ce - cs
                    cs, ce = s, e
                else:
                    ce = max(ce, e)
            busy += ce - cs
            entry["wall_s"] = round(busy, 2)
        out[tag] = entry
    return out


def _hardware_text(perf: dict) -> str:
    """硬件型号的展示文本。只有两个来源:交付记录里写了,或托管服务本来就不可见。"""
    hw = perf["backend"].get("hardware")
    if hw:
        return str(hw)
    st = perf["backend"].get("service_type") or ""
    # ⚠️「自托管」里含「托管」二字:必须先排除,否则自建的 GPU 服务会被说成
    # "硬件不可见"(2026-07-30 测试当场抓到的字串陷阱)。
    if "MaaS" in st or ("托管" in st and "自托管" not in st):
        return "托管服务,硬件不可见(由服务商调度)"
    return NOT_RECORDED if perf["legacy"] else "未记录(站点配置未声明 hardware)"


def _val(v) -> str:
    return "未记录" if v in (None, "") else str(v)


def perf_backend_md(perf: dict) -> str:
    """第一块:VLM 后端卡片(Markdown 表)。"""
    b = perf["backend"]
    ep = b.get("endpoint")
    rows = [
        ("服务端点", f"`{ep}`" if ep else "未记录"),
        ("模型名", _val(b.get("model"))),
        ("服务类型", _val(b.get("service_type"))),
        ("硬件型号", _hardware_text(perf)),
        ("episode 并发(同时处理几条数据)", _val(b.get("episode_concurrency"))),
        ("单条内帧并发(一条数据内同时问几帧)", _val(b.get("frame_concurrency"))),
        ("打标并发(技能打标同时跑几条)", _val(b.get("caption_concurrency"))),
    ]
    head = "### VLM 后端\n\n| 项 | 本次运行取值 |\n|---|---|\n"
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    tail = "\n\n_硬件型号来自本次交付的运行记录(站点配置声明),不是界面推测的。_"
    return head + body + tail


def perf_env_md(perf: dict) -> str:
    """第二块:运行环境(质检管线所在容器的 CPU 侧)。老交付整块"未记录"。"""
    env = perf["env"]
    if not env:
        return ("### 运行环境(质检管线容器)\n\n" + NOT_RECORDED
                + "——运行环境是 2026-07-30 之后的新交付才记录的字段,"
                  "此交付跑批时管线尚未采集。")
    cpu = env.get("cpu_limit_cores")
    mem = env.get("memory_limit_bytes")
    node = env.get("node")
    src = env.get("node_source")
    node_txt = _val(node)
    if node and src == "hostname":
        node_txt += "(容器 hostname;未注入 NODE_NAME,故非节点名)"
    elif node and src == "NODE_NAME":
        node_txt += "(取自调度注入的节点名)"
    rows = [("CPU 配额", f"{cpu} 核" if cpu else "未记录(非容器环境或未设限)"),
            ("内存配额", f"{mem / (1 << 30):.1f} GiB" if mem else "未记录(非容器环境或未设限)"),
            ("运行节点", node_txt)]
    head = "### 运行环境(质检管线容器)\n\n| 项 | 值 |\n|---|---|\n"
    tail = ("\n\n_这里是**管线自己**的 CPU 侧资源(抽帧解码/数值检查在此消耗);"
            "VLM 推理的算力在上面那张卡片的服务端。_")
    return head + "\n".join(f"| {k} | {v} |" for k, v in rows) + tail


#: 次数列口径 = **发起次数**(每一次真实发出的请求,含失败与补发)。2026-08-15
#: 前这里用 n(只数成功),与图上的总数对不上——同一页两套口径,用户实见 1192/1194。
LATENCY_HEADERS = ["调用类型", "发起次数", "平均响应时间(秒)",
                   "P50 响应时间(秒)", "P90 响应时间(秒)", "P99 响应时间(秒)"]


def _attempts_of(s: dict) -> int:
    """发起次数;老快照没有 attempts 字段 → 成功数+失败数(同一口径的降级还原)。"""
    return s.get("attempts", (s.get("n") or 0) + (s.get("errors") or 0))


def latency_rows(perf: dict) -> list[list]:
    """第三块:延时表。按流程顺序(LATENCY_LABELS 的 dict 序),缺的桶不占行;
    未知标签兜底排在最后。"""
    lat = perf["latency"]
    order = [t for t in LATENCY_LABELS if t in lat]
    order += [t for t in sorted(lat) if t not in LATENCY_LABELS]
    rows = []
    for tag in order:
        s = lat.get(tag) or {}
        rows.append([LATENCY_LABELS.get(tag, tag), _attempts_of(s),
                     _val(s.get("mean_s")), _val(s.get("p50_s")),
                     _val(s.get("p90_s")), _val(s.get("p99_s"))])
    return rows


#: 老交付(2026-07-30 之前)没记调用发出时刻 → 算不出墙钟。此时**不画图**:
#: 退回"次数 × 均值"那种条形图会把并发跑的 8 分钟说成 8 小时,宁可空着。
NO_WALL_NOTE = ("此交付未记录调用时刻(旧版本),无法计算墙钟;新交付起提供。")


def human_duration(sec: float) -> str:
    """秒 → 人读的时长。<60s 保留一位小数,再往上按 分/小时 拆(演示要能念出来)。"""
    if sec < 60:
        return f"{sec:.1f} 秒"
    total = int(round(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h} 小时 {m} 分 {s} 秒" if h else f"{m} 分 {s} 秒"


def latency_bar_html(perf: dict) -> str:
    """第三块配图:纯 HTML/CSS 横条图,条长 = 该类调用的**墙钟**。

    ★ 口径只有一个:墙钟 = 忙碌区间并集(该类调用真实在飞的净时长,阶段间空档
    不计;见 vlm_client.latency_summary 的 wall_s)。**不存在**"次数 ×
    均值"的回退画法:我们是并发跑的,那个乘积是几十倍的高估,画出来就是误导。
    没有墙钟数据(老交付)= 不画图 + 一句说明。
    """
    lat = perf["latency"]
    # 总墙钟(2026-08-06 用户点名):整次 run 从启动到交付可用的真实流逝时间,
    # 含 CPU 检查/VLM/导出/落盘回验——回答"这批到底跑了多久"的唯一口径。
    tw = perf.get("total_wall_s")
    total_line = (f"<br>整次运行总墙钟 <b>{human_duration(tw)}</b>"
                  f"(含全部阶段与交付落盘)" if tw else "")
    if not lat:
        return ('<p style="color:#777">本次运行没有 VLM 调用(例如只跑了数值类检查),'
                '因此没有延时数据。</p>')
    # 条形顺序 = 流程顺序(2026-08-15 用户选方案 B):不再按墙钟从长到短排,
    # 谁最费时靠条长看;未知标签兜底排最后
    _flow = [t for t in LATENCY_LABELS if t in lat] + \
            [t for t in sorted(lat) if t not in LATENCY_LABELS]
    items = [(tag, float(lat[tag]["wall_s"]), lat[tag]) for tag in _flow
             if lat[tag].get("wall_s") is not None]
    if not items:
        return f'<p style="color:#777">{NO_WALL_NOTE}</p>'
    top = max(max(w for _, w, _s in items), 1e-9)
    bars = []
    total_unanswered = 0
    for tag, wall, s in items:
        pct = max(1.0, wall / top * 100)
        label = LATENCY_LABELS.get(tag, tag)
        att = _attempts_of(s)
        cnt = f"发起 {att} 次,并发执行" if att > 1 else f"发起 {att} 次"
        # 补发/没拿到结果的说明(2026-08-15 用户定):按对冲口径说人话,不再用
        # "失败 N"的红字——超时补发是系统在正常兜底,不是这一步失败了。
        # 补发次数必须可见:它是判断服务端质量的唯一线索(补发率 3%→30% = 服务
        # 在恶化,不该被"反正救回来了"藏掉)。
        hedged, retried = s.get("hedged", 0), s.get("retried", 0)
        unans = s.get("unanswered", s.get("errors") or 0)
        total_unanswered += unans
        parts = []
        if hedged:
            parts.append(f"{hedged} 次超时后补发")
        if retried:
            parts.append(f"{retried} 次服务报错后重发")
        if parts and not unans:
            parts.append("均已拿到结果")
        if unans:
            # 失败原因全是超时才说"没等到回应";原因未记录(旧交付)/混有服务报错
            # 时只说"没拿到结果",不编原因
            known_all_timeout = ("unanswered" in s
                                 and s.get("unanswered_timeout", 0) == unans)
            parts.append(f"{unans} 次{'没等到回应' if known_all_timeout else '没拿到结果'}")
        note_txt = (f' · <span style="color:{_MUTED}">{",".join(parts)}</span>'
                    if parts else "")
        bars.append(
            f'<div style="margin:8px 0">'
            f'<div style="font:12px/1.5 system-ui;margin-bottom:2px">{label}'
            f' — 墙钟 <b>{human_duration(wall)}</b>({cnt}){note_txt}</div>'
            f'<div style="background:#E5E6EB;border-radius:4px;overflow:hidden;height:14px">'
            f'<div style="width:{pct:.2f}%;height:100%;'
            f'background:{_BAR_COLORS.get(tag, "#888")}"></div></div></div>')
    # 后果说明只在真有没拿到结果的调用时出现(2026-08-15 用户点名):
    # 少一票只会让判定更保守,不会因此错杀 —— 铁律"救人一签、杀人双签"不因超时而变
    tail_unans = ('<div style="font:12px system-ui;color:' + _MUTED + ';margin-top:6px">'
                  '没拿到结果的调用 = 相应判定少一票:判定只会更保守'
                  '(拿不准就弃权进人工复核),不会因此错杀。</div>'
                  if total_unanswered else "")
    return ('<div style="max-width:760px">'
            '<div style="font:12px system-ui;color:#555;margin-bottom:4px">'
            '各类调用的墙钟耗时(忙碌区间并集:该类调用真实在飞的净时长,空档不计;'
            '按流程顺序排列)'
        + total_line + '</div>'
            + "".join(bars)
            + tail_unans
            + '<div style="font:12px system-ui;color:#777;margin-top:6px">'
              '各类调用之间在时间上可能重叠,各条墙钟相加 ≠ 整次运行总时长。</div>'
            '</div>')


# ── 人工裁决:实现在 dataset_level/decisions.py(与 rejudge 命令共用同一份)。
#    那是纯文件 IO 层,不是管道——UI 不 import 管道的红线在此不破。 ──
from ..dataset_level import decisions as _dec
from ..dataset_level.decisions import (APPEAL_CHOICES, APPEALS_CSV,  # noqa: F401
                                       DECISION_CHOICES, DECISIONS_CSV,
                                       VERDICT_CHOICES, VERDICT_HOLD,
                                       VERDICTS_CSV,
                                       is_task_success_reject,
                                       record_label_decision, record_reject_appeal,
                                       record_task_verdict)
from ..dataset_level.decisions import load_label_decisions as _load_decisions
from ..dataset_level.decisions import load_reject_appeals as _load_appeals
from ..dataset_level.decisions import load_task_verdicts as _load_verdicts


def load_label_decisions(m: dict) -> dict:
    return _load_decisions(m["path"])


def load_task_verdicts(m: dict) -> dict:
    return _load_verdicts(m["path"])


def load_reject_appeals(m: dict) -> dict:
    return _load_appeals(m["path"])


# ── 裁决的已应用/未应用与沿用(2026-08-16)──────────────────────────────
#
# 判据全部走 dataset_level/decisions.py 的 decisions_view(与 rejudge 的幂等跳过
# **同源**,不许在 UI 里另写一套比对);本段只做措辞。三处显示的共同纪律:
# 无裁决时一律返回空串 —— 空提示占着位置只会让人以为自己漏看了什么。

def decision_status(m: dict) -> dict:
    """当前 manifest 指着的那次跑批 → 裁决逐条状态 + 计数。

    **每次调用现读三张裁决 CSV**(几十行的小文件):裁决卡片上点一下就落一行新
    裁决,靠 manifest 里的缓存必然陈旧;溯源快照(prov_files)倒是随 manifest
    走 —— 三件套只有 rejudge 会改,改完界面本来就要重载。
    """
    if m.get("load_error") or not m.get("path"):
        return {"records": [], "counts": _dec.application_counts([])}
    files = m.get("prov_files") or {}
    records = _dec.decisions_view(files,
                                  _load_decisions(m["path"]),
                                  _load_verdicts(m["path"]),
                                  _load_appeals(m["path"]),
                                  run_started=_dec.run_started_at(m["path"]))
    return {"records": records, "counts": _dec.application_counts(records)}


def unapplied_banner_md(m: dict) -> str:
    """质检报告页顶部的「有裁决尚未应用」提醒(没有未应用的 → 空串,不占位)。

    防的事故:跑完新一批忘了点「执行裁决」,交出去的就是把人的决定全丢掉的
    数据,而报告不会吭声。2026-08-23 用户定版式:三个数一次说清——已裁(记录数)、
    待裁(队列里还有问题没答的条目数)、未应用(已裁里没落地的)。只在有未应用
    时出现:它是警报不是仪表盘,待裁多少队列页自己会说。
    """
    c = decision_status(m)["counts"]
    if not c["unapplied"]:
        return ""
    return (f"⚠️ 人工裁决:已裁 {c['total']} 条 · 待裁 {merged_pending_count(m)} 条 · "
            f"**{c['unapplied']}** 条尚未应用于交付 —— 去「人工裁决」页点「执行裁决」。")


def carryover_note_md(m: dict) -> str:
    """质检总览表**下方**小字区的沿用计数一行(零沿用 → 空串)。

    ⚠️ 绝不进那张对账表:表的口径是「输入 = 判废 + 精确去重删除 + 交付」,
    加一行沿用就把等式搅了。改变结果的条数单独点出来(用户点名):把仅标记的
    (维持原标注/搁置/维持拒绝)混进去,读者会以为那么多结论都被人动过。
    """
    c = decision_status(m)["counts"]
    if not c["carryover"]:
        return ""
    return (f"_本次结果沿用了此前(早于本次跑批)的人工裁决 **{c['carryover']}** 条,"
            f"其中 **{c['carryover_changed']}** 条改变了结果(其余为仅标记);"
            "逐条见 report.md 的「沿用自此前的人工裁决」小节。_")


#: 裁决卡片溯源行的状态措辞(applied 之外的两种也要说清,别让人对着一条
#: 落空的裁决反复点「执行」)。
_TRACE_WORDING = {"unapplied": "尚未应用 —— 到本页底部点「执行裁决」",
                  "orphaned": "该 episode 不在本次跑批里,无处可施"}


def decision_trace_md(m: dict, line: str, eid: str) -> str:
    """裁决卡片上的溯源一行,如「你在 2026-08-14 21:30 裁过:采纳建议改标
    (本次沿用)」。line ∈ label/verdict/appeal;没裁过 → 空串。"""
    for r in decision_status(m)["records"]:
        if r["line"] != line or r["id"] != eid or not r["kind"]:
            continue
        if r["status"] == "applied":
            state = {"carryover": "本次沿用", "fresh": "本轮已应用"}.get(
                r["when"], "已应用")
        else:
            state = _TRACE_WORDING[r["status"]]
        when_txt = f"在 {r['at']} " if r["at"] else ""
        return f"你{when_txt}裁过:**{r['kind']}**({state})"
    return ""


def application_counts_md(run_path: str) -> str:
    """「人工裁决」页执行入口的计数行(没有任何裁决 → 空串)。

    落空的(该 episode 不在选中的这次跑批里)单独说,**不计入未应用** ——
    混进去那个数字永远消不掉,提醒就成了狼来了。
    """
    c = _dec.application_counts(_dec.run_decision_records(run_path))
    if not c["total"]:
        return ""
    s = (f"人工裁决:共 {c['total']} 条 / 已应用 {c['applied']} 条 / "
         f"未应用 {c['unapplied']} 条")
    if c["orphaned"]:
        s += f";另有 {c['orphaned']} 条无处可施(该 episode 不在这次跑批里)"
    return s


def unapplied_card_note(run_path: str) -> str:
    """跑批完成的任务卡片上的那句提醒(没有未应用的 → 空串)。

    放在任务卡片上是因为那是离「忘记执行」最近的时刻:跑完一批,老裁决对新结果
    全都处于未应用态,此刻不提,下一次想起来就是交付之后。
    """
    c = _dec.application_counts(_dec.run_decision_records(run_path))
    if not c["unapplied"]:
        return ""
    return (f"⚠️ 这份交付记有 {c['unapplied']} 条人工裁决,尚未应用到这次结果 —— "
            "去「质检报告 · 人工裁决」页点「执行裁决」。")


def audit_clip_paths(m: dict, episode_id: str) -> list[str]:
    """该分歧条目的视频片段(details/audit_clips/<ep>__<cam>.mp4,按相机名排序)。"""
    d = os.path.join(m["path"], "details", "audit_clips")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith(episode_id + "__") and f.endswith(".mp4"))


# ───────── Episodes 页重做 + 同步曲线页(2026-08-07)─────────
#
# 起因(用户原话):被拒展示"目前的显示很差"。根因是小尺寸证据帧和**超宽的同步
# 曲线长图**被塞进同一个 4 列画廊,曲线被压成四分之一格 → 必然糊成一条。
# 重做的三条硬规矩:
#   ① 证据分家:小图走多列画廊,长条曲线图走**整幅宽度**的独立组件,永不混排;
#   ② 判决先行:被拒条目最该一眼看到的是"哪一维把它毙了、为什么",不是一堆缩略图;
#   ③ 一切降级:老交付没有逐相机同步数据,缺字段时说一句人话,绝不崩、绝不编。
#
# 同步语义(用户拍板,展示文案与之严格一致,别自造说法):
#   · 同步检查**永不废弃相机** —— 发现异常只**标注**;判废只在 episode 层面,
#     且仅当"所有可信相机一致指向同一个偏移"(verdict=misaligned_all);
#   · 弃权/测不准(undecidable)**不进人工裁决队列**、**不参与综合质量分**,是个标注。

#: 交付里同步检查的中文名。report.py 的 CHECK_CN 写「视频-动作同步」,进度条等处
#: 出现过无连字符的写法 —— 读端两个都认(UI 不 import 管道,只能在此留常量副本;
#: 与 TASK_CHECK_CN 同一套办法)。
SYNC_CHECK_NAMES = ("视频-动作同步", "视频动作同步")

#: 老交付(2026-08-07 之前)的 detail 只有平铺的 lag_s/corr_peak,没有 per_camera。
#: 缺字段时统一这一句 —— 让人知道是交付旧,而不是系统坏了。
LEGACY_SYNC_NOTE = "此交付无逐相机同步数据(旧版本)"

#: 同步判决 → (徽章文字, 主色, 底色, 一句人话)。四态的措辞就是上面那段语义的
#: 界面化,改这里等于改对客户的承诺,改前先看那段注释。
SYNC_VERDICT_TEXT = {
    "aligned": ("同步正常", "#009A29", "#E8FFEA",
                "各可信相机与动作时序对齐,未发现异常。"),
    "misaligned_all": ("整体错位(判废)", "#CB272D", "#FFECE8",
                       "所有可信相机一致指向同一个偏移 —— 这是判废的唯一条件,"
                       "发生在 episode 层面(不是废掉某个相机)。"),
    "annotated": ("已标注异常(不判废)", "#D25F00", "#FFF7E8",
                  "个别相机读数异常,已标注;同步检查永不废弃相机,也不因此判废这条数据。"),
    # verdict 仍是 annotated,但成因是"疑似错位、证据不足"时换个说法——
    # 用户看曲线时最想区分的正是这两者(2026-08-07)
    "_annotated_suspect": ("疑似错位(证据不足)", "#D25F00", "#FFF7E8",
                           "有相机的互相关峰明显偏离 0,但峰形不够可信,不足以定论:"
                           "只标注,不判废、不进人工裁决队列。"),
    "undecidable": ("测不准(弃权)", "#165DFF", "#E8F3FF",
                    "信号不足以判定同步,只作标注:不进人工裁决队列,也不参与综合质量分。"),
}

#: 老交付没有 verdict 字段,只有检查三态 —— 退回讲整体结论,并注明是旧版本。
_SYNC_STATE_TEXT = {
    "pass": ("同步通过", "#009A29", "#E8FFEA", "旧版本交付只有整体结论。"),
    "拒绝": ("同步不通过", "#CB272D", "#FFECE8", "旧版本交付只有整体结论。"),
    "弃权": ("弃权", "#165DFF", "#E8F3FF",
             "旧版本交付只有整体结论;弃权不参与综合质量分。"),
}
_SYNC_UNKNOWN = ("同步结论未知", "#86909C", "#F2F3F5", "此条没有同步检查读数。")

def _fmt_num(v, nd: int = 3) -> str:
    """读数格式化。缺测 → 「—」;**不写 0**:0 是个有意义的滞后值,与"没测出来"
    是两回事,混在一起会让人以为对齐得很好。"""
    if v is None or isinstance(v, bool):
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _sync_check_of(ep: dict) -> dict:
    """episode → 同步检查条目(名字两种写法都认;没有返回 {})。"""
    checks = (ep or {}).get("checks") or {}
    for name in SYNC_CHECK_NAMES:
        if name in checks:
            return checks[name] or {}
    return {}


def sync_check(m: dict, eid: str) -> dict:
    return _sync_check_of((m.get("episodes") or {}).get(eid or "") or {})


def sync_detail(m: dict, eid: str) -> dict:
    """同步检查的 detail(容忍双重编码;没有该检查 → {})。"""
    chk = sync_check(m, eid)
    return _deep_detail(chk.get("detail")) if chk else {}


def sync_badge(detail: dict, state: str = "") -> tuple[str, str, str, str]:
    """(徽章文字, 主色, 底色, 一句人话)。新契约的 verdict 优先,老交付退回三态。"""
    d = detail or {}
    v = d.get("verdict")
    if v == "annotated" and d.get("suspect_cameras") and not d.get("flagged_cameras"):
        return SYNC_VERDICT_TEXT["_annotated_suspect"]
    if v in SYNC_VERDICT_TEXT:
        return SYNC_VERDICT_TEXT[v]
    if state in _SYNC_STATE_TEXT:
        return _SYNC_STATE_TEXT[state]
    return _SYNC_UNKNOWN


SYNC_CAM_HEADERS = ["相机", "标注", "滞后(秒)", "相关峰值", "零偏相关",
                    "峰值比", "峰宽(秒)", "可信", "说明"]


def sync_camera_rows(m: dict, eid: str) -> list[list]:
    """逐相机同步读数 → 表格行。老交付无 per_camera → 空列表(上层给降级说明)。"""
    d = sync_detail(m, eid)
    per = d.get("per_camera")
    if not isinstance(per, dict) or not per:
        return []
    flagged = set(d.get("flagged_cameras") or [])
    rows = []
    for cam in sorted(per):
        c = per[cam] if isinstance(per[cam], dict) else {}
        note = c.get("note") or c.get("code") or ""
        rows.append([cam, "⚠ 已标注" if cam in flagged else "",
                     _fmt_num(c.get("lag_s")), _fmt_num(c.get("corr_peak")),
                     _fmt_num(c.get("corr_at_zero")), _fmt_num(c.get("peak_ratio"), 2),
                     _fmt_num(c.get("peak_width_s"), 2),
                     "可信" if c.get("trusted") else "不可信", str(note)])
    return rows


def _cell(txt: str, *, bold: bool = False, color: str = "#333",
          align: str = "left") -> str:
    return (f'<td style="padding:5px 9px;border-bottom:1px solid #E5E6EB;color:{color};'
            f'text-align:{align}{";font-weight:700" if bold else ""}">{_esc(txt)}</td>')


def _table_html(headers: list, rows: list[list], marks: list[bool],
                mark_color: str = "#FFF7E8") -> str:
    """通用小表:marks[i] 为真的行整行着色 + 左侧色条(被标注/被拒的那行要跳出来)。"""
    head = "".join(f'<th style="padding:5px 9px;text-align:left;font-weight:700;'
                   f'color:#4E5969;border-bottom:2px solid #C9CDD4;white-space:nowrap">'
                   f'{_esc(h)}</th>' for h in headers)
    body = []
    for i, r in enumerate(rows):
        hit = i < len(marks) and marks[i]
        style = (f'background:{mark_color};box-shadow:inset 3px 0 0 0 #FF7D00'
                 if hit else "")
        body.append(f'<tr style="{style}">'
                    + "".join(_cell(c, bold=(hit and j == 0)) for j, c in enumerate(r))
                    + "</tr>")
    return ('<table style="border-collapse:collapse;width:100%;max-width:960px;'
            'font:12px/1.6 system-ui;margin:4px 0 10px">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>')


def sync_camera_html(m: dict, eid: str) -> str:
    """Episodes 页的逐相机同步读数块(徽章 + 摘要 + 表;老交付一句降级说明)。"""
    chk = sync_check(m, eid)
    if not chk:
        return ('<p style="color:#777;font:12px/1.6 system-ui">'
                '这条没有同步读数:该检查未启用,或这条在更早的检查就被拦下,没跑到这一步。</p>')
    d = sync_detail(m, eid)
    txt, fg, bg, why = sync_badge(d, chk.get("state", ""))
    bits = []
    if d.get("consensus_lag_s") is not None:
        bits.append(f"一致偏移 {_fmt_num(d.get('consensus_lag_s'))} 秒")
    if d.get("n_cameras") is not None:
        bits.append(f"相机 {d.get('n_cameras')} 路"
                    + (f"(可信 {d.get('n_trusted')} 路)"
                       if d.get("n_trusted") is not None else ""))
    if d.get("flagged_cameras"):
        bits.append("已标注相机:" + "、".join(str(c) for c in d["flagged_cameras"]))
    if d.get("reason"):
        bits.append(str(d["reason"]))
    summary = (f'<div style="font:12px/1.6 system-ui;color:#555;margin:2px 0 6px">'
               f'{_esc(" · ".join(bits))}</div>' if bits else "")
    rows = sync_camera_rows(m, eid)
    if rows:
        flagged = set(d.get("flagged_cameras") or [])
        table = _table_html(SYNC_CAM_HEADERS, rows, [r[0] in flagged for r in rows])
    else:
        # 老交付:只有平铺读数(lag_s/corr_peak),照实摊开,并说清为什么没有逐相机
        flat = " · ".join(f"{k}={_fmt_num(d[k])}" for k in ("lag_s", "corr_peak")
                          if d.get(k) is not None)
        table = (f'<div style="font:12px/1.6 system-ui;color:#777;background:#F7F8FA;'
                 f'border-left:3px solid #C9CDD4;padding:6px 10px;max-width:960px">'
                 f'{LEGACY_SYNC_NOTE}'
                 + (f'。本条整体读数:{_esc(flat)}' if flat else "") + '</div>')
    return ('<div style="margin-top:6px">'
            '<div style="font:13px/1.6 system-ui;font-weight:700;color:#4E5969;'
            'margin-bottom:4px">视频-动作同步(逐相机读数)</div>'
            f'<span style="background:{bg};color:{fg};border-radius:999px;'
            f'padding:2px 12px;font:12px/1.8 system-ui;font-weight:700">{_esc(txt)}</span>'
            f'<span style="color:#86909C;font:12px/1.8 system-ui;margin-left:8px">'
            f'{_esc(why)}</span>'
            + summary + table + '</div>')


def episode_verdict_label(ep: dict) -> str:
    """卡片头上的那三个字。待裁决优先于通过/拒绝 —— 系统还没定论时,先告诉人
    "该你上了",而不是显示一个随时会变的当前判决。"""
    if (ep or {}).get("pending"):
        return "待裁决"
    return (ep or {}).get("verdict") or "?"


def fatal_checks(m: dict, eid: str) -> list[str]:
    """把这条数据毙掉的检查(state == 拒绝)。可能不止一维,按 checks 顺序返回。"""
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    return [name for name, c in (ep.get("checks") or {}).items()
            if (c or {}).get("state") == "拒绝"]


def episode_reason_text(m: dict, eid: str) -> str:
    """一句人话原因:优先用致命检查自己写的 reason,其次交付里的拒绝原因。"""
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    for name in fatal_checks(m, eid):
        d = (ep.get("checks") or {}).get(name, {}).get("detail") or {}
        d = _deep_detail(d)
        why = d.get("reason") or d.get("verdict")
        if why:
            return str(why)
    return str(ep.get("reject_reason") or "")


def episode_card_html(m: dict, eid: str) -> str:
    """详情面板第一行:大徽章 + 判决 + 一句人话理由。

    **通过条目极简**(2026-08-11 用户原话:"就一行 ✅ 通过,别的不说"):过了的
    数据没有故事,客户点开只是确认一眼;把致命项、弃权说明、读数一股脑堆上来,
    只会把真正要看的拒绝/待人工条目淹掉。逐维读数一律退到下方折叠的检查明细里。
    """
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    if not eid or not ep:
        return ('<div style="border:1px dashed #C9CDD4;border-radius:10px;padding:14px 18px;'
                'color:#86909C;font:13px/1.6 system-ui">'
                '在左侧清单里选一条 episode,这里会显示它的判决、理由与视频。</div>')
    txt, fg, bg, bd = _BUCKET_STYLES[episode_bucket(m, eid)]
    head = (f'<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px">'
            f'<span style="font-size:1.35rem;font-weight:800;color:{fg}">{_esc(txt)}</span>'
            f'<span style="font:14px/1.6 ui-monospace,monospace;color:#4E5969;'
            f'font-weight:700">{_esc(eid)}</span>')
    shell = (f'<div style="background:{bg};border:1px solid {bd};border-left:6px solid {fg};'
             f'border-radius:10px;padding:14px 18px;margin:4px 0 10px">')
    if episode_bucket(m, eid) == BUCKET_PASSED:
        return shell + head + "</div></div>"
    score = ep.get("soft_score")
    if score is not None:
        head += (f'<span style="margin-left:auto;background:#fff;border:1px solid #E5E6EB;'
                 f'border-radius:999px;padding:2px 12px;font:12px/1.8 system-ui;'
                 f'color:#4E5969">综合质量分 '
                 f'<b style="color:#1D2129">{_esc(_fmt_num(score))}</b></span>')
    head += "</div>"
    reason = episode_reason_line(m, eid)
    reason_html = (f'<div style="margin-top:9px;font:13px/1.8 system-ui;color:#4E5969">'
                   f'{_esc(reason)}</div>' if reason else "")
    # 同步弃权是个标注:它既不进裁决队列也不进质量分,卡片上讲一句,免得客户
    # 看到"弃权"二字以为这条数据被扣了分。
    footnote = ""
    if sync_detail(m, eid).get("verdict") == "undecidable":
        footnote = ('<div style="margin-top:8px;font:12px/1.6 system-ui;color:#165DFF;'
                    'background:#E8F3FF;border-radius:6px;padding:5px 10px">'
                    '同步测不准仅作标注:不进人工裁决队列,也不参与综合质量分。</div>')
    return shell + head + reason_html + footnote + "</div>"


def check_table_html(m: dict, eid: str) -> str:
    """逐维检查读数表(数据仍来自 check_rows),投「拒绝」的那一维整行标红。

    为什么不用 Dataframe:Dataframe 没法把某一行做视觉强调,而"被拒的是哪一维"
    正是这页最该一眼看到的信息。
    """
    rows = check_rows(m, eid) if eid else []
    if not rows:
        return ('<p style="color:#777;font:12px/1.6 system-ui">'
                '这条没有记录逐维读数:更早的检查判废后就不再往下跑;旧版本生成的交付也没有这份记录。</p>')
    marks = [r[1] == "拒绝" for r in rows]
    return ('<div style="font:13px/1.6 system-ui;font-weight:700;color:#4E5969;'
            'margin-top:4px">各维检查读数(标红=把这条毙掉的那一维)</div>'
            + _table_html(CHECK_HEADERS, [[str(c) for c in r] for r in rows],
                          marks, mark_color="#FFECE8"))


# ── 同步曲线页(2026-08-07 新增):details/plots/<ep>_sync.png 的画廊 ──

SYNC_FILTER_ALL = "全部"
SYNC_FILTER_FLAGGED = "只看有标注/异常的"
SYNC_FILTERS = [SYNC_FILTER_ALL, SYNC_FILTER_FLAGGED]

#: 一页多少张。曲线是宽长图,一屏塞太多既看不清也拖慢首屏;分页比懒加载简单且
#: 零 JS(本模块的一贯做法)。
SYNC_PAGE_SIZE = 20      # 每页张数:曲线是整幅宽度的长图,平铺往下滚最顺手
                         # (2026-08-07 用户定)。翻页只是"图多到塞不下"时的兜底,
                         # 一页放得下时 UI 会把翻页件整排隐藏

#: 交付里没有 plots 时的说明。2026-08-13 用户点名删掉后面那串开关说明:
#: `pipeline.sync_plots` 是我们的实现细节,客户既不知道去哪改、也不该被要求知道
#: (要改的人现在在任务台「更多设置 › 视频-动作同步的证据图」里点就行)。
NO_PLOTS_NOTE = "此交付没有同步曲线图(`details/plots/` 为空)。"


def _sync_flagged(ep: dict, detail: dict, state: str) -> bool:
    """这条曲线值不值得人看:新契约看 verdict/flagged_cameras,老交付退回检查三态
    与 episode 判决(老交付本来也只在非「过」时才画图)。"""
    v = detail.get("verdict")
    if v in SYNC_VERDICT_TEXT:
        # 判 aligned 但有相机被诊断出毛病的(假峰/疑似错位/无信号),同样要进筛选:
        # ep4 就是这种——整条结论没问题,可那一路的峰肉眼可见地偏了,用户第一个
        # 想复查的就是它(2026-08-07)。
        return (v != "aligned"
                or bool(detail.get("flagged_cameras"))
                or bool(detail.get("suspect_cameras"))
                or bool(detail.get("noisy_cameras")))
    if detail.get("flagged_cameras"):
        return True
    if state in ("拒绝", "弃权"):
        return True
    return ep.get("verdict") == "拒绝" or bool(ep.get("pending"))


#: 诊断标签 → 圆点颜色。对齐=绿、错位=红、测不准=琥珀(需注意但不是罪证)、
#: 无信号=灰。与页面其它徽章同一套语义,不另造配色。
_DIAG_COLOR = {"aligned": "#00B42A", "misaligned": "#F53F3F",
               "false_peak": "#FF7D00", "blurry_motion": "#FF7D00",
               "rival_lags": "#FF7D00", "weak_signal": "#FF7D00",
               "partial_visibility": "#FF7D00",
               "no_motion": "#86909C"}


def _diag_rows(detail: dict) -> list[dict]:
    """同步 detail → 逐相机诊断行(纯数据)。老交付没有 diagnosis → 退回 note。"""
    per = (detail or {}).get("per_camera")
    if not isinstance(per, dict):
        return []
    rows = []
    for cam in sorted(per):
        r = per[cam] if isinstance(per[cam], dict) else {}
        dg = r.get("diagnosis") if isinstance(r.get("diagnosis"), dict) else {}
        cause = str(dg.get("cause") or ("aligned" if r.get("trusted") else ""))
        lag = r.get("lag_s")
        rows.append({
            "cam": cam,
            "lag": "—" if lag is None else f"{float(lag):+.2f}s",
            "label": str(dg.get("label") or ("对齐" if r.get("trusted") else "测不准")),
            "text": str(dg.get("text") or r.get("note") or ""),
            "advice": str(dg.get("advice") or ""),
            "color": _DIAG_COLOR.get(cause, "#86909C"),
        })
    return rows


def sync_diag_html(items: list) -> str:
    """逐相机诊断框(每张曲线右侧)。**说病因,不说笼统结论**——
    用户 2026-08-07:"你不是 instruction 都说'又矮又胖'的情况是啥了嘛,
    难道这种情况不是'又矮又胖'背后的问题吗?你得给出正确的诊断啊"。"""
    if not items:
        return ""
    out = ['<div class="sync-diag-title">逐相机诊断</div>']
    for r in items:
        out.append(
            f'<div class="sync-diag-row">'
            f'<div class="sync-diag-head">'
            f'<span class="sync-dot" style="background:{r["color"]}"></span>'
            f'<b>{_esc(r["cam"])}</b>'
            f'<span class="sync-diag-lag">{_esc(r["lag"])}</span></div>'
            f'<div class="sync-diag-label" style="color:{r["color"]}">'
            f'{_esc(r["label"])}</div>'
            f'<div class="sync-diag-text">{_md_bold(r["text"])}</div>'
            + (f'<div class="sync-diag-advice">→ {_esc(r["advice"])}</div>'
               if r["advice"] else "")
            + "</div>")
    return "".join(out)


def sync_plot_items(m: dict, mode: str = SYNC_FILTER_ALL) -> list[dict]:
    """有曲线图的 episode → [{id, path, badge, color, flagged}](纯数据,不产 HTML)。"""
    out = []
    for eid, ep in sorted((m.get("episodes") or {}).items()):
        if not ep.get("plot"):
            continue
        chk = _sync_check_of(ep)
        d = _deep_detail(chk.get("detail")) if chk else {}
        txt, fg, _bg, _why = sync_badge(d, chk.get("state", ""))
        flagged = _sync_flagged(ep, d, chk.get("state", ""))
        out.append({"id": eid, "path": ep["plot"], "badge": txt,
                    "color": fg, "flagged": flagged,
                    "cameras": _diag_rows(d), "reason": str(d.get("reason") or "")})
    if mode == SYNC_FILTER_FLAGGED:
        return [it for it in out if it["flagged"]]
    return out


def sync_plots_mode(m: dict) -> str:
    """本次质检画曲线的范围:all / flagged / off(读不到 → "")。"""
    pl = ((m.get("config_effective") or {}).get("pipeline") or {})
    return str(pl.get("sync_plots") or "")


def sync_checked_count(m: dict) -> int:
    """做过同步检查的 episode 条数(不管有没有画曲线)。"""
    return sum(1 for ep in (m.get("episodes") or {}).values() if _sync_check_of(ep))


def sync_coverage_note(m: dict, n_plots: int) -> str:
    """「全部」到底是全部什么?—— 覆盖范围说明。

    2026-08-07 用户问:"假如跑质检时没设 all-plots,只有有问题的 episode 有图,
    我点了「全部」会发生什么?" 答案是只会看到那几张,而页面从前只说"共 N 张曲线",
    读起来像全库只有 N 条 —— 会让人误以为其余 episode 没被检查。这里说破。
    """
    mode = sync_plots_mode(m)
    n_ck = sync_checked_count(m)
    if mode == "flagged" or (mode != "all" and n_ck > n_plots > 0):
        miss = max(0, n_ck - n_plots)
        return (f"\n\n⚠️ 本次质检**只为需要留意的条目画了曲线**"
                f"(配置 `pipeline.sync_plots = flagged`):{n_ck} 条 episode 都做了"
                f"同步检查,但只有 {n_plots} 条有图,其余 {miss} 条同步正常、未出图。"
                f"这里的「全部」= 全部**已出图**的曲线,不是全部 episode。"
                f"要逐条都看,请加 `--set pipeline.sync_plots=all` 重跑。")
    return ""


def sync_view(m: dict, mode: str = SYNC_FILTER_ALL, page: int = 0,
              page_size: int = SYNC_PAGE_SIZE) -> dict:
    """同步曲线页的一屏:{page, pages, items, note, pos}。

    items = [(图片路径, "epXXXXXX · 徽章")] —— 直接喂 gr.Gallery(自带点击放大),
    每张的标题就是 episode 号 + 该条的同步判定徽章。页码越界回绕(与裁决卡片一致)。
    """
    items = sync_plot_items(m, mode)  # [{id,path,badge,color,flagged}]
    n_all = len(sync_plot_items(m, SYNC_FILTER_ALL))
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = (page or 0) % pages
    shown = items[page * page_size:(page + 1) * page_size]
    if not n_all:
        note = NO_PLOTS_NOTE
    elif not items:
        note = (f"本交付的 {n_all} 张曲线里没有被标注/异常的条目 —— "
                "切到「全部」可以逐条看。")
    else:
        note = f"共 **{len(items)}** 张曲线"
        if mode == SYNC_FILTER_FLAGGED and n_all > len(items):
            note += f"(另有 {n_all - len(items)} 张同步正常的未列出)"
        note += ";点任意一张放大,标题里的徽章是该条的同步判定。"
        note += sync_coverage_note(m, n_all)
    pos = f"第 {page + 1} / {pages} 页" if pages > 1 else ""
    return {"page": page, "pages": pages,
            "items": [(it["path"], f'{it["id"]} · {it["badge"]}') for it in shown],
            "cards": sync_cards_html(shown),      # 自绘卡片(一行一张,点图开原图)
            "note": note, "pos": pos}


#: 表头一律说人话(2026-08-07 用户:"四分位距是个啥?能改不")。"四分位距"是统计
#: 黑话,它在这里的意思就是"这一路的滞后在各条之间跳得厉害不厉害"——直接写那个意思。
SYNC_HEALTH_HEADERS = ["相机", "有效读数", "典型滞后(秒)", "逐条波动(秒)",
                       "疑似错位", "测不准", "已标注"]


def sync_health_rows(m: dict) -> list[list]:
    """数据集级 lag 分布(dataset.sync_health.per_camera)→ 表格行;老交付空表。"""
    h = (m.get("dataset") or {}).get("sync_health") or {}
    per = h.get("per_camera")
    if not isinstance(per, dict):
        return []
    rows = []
    for cam in sorted(per):
        s = per[cam] if isinstance(per[cam], dict) else {}
        rows.append([cam, s.get("n", "—"), _fmt_num(s.get("median_lag_s")),
                     _fmt_num(s.get("iqr_s")),
                     s.get("n_suspect", "—"), s.get("n_abstained", "—"),
                     s.get("n_flagged", "—")])
    return rows


def sync_health_marks(m: dict) -> list[bool]:
    """哪几行该高亮:典型滞后超容差,或有疑似错位的条目。"""
    h = (m.get("dataset") or {}).get("sync_health") or {}
    per = h.get("per_camera")
    if not isinstance(per, dict):
        return []
    marks = []
    for cam in sorted(per):
        s = per[cam] if isinstance(per[cam], dict) else {}
        med = s.get("median_lag_s")
        marks.append(bool(s.get("n_suspect"))
                     or (med is not None and abs(float(med)) > 0.25))
    return marks


#: 「同步曲线」页顶部的结论横幅 + 读图指南(2026-08-07 用户点名:
#: "用户就看到一堆曲线,能得到什么提示呢")。原来的建议埋在整页图之后,
#: 滚不到 = 等于没有。现在结论先行:先说这份数据集同步得怎么样、**该怎么办**,
#: 曲线退居为证据。
SYNC_HOWTO = (
    "**怎么看这些图**:左边每格一路相机——蓝线=画面在动的程度,红线=机械臂在动的"
    "程度,两条同步起伏才对;右边一格是所有相机的互相关曲线,**峰落在绿带(0±0.25s)"
    "内就是对齐**。峰又矮又胖 = 这一路测不准(背景有人走动/相机晃动都会这样),"
    "系统不会拿它下结论。")


def sync_conclusion(m: dict) -> dict:
    """数据集级同步结论 → {level, title, points}(纯数据,便于单测)。

    level: ok / notice / attention —— 只影响配色,不代表判废(同步永不废相机,
    判废只在"所有可信相机一致指向同一偏移"这一种情形,且已在漏斗里发生过了)。
    """
    eps = m.get("episodes") or {}
    h = (m.get("dataset") or {}).get("sync_health") or {}
    n_plot = n_flag = n_undec = n_killed = n_suspect = n_noisy = 0
    for eid, ep in eps.items():
        chk = _sync_check_of(ep)
        if not chk:
            continue
        d = _deep_detail(chk.get("detail")) or {}
        v = d.get("verdict")
        if v == "undecidable":
            n_undec += 1
        if v == "misaligned_all" or chk.get("state") == "拒绝":
            n_killed += 1
        if d.get("suspect_cameras"):
            n_suspect += 1
        if d.get("noisy_cameras"):
            n_noisy += 1
        # 「被标注异常」只算**真有问题**的路(可信且错位、或完全无信号)。
        # 假峰/测不准另有说法,混进来会让横幅自相矛盾:标题说"同步正常"、
        # 条目却说"有相机被标注异常"(2026-08-07 实见)。
        if d.get("flagged_cameras") or (not d and _sync_flagged(ep, d,
                                                               chk.get("state", ""))):
            n_flag += 1
        if ep.get("plot"):
            n_plot += 1
    n_all = sum(1 for ep in eps.values() if _sync_check_of(ep))
    points: list[str] = []
    level, title = "ok", "同步正常:未发现视频与动作错位"
    if n_noisy:
        title = (f"同步正常:未发现错位;{n_noisy} 条有相机因画面干扰测不准"
                 f"(证据仍偏向对齐)")
    if n_flag:
        level, title = "notice", f"{n_flag} 条有相机被标注异常(不判废)"
    if n_suspect:
        level, title = "notice", (f"{n_suspect} 条有相机疑似错位(证据不足,"
                                  f"不判废)")

    if n_killed:
        level, title = "attention", f"{n_killed} 条因整条错位被判废"
        points.append("判废条件极严:**所有可信相机一致指向同一个偏移**才杀——"
                      "这通常意味着录制管线的时间轴出了问题,而不是某一路相机的毛病。")
    if h.get("negative_lag_episodes"):
        level = "attention"
        points.append("出现**负滞后**(画面早于动作)。这在物理上没有良性解释,"
                      "多半来自数据装配环节(格式转换错行、episode 边界切错、开头掉帧)"
                      "——建议回查转换流程。")
    if n_flag:
        level = "notice" if level == "ok" else level
        points.append(f"**{n_flag} 条**有相机被标注异常。**视频一路没删、数据照常交付**;"
                      "如果要拿这些条目做逐帧对齐敏感的训练,建议对被标注的那一路降权或不用。")
    if n_noisy:
        points.append(
            f"**{n_noisy} 条**的某一路测出了偏移,但把画面与动作错开和**完全不错开**"
            "几乎同样像 —— 这是画面干扰造成的**假峰**,证据其实偏向对齐,不是错位。"
            "逐条曲线右侧的诊断框写明了是哪一路、本条实测到的成因、该怎么改善。")
    if n_suspect:
        level = "notice" if level == "ok" else level
        points.append(
            f"**{n_suspect} 条**有相机**疑似错位但证据不足**(测出的偏移明显不在"
            "零点,但证据还不够硬)。系统**不判废也不进人工队列**,但这类条目值得"
            "抽查:如果同一路反复出现,多半是真延时。")
    if n_undec:
        points.append(f"**{n_undec} 条测不准**(背景干扰大、动作幅度小或静止段长)。"
                      "测不准 **不是** 质量问题、不进人工队列、不参与打分——"
                      "只是这条数据上本方法没有判别力。")
    if h.get("advice"):
        points.append(str(h["advice"]))
    if not points:
        points.append("逐相机测量结果一致且都落在容差内,这份数据可直接用于"
                      "对时序精度敏感的训练(如模仿学习的逐帧配对)。")
    points.append(f"本页共 {n_plot} 张曲线(检查了 {n_all} 条 episode)——曲线是**证据**,"
                  "结论已写在上面,正常情况下不必逐张看。")
    return {"level": level, "title": title, "points": points}


_SYNC_LEVEL_STYLE = {
    "ok": ("#E8FFEA", "#00B42A", "#009A29", "✅"),
    "notice": ("#FFF7E8", "#FF7D00", "#D25F00", "🔎"),
    "attention": ("#FFECE8", "#F53F3F", "#CB272D", "⚠️"),
}


def sync_conclusion_html(m: dict) -> str:
    """结论横幅(页面最顶):一句结论 + 该怎么办的要点。"""
    c = sync_conclusion(m)
    bg, line, fg, icon = _SYNC_LEVEL_STYLE.get(c["level"], _SYNC_LEVEL_STYLE["ok"])
    lis = "".join(f'<li style="margin:3px 0">{_md_bold(p)}</li>' for p in c["points"])
    return (f'<div style="background:{bg};border:1px solid {line};border-left:6px solid {line};'
            f'border-radius:10px;padding:13px 18px;margin:4px 0 10px">'
            f'<div style="font-weight:800;font-size:1.08rem;color:{fg};margin-bottom:6px">'
            f'{icon} {_esc(c["title"])}</div>'
            f'<ul style="margin:0;padding-left:20px;font:13px/1.75 system-ui;color:{fg}">'
            f"{lis}</ul></div>")


def _md_bold(s: str) -> str:
    """把 **粗体** 转成 <b>(结论文案里手写的强调),其余一律转义。"""
    import re as _re
    parts = _re.split(r"\*\*(.+?)\*\*", str(s))
    out = []
    for i, seg in enumerate(parts):
        out.append(f"<b>{_esc(seg)}</b>" if i % 2 else _esc(seg))
    return "".join(out)


def _file_url(path: str) -> str:
    """本地文件 → gradio 的静态文件 URL(交付目录已在 allowed_paths 里)。

    带 ?v=<mtime> 版本号:重画曲线是原地重写同名 PNG,FSX 重写有短暂读不到的
    空窗,页面赶上空窗会把破图缓存住(2026-08-07 用户实见 ep1/ep2 空白)。
    mtime 进 URL 后,文件一变 URL 就变,缓存天然失效。
    """
    from urllib.parse import quote
    p = str(path)
    try:
        ver = f"?v={int(os.stat(p).st_mtime)}"
    except OSError:
        ver = ""
    return "/gradio_api/file=" + quote(p) + ver

# 传输抖动自愈:失败后退避重试,最多 8 次(≈9s)。只改 ?v= 的值,不引入 & 字符
# ——属性里的 & 会被 HTML 解析器当实体开头,踩过一次不再踩。
_IMG_RETRY = (
    ' onerror="var n=+(this.dataset.n||0);if(n<8){this.dataset.n=n+1;var i=this;'
    "setTimeout(function(){i.src=i.src.replace(/[?]v=.*$/,'?v='+Date.now())},"
    '260*n+180)}"')


def sync_cards_html(items: list) -> str:
    """曲线卡片(一行一张,2026-08-07 用户定:两列太挤、图被压瘦)。

    自绘而非 gr.Gallery/gr.Image,原因两条:①要精细控制边框/间距/留白;
    ②放大要用**页内灯箱**——最初做成 <a target=_blank> 开新标签页,用户反馈
    "点开放大后就回不去了",改成 checkbox 灯箱:点图 → 全屏遮罩看大图,
    点遮罩任意处关掉,不离开页面。纯 CSS(label+checkbox),不依赖 JS——
    gr.HTML 走 innerHTML 注入,<script> 不执行,CSS 机关永远好使。

    图片加载防线(2026-08-07 实锤,三张图长期空白):同页多张 200KB+ 的图并发拉,
    `kubectl port-forward` 会掐掉部分流(转发日志 broken pipe / connection reset),
    浏览器把失败结果记死,刷新也不好。两道防线:
      · 灯箱大图 loading=lazy —— 藏着不发请求,并发请求量直接减半;
      · onerror 自动重试(换 v 值绕缓存,退避到 8 次)—— 传输抖动自愈,
        不再需要人去硬刷新。内联事件属性经 innerHTML 注入是执行的(<script> 才不执行)。
    """
    if not items:
        return ""
    out = []
    for k, it in enumerate(items):
        url = _file_url(it["path"])
        lb = f"sync-lb-{k}"                       # 灯箱开关 id,页内唯一即可
        flag = it.get("flagged")
        accent = "#FF7D00" if flag else "#E5E6EB"
        out.append(
            f'<div class="sync-card">'
            f'<div class="sync-card-head" style="border-left:4px solid {accent}">'
            f'<span class="sync-eid">{_esc(it["id"])}</span>'
            f'<span class="sync-badge" style="color:{it.get("color") or "#4E5969"}">'
            f'{_esc(it.get("badge") or "")}</span>'
            f'<a class="sync-open" href="{url}" target="_blank" rel="noopener">'
            f"原图 ↗</a></div>"
            f'<div class="sync-card-body">'
            f'<label class="sync-figure" for="{lb}" title="点击放大">'
            f'<img class="sync-img" src="{url}" alt="{_esc(it["id"])}"{_IMG_RETRY}>'
            f"</label>"
            f'<div class="sync-diag">{sync_diag_html(it.get("cameras") or [])}'
            + (f'<div class="sync-diag-foot">{_md_bold(it["reason"])}</div>'
               if it.get("reason") else "")
            + "</div></div>"
            f'<input type="checkbox" id="{lb}" class="sync-lb-toggle">'
            f'<label for="{lb}" class="sync-lb" title="点击任意处关闭">'
            f'<img src="{url}" loading="lazy" alt="{_esc(it["id"])}"{_IMG_RETRY}>'
            f"</label></div>")
    return '<div class="sync-cards">' + "".join(out) + "</div>"


def sync_health_html(m: dict) -> str:
    """数据集级同步健康度:逐相机 lag 分布 + 系统给的建议。老交付整块降级一句话。"""
    h = (m.get("dataset") or {}).get("sync_health") or {}
    rows = sync_health_rows(m)
    if not rows and not h.get("advice"):
        return ('<p style="color:#777;font:12px/1.6 system-ui">'
                f'{LEGACY_SYNC_NOTE}——数据集级 lag 分布是新版本交付才统计的。</p>')
    parts = ['<div style="font:13px/1.6 system-ui;font-weight:700;color:#4E5969;'
             'margin-top:2px">全库逐相机同步概览</div>',
             '<div style="font:12px/1.7 system-ui;color:#86909C;margin:2px 0 6px">'
             '<b>典型滞后</b>=这一路画面比动作晚多少(正=画面晚,负=画面早,'
             '越接近 0 越好);<b>逐条波动</b>=各条 episode 之间这个数跳得厉不厉害'
             '(小=录制稳定,大=时快时慢);<b>疑似错位</b>=峰明显偏了但证据不够硬,'
             '不判废、只提醒;<b>测不准</b>=这一路信号不适合做此项判定。</div>']
    if rows:
        marks = sync_health_marks(m)
        parts.append(_table_html(SYNC_HEALTH_HEADERS,
                                 [[str(c) for c in r] for r in rows], marks))
    if h.get("advice"):
        parts.append('<div style="background:#FFF7E8;border-left:3px solid #FF7D00;'
                     'padding:7px 11px;max-width:960px;font:12px/1.7 system-ui;'
                     'color:#D25F00">建议:' + _md_bold(str(h["advice"])) + '</div>')
    neg = h.get("negative_lag_episodes") or []
    if neg:
        head = "、".join(str(e) for e in neg[:10])
        parts.append('<div style="font:12px/1.7 system-ui;color:#555;margin-top:6px">'
                     f'负滞后(画面先于动作)的条目 {len(neg)} 条:{_esc(head)}'
                     + ("…" if len(neg) > 10 else "") + '</div>')
    return "".join(parts)


# ───────── Episodes 页整页改版(2026-08-11):三桶 + 左清单右详情 ─────────
#
# 起因(用户与其同事拍板):旧页太乱——一张七列大表 + 三个筛选档 + 证据帧画廊 +
# 曲线 + 三路切片,客户其实只关心三件事:**哪些过了、哪些被拒、哪些还等着人来定**。
# 骨架照 lerobot visualize_dataset 的"左导航右详情":左边一列清单(每行一句
# 为什么),右边一屏详情,**视频是详情的主角**。静态证据帧整块撤掉(用户原话:
# 体验太差);裁决页的证据帧照旧,不动那边。
#
# 桶的口径(下面这几个纯函数是唯一事实源,app.py 只做装配):
#   通过   = 判决通过且已交付,没有任何待人工的事情压着
#   拒绝   = 被判掉的,**含精确去重删除**(交付里判决写「拒绝(去重)」,理由说"重复")
#   待人工 = 系统弃权待裁决 ∪ 标注-画面分歧复核队列
# 三桶互斥,优先级 拒绝 > 待人工 > 通过:被拒的已经出局,不该再催人去裁它;
# 系统还没定论的排在通过前面,免得客户把"待定"当"已过"。

BUCKET_PASSED = "通过"
BUCKET_REJECTED = "拒绝"
BUCKET_PENDING = "待人工"
BUCKET_ALL = "全部"

#: 三桶的展示顺序与图标(界面上就长这样,别再建第二套映射)。
BUCKETS = (BUCKET_PASSED, BUCKET_REJECTED, BUCKET_PENDING)
BUCKET_ICONS = {BUCKET_PASSED: "✅", BUCKET_REJECTED: "❌", BUCKET_PENDING: "⏳"}

#: 三桶 → (徽章图标, 主色, 底色, 边色)。绿=通过、红=拒绝、琥珀=待人工;只留图标不留字
#: (issue #59:颜色+图标已说明一切)。样式按**桶**取,不按当前判决取——
#: 只因标注分歧进待人工桶的条目当前判决是"通过",按判决取会给它挂 ✅(实见的张冠李戴)。
_BUCKET_STYLES = {
    BUCKET_PASSED: ("✅", "#009A29", "#E8FFEA", "#AFF0B5"),
    BUCKET_REJECTED: ("⛔", "#CB272D", "#FFECE8", "#FDCDC5"),
    BUCKET_PENDING: ("⏳", "#D25F00", "#FFF7E8", "#FFE4BA"),
}

#: 清单行里那半句人话的长度上限。清单是用来"扫"的,一行超过这个宽度就开始
#: 换行,几百条一换行整列就散了。
LIST_REASON_CAP = 24


def humanize_reason(text) -> str:
    """交付里的原因串 → 界面用词。

    交付记的是实现记法(`硬门违规: 「任务成败判定」`)。"硬门"是机制黑话
    (2026-08-11 用户点名清除):客户要的是"哪项没过",不是我们内部怎么分类
    检查的。报告侧的措辞另有工单,这里只管 UI 这一端。
    """
    s = str(text or "").strip()
    for prefix in ("硬门违规: ", "硬门违规:", "硬门违规:"):
        if s.startswith(prefix):
            s = "未通过" + s[len(prefix):].strip()
            break
    for bad, good in (("硬门", "不合格拦截"), ("软分", "质量分"),
                      ("双签硬杀", "两类证据相互印证,判废"), ("路双签", "路相互印证"),
                      ("硬杀", "判废"), ("孤证", "单一证据")):
        s = s.replace(bad, good)   # 老交付里的行话不许漏出去(issue #59 追加三个词)
    return s


def audit_queue_ids(m: dict) -> set:
    """标注-画面分歧复核队列里的 episode id(待人工桶的第二个来源)。"""
    return {a.get("id", "") for a in (m.get("audit_queue") or []) if a.get("id")}


def is_dedup_drop(ep: dict) -> bool:
    """这条是被精确去重删掉的(判决写「拒绝(去重)」)——理由要说"重复"而不是
    "质量不合格",它的画面一点毛病没有,只是和另一条一模一样。"""
    return "去重" in str((ep or {}).get("verdict") or "")


def episode_bucket(m: dict, eid: str, _left: set | None = None) -> str:
    """episode → 三桶之一(口径见本节顶部注释)。

    2026-08-25 用户点名:名叫「待人工」就得真还欠着人 —— 分歧/成败弃权条目
    人工已给全结论(草稿即可)就离开 ⏳ 桶,判据与人工裁决队列同源
    (question_pending_ids)。其它维度的弃权(如同步)人工裁决页管不了,
    没人能替它给结论,照旧 ⏳。批量调用把算好的集合从 _left 传进来
    (逐条各读一遍裁决 CSV,两百行清单就是四百次文件读)。"""
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    if str(ep.get("verdict") or "").startswith("拒绝"):
        return BUCKET_REJECTED
    if [q for q in (ep.get("pending") or []) if q != TASK_CHECK_CN]:
        return BUCKET_PENDING
    if not (ep.get("pending") or eid in audit_queue_ids(m)):
        return BUCKET_PASSED
    left = question_pending_ids(m) if _left is None else _left
    return BUCKET_PENDING if eid in left else BUCKET_PASSED


def bucket_ids(m: dict, bucket: str = BUCKET_ALL) -> list[str]:
    """该桶里的 episode id(id 升序,稳定)。未知桶名一律当「全部」——前端能塞
    任意字符串进来,不该因为一个陌生值就给空清单(空清单看起来像交付坏了)。"""
    eids = sorted((m.get("episodes") or {}).keys())
    if bucket in BUCKETS:
        left = question_pending_ids(m)
        return [e for e in eids if episode_bucket(m, e, left) == bucket]
    return eids


def bucket_counts(m: dict) -> dict:
    """{通过: n, 拒绝: n, 待人工: n, 全部: n}。三桶互斥,三者之和 = 全部。"""
    counts = {b: 0 for b in BUCKETS}
    left = question_pending_ids(m)
    for eid in (m.get("episodes") or {}):
        counts[episode_bucket(m, eid, left)] += 1
    counts[BUCKET_ALL] = sum(counts.values())
    return counts


def bucket_choices(m: dict) -> list:
    """顶部三桶(+全部)的单选项:[(界面标签, 桶名)]。标签自带计数——数字就是
    客户最想先看到的东西,不该藏在下一屏。"""
    c = bucket_counts(m)
    # 计数带括号(issue #59 第 2 条:「通过 50」读起来像一个词,有歧义)
    out = [(f"{BUCKET_ICONS[b]} {b} ({c[b]})", b) for b in BUCKETS]
    out.append((f"{BUCKET_ALL} ({c[BUCKET_ALL]})", BUCKET_ALL))
    return out


def _first_sentence(text: str) -> str:
    """取首句(中英句号/分号/换行断句)。理由常是一整段,清单只放第一句。"""
    s = str(text or "").strip()
    for sep in ("。", "\n", ";", ";"):
        i = s.find(sep)
        if i > 0:
            s = s[:i]
    return s.strip()


def episode_reason_line(m: dict, eid: str) -> str:
    """详情面板第一屏那句人话理由(通过条目没有理由——它没什么可解释的)。

    拒绝:优先说"未通过「哪一项」",后面接那项检查自己写的人话;去重条目单说
    "重复"(它的画面没问题)。待人工:说清是哪一项弃权、弃权原因是什么。
    """
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    bucket = episode_bucket(m, eid)
    if bucket == BUCKET_PASSED:
        return ""
    if bucket == BUCKET_REJECTED:
        if is_dedup_drop(ep):
            return humanize_reason(ep.get("reject_reason")) or "与另一条数据完全重复"
        # 交付里的拒绝原因就是唯一事实源:判决层已按
        # 「未通过「检查名」:该检查写的人话」拼好(report.hard_fail_reason),
        # 报告与这里引用同一个串,UI **不再自拼一套**。
        line = humanize_reason(ep.get("reject_reason"))
        why = humanize_reason(episode_reason_text(m, eid))
        # 老交付只写了"硬门违规: 「X」"(没带检查的人话)→ 这里补上,让老交付也读得懂
        if why and why not in line:
            line = f"{line}:{why}" if line else why
        return line
    # 同 episode_list_reason:high-level,不携带读数细节(2026-08-23 用户定)
    bits = [f"「{chk}」证据不足,系统拿不准——需要人看视频给结论"
            for chk in ep.get("pending") or []]
    if eid in audit_queue_ids(m):
        bits.append("原始标注与画面描述不一致,需人工确认")
    return ";".join(bits)


def episode_list_reason(m: dict, eid: str) -> str:
    """左清单那半句:**人话在前**,不带「未通过「检查名」:」的前缀。

    两处措辞故意解耦(2026-08-11 用户定):清单列窄、单行省略,前二十来个字就被
    截住,前缀会把"到底怎么了"整句挤出视野;横幅(episode_reason_line)则要完整
    交代"哪一项没过 + 为什么"。拿不到检查自己写的人话时(老交付)退回横幅那套
    格式 —— 宁可啰嗦,不可空白。
    """
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    bucket = episode_bucket(m, eid)
    if bucket == BUCKET_PASSED:
        return ""
    if bucket == BUCKET_REJECTED:
        if is_dedup_drop(ep):
            return humanize_reason(ep.get("reject_reason")) or "与另一条数据完全重复"
        # episode_reason_text 优先给致命检查自己写的那句;它拿不到才退回交付里的
        # 拒绝原因(老交付 = "未通过「X」",正是这里要的兜底)
        return humanize_reason(episode_reason_text(m, eid)) or episode_reason_line(m, eid)
    # 2026-08-23 用户定:待人工的理由在 UI 只说 high-level,"0.15 在灰区(0.25~0.45)"
    # 这类数字细节用户不 care;全文仍在报告与下方「检查明细」里。
    bits = [f"「{chk}」拿不准" for chk in (ep.get("pending") or [])]
    if eid in audit_queue_ids(m):
        bits.append("标注与画面不一致")
    bits = [b for b in bits if b]
    return ";".join(bits) or episode_reason_line(m, eid)


def episode_short_reason(m: dict, eid: str) -> str:
    """清单行右边那半句人话(首句 + 截断)。通过条目返回空串。"""
    s = _first_sentence(episode_list_reason(m, eid))
    return s[:LIST_REASON_CAP] + "…" if len(s) > LIST_REASON_CAP else s


def episode_list_items(m: dict, bucket: str = BUCKET_ALL) -> list[dict]:
    """左侧清单的数据行:[{id, icon, reason, label}]。label 即界面上那一行。"""
    out = []
    left = question_pending_ids(m)
    for eid in bucket_ids(m, bucket):
        b = episode_bucket(m, eid, left)
        icon = BUCKET_ICONS[b]
        reason = episode_short_reason(m, eid)
        out.append({"id": eid, "bucket": b, "icon": icon, "reason": reason,
                    "label": f"{eid} {icon} {reason}".rstrip()})
    return out


def episode_list_choices(m: dict, bucket: str = BUCKET_ALL) -> list:
    """左侧清单的单选项:[(界面标签, episode id)]。"""
    return [(it["label"], it["id"]) for it in episode_list_items(m, bucket)]


#: 左清单每页多少条(2026-08-11 用户实见:两百行单选框一次渲染就到极限了)。
#: 翻页口径与同步曲线页(SYNC_PAGE_SIZE 一族)保持一致:页码越界回绕、一页放得下
#: 就把翻页件整排隐藏——不发明第二套翻页。
EPISODE_PAGE_SIZE = 50


def episode_list_view(m: dict, bucket: str = BUCKET_ALL, page: int = 0,
                      selected: str | None = None,
                      page_size: int = EPISODE_PAGE_SIZE) -> dict:
    """左清单的一屏:{page, pages, choices, value, pos, multi}。

    selected(当前正在看的那条)**只在它落在本页时**才回填成选中态:翻到别的页
    时右侧详情不动、清单里没有高亮项,翻回来它还亮着——这就是"跨页保持"。
    """
    items = episode_list_items(m, bucket)
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = (page or 0) % pages
    shown = items[page * page_size:(page + 1) * page_size]
    ids = [it["id"] for it in shown]
    return {"page": page, "pages": pages,
            "choices": [(it["label"], it["id"]) for it in shown],
            "value": selected if selected in ids else None,
            "pos": f"第 {page + 1} / {pages} 页" if pages > 1 else "",
            "multi": pages > 1}


# ── 视频来源链(2026-08-11):同一条 episode 的视频可能在两个地方,顺序固定 ──
#
#   ① 审片站(`curation review-page` 的产出,UI 用 --review-dir / 环境变量
#      CURATION_REVIEW_DIR 指到它):**全部 episode 都有**,含被拒的——被拒条目
#      恰恰是最该看画面的,所以审片站排第一;
#   ② 交付内 `lerobot_curated` 的逐条 mp4(v2 布局):**只有通过的条目有**
#      (被拒的压根没进交付集);v3 源导出的是合并大 mp4,不切分 → 不属于本源;
#   ③ 两处都没有:说一句"怎么才能有",不空着也不假装。

VIDEO_SOURCE_REVIEW = "审片站"
VIDEO_SOURCE_CURATED = "交付数据集"
#: ④ **源数据集**(2026-08-19 用户实机点名:「在 episode 页面不管过没过都应该
#: 显示视频」)。前三个来源全是**产物**,而产物天然带偏:交付集只装通过的、
#: 裁决片段只装进了队列的、审片站要另外生成。于是**被拒的条目往往一路都没有**
#: —— 可它恰恰是最该被看见的那一条:系统自己把"被拒复议"定义成"证据够就杀"的
#: 保险丝,而看不见画面的复议就是走过场。
#: 源数据集对通过与否一视同仁,是唯一天然全覆盖的来源,所以垫在最后兜底。
VIDEO_SOURCE_SOURCE = "源数据集"
VIDEO_SOURCE_NONE = "无"

#: 四处都没有视频时的提示。
#: ⚠️ 措辞别再写成"运行 review-page"(那是行话,客户不知道去哪运行):先说清
#: **为什么没有**,再说出路。
NO_VIDEO_NOTE = ("这条找不到画面:交付里没有它的视频,也没找到源数据集。"
                 "源数据集还在的话,重新打开这份交付即可")

_SOURCE_NOTE = {
    VIDEO_SOURCE_REVIEW: "视频来自审片站(全部 episode 都有,含被拒条目)",
    VIDEO_SOURCE_CURATED: "视频来自交付数据集 lerobot_curated(只有通过的条目有)",
    VIDEO_SOURCE_SOURCE: "视频来自源数据集(不分通过与否,原样的那一条)",
}


def _camera_label(name: str) -> str:
    """相机键 → 界面名(去掉 LeRobot 的 observation.images. 前缀,那是 schema 细节)。"""
    s = str(name)
    return s.split("observation.images.")[-1] if "observation.images." in s else s


def _norm_name(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


#: 审片站的身份文件(review_page.write_site_manifest 写的),记着这个站是从哪个
#: 数据集生成的。UI 不 import 管道的红线在此照旧:只认文件名与字段,不 import 生成侧。
SITE_JSON = "site.json"


def delivery_source_dataset(m: dict) -> tuple[str | None, str]:
    """本交付的源数据集 →(绝对路径 or None, 数据集名)。

    交付的 passed.json 目前**只记数据集名**(`数据集` 字段,值就是源目录名),不记
    源路径 —— 所以匹配审片站时通常只能比名。留 `source_dataset` 这个读法是为了
    将来交付真记了路径时,路径比对自动生效(路径比名严,能分开同名不同处的数据集)。
    """
    return (m.get("source_dataset") or None), str(m.get("name") or "")


def review_sites(review_dir: str | None) -> list[str]:
    """审片站根目录 → 候选站点目录(根本身 + 一级子目录)。

    一站一目录是 `review-page --output <根>/<站名>` 的惯例,但把根直接指到某个站
    也是合法用法,两种都认。
    """
    if not review_dir:
        return []
    out = [review_dir]
    try:
        out += [os.path.join(review_dir, d) for d in sorted(os.listdir(review_dir))
                if os.path.isdir(os.path.join(review_dir, d))]
    except OSError:
        pass
    return out


def site_matches_delivery(site_dir: str, m: dict) -> bool:
    """这个站点是不是**本交付那份数据**生成的:只认 site.json 里的源数据集身份。"""
    site = _load_json(os.path.join(site_dir, SITE_JSON))
    if not site:
        return False
    src_path, src_name = delivery_source_dataset(m)
    site_path = site.get("source_dataset")
    if src_path and site_path:              # 两边都有路径 → 比路径(最严)
        return os.path.normpath(str(src_path)) == os.path.normpath(str(site_path))
    site_name = site.get("dataset_name") or os.path.basename(
        os.path.normpath(str(site_path or "")))
    return bool(src_name) and _norm_name(site_name) == _norm_name(src_name)


def review_clip_paths(review_dir: str | None, m: dict, eid: str) -> list[str]:
    """审片站里**本交付这份数据**的该 episode 片段(按相机名排序;没有 → 空表)。

    认领顺序(2026-08-11 收紧,起因见下):
      ① 站点 site.json 声明的源数据集与本交付一致 —— 精确认领;
      ② 站名归一化后等于数据集名 —— 老站点(没 site.json)的降级通道。
    **没有第三档**。此前还有"随便找第一个带这个 episode 号的站"这一档,实测
    droid-ep13-20-demo 就借用了 droid200 站的片段:那次同源同号,巧对;换个数据集
    同号就是给客户放错视频。宁缺勿错 —— 认不出来就当没有,退到交付集内的视频。
    """
    if not review_dir or not eid:
        return []
    sites = review_sites(review_dir)
    _, want = delivery_source_dataset(m)
    ordered = [s for s in sites if site_matches_delivery(s, m)]
    ordered += [s for s in sites
                if s not in ordered and want
                and _norm_name(os.path.basename(os.path.normpath(s))) == _norm_name(want)]
    for root in ordered:
        d = os.path.join(root, "details", "audit_clips")
        if not os.path.isdir(d):
            continue
        hits = sorted(os.path.join(d, f) for f in os.listdir(d)
                      if f.startswith(eid + "__") and f.endswith(".mp4"))
        if hits:
            return hits
    return []


def delivered_ids(m: dict) -> list[str]:
    """进了交付集的 episode(= 判决不是拒绝的,含还在等人裁的那些——它们判决仍是
    通过、数据照常交付)。顺序 = id 升序 = 导出器重编号的顺序。"""
    eps = m.get("episodes") or {}
    return [e for e in sorted(eps) if not str((eps[e] or {}).get("verdict") or ""
                                              ).startswith("拒绝")]


def curated_index_of(m: dict, eid: str) -> int | None:
    """交付集里这条数据的新序号(交付集是重编号的),定不下来就返回 None。

    两条来路,**都定不下来就诚实弃权**——序号猜错 = 播的是别人的视频:
      ① `meta/curation_camera_health.json` 逐条记了 源 episode_id ↔ 新 episode_index
         (逐相机同步旁挂文件,新交付都有),这是精确来路;
      ② 退路:导出器就是把幸存条目按原序密排重编号的(new_idx = enumerate(keep)),
         所以序位可以换算——但只有当交付集条数与本清单里的交付条数**严格相等**时
         才敢用,对不上说明中间还有别的增删,宁可不给视频。
    """
    if not eid:
        return None
    root = os.path.join(m.get("path") or "", "lerobot_curated")
    for r in _load_json(os.path.join(root, "meta",
                                     "curation_camera_health.json")).get("episodes") or []:
        if r.get("source_episode_id") == eid and r.get("episode_index") is not None:
            return int(r["episode_index"])
    kept = delivered_ids(m)
    total = _load_json(os.path.join(root, "meta", "info.json")).get("total_episodes")
    if total is not None and eid in kept and len(kept) == int(total):
        return kept.index(eid)
    return None


def curated_video_paths(m: dict, eid: str) -> list[str]:
    """交付内 lerobot_curated 的逐条 mp4(v2 布局;v3 是合并大 mp4 → 空表)。"""
    root = os.path.join(m.get("path") or "", "lerobot_curated")
    ver = str(_load_json(os.path.join(root, "meta", "info.json")
                         ).get("codebase_version") or "")
    if not ver.startswith("v2"):
        return []                      # v3 合并 mp4 不切分,不属于本来源
    idx = curated_index_of(m, eid)
    if idx is None:
        return []
    return sorted(glob.glob(os.path.join(root, "videos", "chunk-*", "*",
                                         f"episode_{idx:06d}.mp4")))


def source_video_paths(m: dict, eid: str,
                       data_root: str | None = None) -> list[str]:
    """**源数据集**里这条 episode 的逐路 mp4(v2 布局)。

    与另外三个来源的关键差别:它**不分通过与否**——被拒的、弃权的、通过的都在,
    因为它就是客户交进来的原始数据。这也是它垫在最后的理由:前三档命中就用前三档
    (那才是"我们交付的那一份"),前三档没有时,至少让人看得见画面。

    ⚠️ 用 **eid 的原始序号**直接拼路径,不做任何重编号映射:源数据集的
    episode_NNNNNN 就是原始序号,而交付集才是重编过的(curated_index_of 是给那边
    用的)。这两套编号混用会放出**另一条 episode 的画面** —— 人对着错的证据做裁决,
    比没有画面更糟。
    ⚠️ v3 源是合并大 mp4(一个文件装多条),切不出单条 → 返回空表,与
    curated_video_paths 同一条规矩。
    """
    root, name = delivery_source_dataset(m)
    # ⚠️ 交付的 run.json/passed.json **只记数据集名**、不记路径(见
    # delivery_source_dataset 的注释),所以光靠交付自己解析不出源目录 ——
    # 那样这条兜底路对绝大多数交付都是空的,等于没做(2026-08-19 实测:
    # debug 交付的 source_dataset 就是 None)。
    # 用界面已知的「数据集根目录」把名字还原成路径:名字就是源目录名。
    if (not root or not os.path.isdir(root)) and data_root and name:
        cand = os.path.join(str(data_root), name)
        root = cand if os.path.isdir(cand) else root
    if not root or not os.path.isdir(root):
        return []
    ver = str(_load_json(os.path.join(root, "meta", "info.json")
                         ).get("codebase_version") or "")
    if not ver.startswith("v2"):
        return []
    num = "".join(ch for ch in str(eid) if ch.isdigit())
    if not num:
        return []
    return sorted(glob.glob(os.path.join(root, "videos", "chunk-*", "*",
                                         f"episode_{int(num):06d}.mp4")))


# ── 片段可播性(2026-08-11 用户实锤:ep000018 摆了三个"死"播放器)──
#
# 那条是 8 帧 0.47 秒的采集残段(被拒原因就是它),切出来的审片片段只有 **1 帧、
# 0.25 秒**:文件存在、近 10KB、mp4 魔数俱全,播放器就是放不出东西。两类毛病都要
# 认出来,缺一不可:
#   ① 缺失 / 过小 / 没有 ftyp 魔数 —— 截断、零填充那一族(FSX 直写坑的常见形态),
#      只读文件头判定,**不整读**(交付集里的 mp4 可能几十 MB);
#   ② 容器头里帧数 ≤1 或时长 < 0.5 秒 —— 就是"能开但没得放"的死片段。同样只读头
#      (av.open 解容器索引,**不解码任何一帧**)。
# ⚠️ 判出来是为了**说清楚**,不是为了把播放器撤掉(2026-08-11 用户原话:
#    "视频还是要放在那里占位")—— 槽位照摆,旁边写明白它为什么放不动;
#    判不了的一律当能播:证据不足时不许替客户把视频藏起来。

MIN_PLAYABLE_BYTES = 4096
MIN_PLAYABLE_SECONDS = 0.5
MIN_PLAYABLE_FRAMES = 2

#: 拿不到帧数/时长时的兜底说明(文件缺失、截断、根本不是 mp4)。
BROKEN_LANE_TEXT = "片段损坏或过短,无法正常播放"

#: 被拒条目的半句话:片段放不动这件事本身往往就是它被拒的原因(残段/坏帧)。
REJECTED_CLIP_TAIL = "这正是该条被拒的原因"

_PROBE_CACHE: dict = {}


def short_clip_text(frames: int | None, duration_s: float | None) -> str:
    """"视频过短(N 帧 / X.XX 秒),无法正常播放" —— 读数拿得到几个就写几个。"""
    bits = []
    if frames:
        bits.append(f"{frames} 帧")
    if duration_s:
        bits.append(f"{duration_s:.2f} 秒")
    return (f"视频过短({' / '.join(bits)}),无法正常播放" if bits
            else BROKEN_LANE_TEXT)


def clip_probe(path: str) -> dict:
    """片段 → {playable, frames, duration_s, why}(判据见上)。按 路径+mtime+大小 记忆。"""
    try:
        st = os.stat(path)
    except OSError:
        return {"playable": False, "frames": None, "duration_s": None,
                "why": BROKEN_LANE_TEXT}
    key = (path, int(st.st_mtime), st.st_size)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    out = {"playable": True, "frames": None, "duration_s": None, "why": ""}
    head = b""
    if st.st_size >= MIN_PLAYABLE_BYTES:
        try:
            with open(path, "rb") as f:
                head = f.read(12)         # 只读文件头:FSX 上整读是灾难
        except OSError:
            head = b""
    if st.st_size < MIN_PLAYABLE_BYTES or b"ftyp" not in head:
        out = {"playable": False, "frames": None, "duration_s": None,
               "why": BROKEN_LANE_TEXT}
    else:
        frames, dur = _probe_frames_duration(path)
        out["frames"], out["duration_s"] = frames, dur
        too_short = ((frames is not None and frames < MIN_PLAYABLE_FRAMES)
                     or (dur is not None and dur < MIN_PLAYABLE_SECONDS))
        if too_short:
            out["playable"] = False
            out["why"] = short_clip_text(frames, dur)
    _PROBE_CACHE[key] = out
    return out


def _probe_frames_duration(path: str):
    """容器头 → (帧数, 时长秒);没有解码库或读不出 → (None, None)= 不下结论。"""
    try:
        import av
    except Exception:  # noqa: BLE001
        return None, None                 # 没有解码库就不做这层判断,不瞎猜
    try:
        with av.open(path) as c:
            st = c.streams.video[0]
            frames = int(st.frames or 0) or None
            dur = (float(c.duration) / 1e6) if c.duration else None
        return frames, dur
    except Exception:  # noqa: BLE001
        # 连容器都开不了 = 确实放不动。读数给 0(而不是 None)才能让上面判成
        # "不可播";文案那边 0 是假值,自动退回"损坏或过短"的兜底说法。
        return 0, 0.0


def clip_is_playable(path: str) -> bool:
    """这个片段能不能正常播(clip_probe 的布尔快捷方式)。"""
    return bool(clip_probe(path)["playable"])


def _lane(path: str, eid: str, source: str) -> dict:
    base = os.path.basename(path)
    cam = (base[len(eid) + 2:-4] if source == VIDEO_SOURCE_REVIEW
           else os.path.basename(os.path.dirname(path)))
    p = clip_probe(path)
    return {"camera": _camera_label(cam), "path": path,
            "playable": p["playable"], "frames": p["frames"],
            "duration_s": p["duration_s"], "why": p["why"]}


def episode_videos(m: dict, eid: str, review_dir: str | None = None,
                   data_root: str | None = None) -> dict:
    """该 episode 的全部相机视频 → {source, note, videos:[{camera, path, playable, why…}]}。

    来源链见上;命中哪一档就整档用,不混着拼(混着拼会出现同一路相机两个版本)。
    一档里**一路能播的都没有**时,先看下一档有没有能播的(有就用下一档);都没有
    就还用第一档 —— 播不动也照样摆槽位,旁边写清为什么(见上面的 ⚠️)。
    """
    cands = [(s, p) for s, p in
             ((VIDEO_SOURCE_REVIEW, review_clip_paths(review_dir, m, eid)),
              (VIDEO_SOURCE_CURATED, curated_video_paths(m, eid)),
              # ④ 源数据集垫底:前三档全是产物,天然漏掉被拒条目(2026-08-19)
              (VIDEO_SOURCE_SOURCE, source_video_paths(m, eid, data_root))) if p]
    if not cands:
        return {"source": VIDEO_SOURCE_NONE, "note": NO_VIDEO_NOTE, "videos": []}
    lanes_by_source = [(s, [_lane(p, eid, s) for p in paths]) for s, paths in cands]
    source, lanes = next((sl for sl in lanes_by_source
                          if any(x["playable"] for x in sl[1])), lanes_by_source[0])
    return {"source": source, "note": _SOURCE_NOTE[source], "videos": lanes}


#: 「同时播放」按钮的两个状态文字(切换即暂停)。
PLAY_ALL_TEXT = "▶ 同时播放"
PAUSE_ALL_TEXT = "⏸ 暂停"

#: 按钮的行为:把本区内所有 <video> 归零后一起播,再点一次全部暂停。
#: 走内联事件属性——gr.HTML 是 innerHTML 注入,<script> 标签不执行、内联事件执行
#: (同步曲线页的 onerror 重试是同一条经验)。**不写 & 字符**:属性值里的 & 会被
#: HTML 解析器当实体开头,踩过一次不再踩(所以下面用嵌套 if 而不是 `&&`)。
#:
#: 找视频有两种挂法,一条 JS 通吃:
#: ①`data-zone="<elem_id>"` —— 按钮和视频不在同一个 DOM 子树里(「待你裁决」的
#:   合并裁决卡用的是 gr.Video 组件,按钮只能另起一行);②没有 data-zone 就往上找
#:   `.ep-video-zone`(Episodes 详情页,按钮与视频同属一块 gr.HTML)。
#:
#: 播完自动把按钮弹回「同时播放」(2026-08-14 去掉 loop 之后必须做):不然视频早
#: 停了、按钮还写着「暂停」,再点一下才发现是从头播 —— 白点一次。
_PLAY_ALL_JS = (
    "var b=this,t=b.dataset.zone,"
    "z=t?document.getElementById(t):b.closest('.ep-video-zone'),"
    "vs=z?z.querySelectorAll('video'):[];"
    "function fin(){var live=0;vs.forEach(function(v){"
    "if(v.paused===false){if(v.ended===false){live=1}}});"
    f"if(live===0){{b.dataset.on='0';b.textContent='{PLAY_ALL_TEXT}'}}}}"
    "if(b.dataset.on==='1'){vs.forEach(function(v){v.pause()});fin()}"
    "else{vs.forEach(function(v){v.onended=fin;v.onpause=fin;v.loop=false;"
    "try{v.currentTime=0}catch(e){}try{v.play()}catch(e){}});"
    f"b.dataset.on='1';b.textContent='{PAUSE_ALL_TEXT}'}}")


def play_all_button_html(note: str = "", zone: str | None = None) -> str:
    """「同时播放」按钮(+ 右侧一行小字)。两处视频区共用同一个实现。

    `zone` 给 elem_id 时按 id 找视频,不给就在自己所在的 `.ep-video-zone` 里找。
    """
    z = f'data-zone="{_esc(zone)}" ' if zone else ""
    tail = (f'<span style="font:12px/1.6 system-ui;color:#86909C">{note}</span>'
            if note else "")
    return ('<div style="display:flex;align-items:center;gap:12px;margin-bottom:7px">'
            f'<button type="button" data-on="0" {z}onclick="{_PLAY_ALL_JS}" '
            'style="background:#1D2129;color:#fff;border:none;border-radius:8px;'
            'padding:6px 16px;font:13px/1.6 system-ui;font-weight:700;cursor:pointer">'
            f'{PLAY_ALL_TEXT}</button>{tail}</div>')


def episode_video_html(m: dict, eid: str, review_dir: str | None = None,
                       data_root: str | None = None) -> str:
    """详情面板的视频区:一个「同时播放」按钮 + 该条全部相机并排。

    不自动播(客户可能同时开着几路 200KB 的片段,自动播 = 一进页面就抢带宽);
    静音 + preload=metadata:首屏只拉元数据,点了才真下视频。

    **不循环**(2026-08-14 用户定):裁决是"看一遍下判断"的事,片子自己转圈只会
    让人分不清看到的是第几遍;要重看点按钮从头播。
    """
    v = episode_videos(m, eid, review_dir, data_root)
    if not v["videos"]:
        return ('<div class="ep-video-zone" style="background:#F7F8FA;border:1px dashed '
                '#C9CDD4;border-radius:10px;padding:14px 18px;color:#86909C;'
                f'font:13px/1.7 system-ui">🎬 此条暂无视频——{_esc(v["note"])}。</div>')
    # 放不动的那一路**照样摆播放器**(用户原话:"视频还是要放在那里占位"),
    # 只在槽位下面补一行说明它为什么放不动 —— 一个没有解释的黑框才是最劝退的。
    cells = "".join(
        f'<figure style="flex:1 1 260px;min-width:220px;margin:0">'
        f'<video src="{_file_url(it["path"])}" muted playsinline controls '
        f'preload="metadata" style="width:100%;border-radius:8px;background:#000">'
        f'</video>'
        f'<figcaption style="font:11px/1.6 ui-monospace,Menlo,monospace;color:#86909C;'
        f'margin-top:3px">{_esc(it["camera"])}</figcaption>'
        + ('' if it.get("playable", True) else
           f'<div style="font:11px/1.6 system-ui;color:#D25F00;background:#FFF7E8;'
           f'border:1px solid #FFE4BA;border-radius:6px;padding:3px 7px;margin-top:3px">'
           f'⚠️ {_esc(it.get("why") or BROKEN_LANE_TEXT)}</div>')
        + '</figure>'
        for it in v["videos"])
    bad = [x for x in v["videos"] if not x.get("playable", True)]
    if bad and len(bad) == len(v["videos"]):
        tail = (f"({REJECTED_CLIP_TAIL})"
                if episode_bucket(m, eid) == BUCKET_REJECTED else "")
        status = f" · 全部 {len(bad)} 路{bad[0].get('why') or BROKEN_LANE_TEXT}{tail}"
    elif bad:
        status = f" · 其中 {len(bad)} 路无法正常播放"
    else:
        status = ""
    note = (f'{_esc(v["note"])} · 共 {len(v["videos"])} 路相机{_esc(status)}')
    return ('<div class="ep-video-zone" style="margin:2px 0 10px">'
            + play_all_button_html(note)
            + f'<div style="display:flex;flex-wrap:wrap;gap:10px">{cells}</div></div>')


#: 待人工条目在检查明细上方的那行醒目提示。Gradio 做不了跨页签跳转(页签切换在
#: 前端,后端拿不到句柄),所以给文字指引——写清去哪一页、在那儿能干什么。
MANUAL_HINT_TEXT = ("这条还等着人来定:去「人工裁决」页,看视频后给结论;"
                    "裁完在同一页点「执行裁决」即生效。")


def manual_hint_html(m: dict, eid: str) -> str:
    """待人工条目的指路条;其它桶不占位(空串)。

    2026-08-25 起多一档:结论给全了但还没「执行裁决」的条目离开 ⏳ 桶
    (用户点名:名叫待人工就得真欠着),这里换蓝条说明"裁了、待应用"——
    不然刚裁完的条目在轨迹页什么都不显示,像裁决没记上。判据与顶部横幅
    同源(decision_status)。"""
    if not eid:
        return ""
    if episode_bucket(m, eid) == BUCKET_PENDING:
        return ('<div style="background:#FFF7E8;border:1px solid #FF7D00;'
                'border-left:6px solid #FF7D00;border-radius:10px;'
                'padding:11px 16px;'
                'margin:2px 0 8px;font:13px/1.7 system-ui;color:#D25F00">'
                f'<b>⏳ 待人工裁决</b> — {_esc(MANUAL_HINT_TEXT)}</div>')
    if any(r["id"] == eid and r["status"] == "unapplied"
           for r in decision_status(m)["records"]):
        return ('<div style="background:#E8F3FF;border:1px solid #165DFF;'
                'border-left:6px solid #165DFF;border-radius:10px;'
                'padding:11px 16px;'
                'margin:2px 0 8px;font:13px/1.7 system-ui;color:#165DFF">'
                '<b>已裁决(未应用)</b> — 结论已记录:'
                '在「人工裁决」页点「执行裁决」即生效。</div>')
    return ""
