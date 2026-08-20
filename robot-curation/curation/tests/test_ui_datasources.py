"""多 TOS 桶框架 + 深链 tos:// 解析(2026-08-17)。

防的事故(测试同事提出、用户拍板要先堵):rerun 深链把 tos://桶/… URL 的**桶名
扔掉**只传最后一段,而界面只扫一个 --data-root —— 两个桶各有一个同名数据集时,
会在默认桶里找到同名的那个、一声不响跑错数据(不报错不提示,最坏的一类 bug)。

分层同 test_ui_runner / test_ui_manifest:解析、对表、白名单全是 runner 里的
纯函数,在此钉死;Gradio 层只验"组件在不在/藏没藏/接没接线"。

2026-08-17 当天三轮返工(都是用户当面提的,别改回去):
① 下拉显示从「默认」改成「桶名:数据集目录」("这个显示没有任何信息量");
② 叫法从「数据源」整个换成「TOS 桶」,配置键 data_sources → tos_buckets
  (零迁移成本窗口内改名;旧键出现必须明确报错,不静默);
③ 「TOS 桶」→「数据集根目录」+ 显示串改 tos://桶/桶内前缀:用户指出
  `curation:/mnt/tos/datasets` 把桶名和**容器内挂载路径**用冒号混成一串
  (curation 才是桶,datasets 是桶内前缀)。配置键 tos_buckets **不再改**:
  键名按声明对象命名、界面标签按用户所选对象命名。同轮补上深链的第二级对表
  (桶内前缀)与根目录三态探测(没挂上 ≠ 挂了但空)。
"""
from __future__ import annotations

import json
import os

import pytest
import yaml

from curation.ui import runner


def _site_yaml(tmp_path, sources) -> str:
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump({"tos_buckets": sources}, allow_unicode=True),
                 encoding="utf-8")
    return str(p)


def _two_sources(tmp_path):
    """两个 TOS 桶,各有一个数据集;**默认桶里故意放一个与备用桶同名的**
    (demo_v2)—— 回落 bug 只有在同名诱饵存在时才咬得出来。
    tos_prefix 显式声明(与 tos_buckets() 产出的键同名,所以这份夹具既能直接
    喂 prefill_plan,也能原样写进站点 yaml 走完整解析)。"""
    a, b = tmp_path / "roota", tmp_path / "rootb"
    for root, names in ((a, ["demo_v2", "only_a"]), (b, ["demo_v2", "only_b"])):
        for n in names:
            (root / n / "meta").mkdir(parents=True)
            (root / n / "meta" / "info.json").write_text("{}")
    return [{"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
             "datasets_path": str(a)},
            {"name": "备用桶", "bucket": "bucketa", "tos_prefix": "prefix",
             "datasets_path": str(b)}]


# ───────── 配置 → TOS 桶清单 ─────────

def test_missing_tos_buckets_synthesizes_single_bucket(tmp_path):
    """配置没写 tos_buckets → 用旧 --data-root 合成单桶,桶名如实标未知。

    防:老部署(没改配置)升级后行为变化 —— 目录逐字节同旧值;合成桶的 bucket
    必须是 None(不许假装挂载的就是某个桶,那会让 tos:// 深链误匹配),显示
    文本也要如实写「(未声明桶)」;内部标识仍叫「默认」(key 不是给人看的)。
    """
    got = runner.tos_buckets(None, "/mnt/tos/datasets")
    assert got == [{"name": "默认", "bucket": None,
                    "datasets_path": "/mnt/tos/datasets",
                    "tos_prefix": None, "endpoint": None,
                    "label": "(未声明桶):/mnt/tos/datasets"}]
    # 站点文件存在但没写这段 → 同样合成(不因文件在场就变行为)
    cfg = str(tmp_path / "site.yaml")
    with open(cfg, "w") as f:
        f.write("vlm_backends: {}\n")
    assert runner.tos_buckets(cfg, "/mnt/tos/datasets") == got


def test_configured_buckets_keep_order_and_skip_broken_entries(tmp_path):
    """配置声明的多 TOS 桶按序解析;缺 datasets_path 的坏条目跳过不拖垮整段;
    重名自动去重(名字是下拉的选中标识,撞名会让两个源指到同一个)。"""
    cfg = _site_yaml(tmp_path, [
        {"name": "默认", "bucket": "curation", "mount_root": "/mnt/tos",
         "datasets_path": "/mnt/tos/datasets"},
        {"name": "备用", "bucket": "bucketa", "mount_root": "/mnt/tos2",
         "datasets_path": "/mnt/tos2/datasets"},
        {"name": "坏的"},                                    # 缺 datasets_path → 跳过
        {"name": "备用", "bucket": "b2", "datasets_path": "/p3"},   # 重名 → 加后缀
    ])
    got = runner.tos_buckets(cfg, "/ignored")
    assert [s["name"] for s in got] == ["默认", "备用", "备用 (2)"]
    assert got[0]["bucket"] == "curation"
    assert got[1]["datasets_path"] == "/mnt/tos2/datasets"
    # 显示文本 = tos://桶/桶内前缀(本地挂载路径不进显示串,2026-08-17 用户
    # 指出冒号拼法把两套地址空间混成一串);占位别名「默认」不印;真别名前缀;
    # 前缀推导不出的条目如实标"未知",不编
    assert [s["label"] for s in got] == [
        "tos://curation/datasets",
        "备用 · tos://bucketa/datasets",
        "备用 · tos://b2(桶内前缀未知)",
    ]


def test_bucket_path_rejects_forged_identifiers():
    """★安全边界:界面传的是桶的**内部标识**,伪造成路径必须被拒。

    防:TOS 桶下拉的值被改成 `../../etc` 之类后,直接当根目录去拼路径 ——
    那等于把「面板不接受自由路径输入」的边界从后门凿穿。
    """
    sources = [{"name": "默认", "bucket": None, "datasets_path": "/mnt/tos/datasets"}]
    assert runner.bucket_path(sources, "默认") == "/mnt/tos/datasets"
    for forged in ("../../etc", "/etc/passwd", "默认/..", "", None, "不存在的源"):
        with pytest.raises(ValueError):
            runner.bucket_path(sources, forged)


# ───────── 深链:query 键与解析 ─────────

def test_deeplink_values_reads_all_three_keys_and_merges():
    """dataset / dataset_url / url 三个键都认,多键并存按键序合并去重。

    防:rerun 侧换个键名(dataset → dataset_url),这边一声不响什么都不做 ——
    "深链没生效"和"深链生效但选错"一样难查。
    """
    class QP(dict):
        def getlist(self, k):
            v = self.get(k)
            return v if isinstance(v, list) else ([v] if v is not None else [])

    vals, present = runner.deeplink_values(QP({"dataset": "a,b"}))
    assert (vals, present) == (["a", "b"], True)
    vals, present = runner.deeplink_values(
        QP({"dataset_url": "tos://x/datasets/c"}))
    assert (vals, present) == (["tos://x/datasets/c"], True)
    vals, present = runner.deeplink_values(QP({"url": "tos://x/d"}))
    assert (vals, present) == (["tos://x/d"], True)
    # 多键并存:按 dataset → dataset_url → url 合并,重复只留一份
    vals, present = runner.deeplink_values(
        QP({"dataset": "a", "url": "a,b", "dataset_url": "c"}))
    assert (vals, present) == (["a", "c", "b"], True)
    # 键出现但值为空:present 必须是 True(界面要据此给"没预选上"的提示)
    vals, present = runner.deeplink_values(QP({"dataset": ""}))
    assert (vals, present) == ([], True)
    # 没有相关键:present=False,界面保持今天的静默(没人点深链就别弹提示)
    vals, present = runner.deeplink_values(QP({"foo": "bar"}))
    assert (vals, present) == ([], False)
    # 普通 dict(无 getlist)也走得通
    assert runner.deeplink_values({"dataset": "x"}) == (["x"], True)


def test_parse_dataset_ref_is_deterministic_on_odd_inputs():
    """畸形 URL 各形态都有确定行为:解析不了就报 error,绝不猜。

    防:`tos://bucket`(没有数据集段)被解析成"数据集叫 bucket"之类的将错就错;
    以及 %2F 解码后藏进来的路径分隔符穿过 safe_name。
    """
    assert runner.parse_dataset_ref("demo_v2") == {"bucket": None, "prefix": None,
                                                   "dataset": "demo_v2"}
    ok = runner.parse_dataset_ref("tos://BucketA/datasets/demo_v2/")
    assert ok == {"bucket": "bucketa", "prefix": "datasets",
                  "dataset": "demo_v2"}   # 尾斜杠容忍;桶名小写比
    # 前缀/数据集名大小写**敏感**(对象存储的 key 就是;不许"差不多就算对上")
    got = runner.parse_dataset_ref("tos://b/DS/Demo")
    assert (got["prefix"], got["dataset"]) == ("DS", "Demo")
    # 多级前缀原样保留;数据集直接在桶根 → 前缀是**空串**(≠ None:空串是
    # "确知在桶根",None 是"裸名字没给 URL",深链对表要分得清这两种)
    assert runner.parse_dataset_ref("tos://b/raw/2026/x")["prefix"] == "raw/2026"
    assert runner.parse_dataset_ref("tos://b/x")["prefix"] == ""
    for bad in ("tos://bucketa",            # 只有桶名
                "tos://",                   # 什么都没有
                "tos:///datasets/x",        # 桶名是空的
                "https://host/datasets/x",  # 不是 tos 协议
                "tos://b/datasets/demo%20v2",   # %20 → 空格,过不了 safe_name
                "tos://b/datasets/a%2Fb"):      # %2F → 藏进来的 /
        got = runner.parse_dataset_ref(bad)
        assert got.get("error"), bad
        assert "dataset" not in got, bad


# ───────── 深链:三种结局 + 兼容 ─────────

def test_deeplink_bucket_match_preselects_source_and_dataset():
    """结局①:桶匹配上已配置的 TOS 桶、数据集也在 → 预选,提示"已按链接选中"。"""
    sources = [{"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
                "datasets_path": "/a"},
               {"name": "备用桶", "bucket": "bucketa", "tos_prefix": "datasets",
                "datasets_path": "/b"}]
    lister = lambda p: {"/a": ["demo_v2"], "/b": ["demo_v2", "x"]}[p]
    plan = runner.prefill_plan(["tos://bucketA/datasets/demo_v2"], sources, lister)
    assert plan["source"] == "备用桶" and plan["datasets"] == ["demo_v2"]
    assert plan["choices"] == ["demo_v2", "x"]
    assert "已按链接选中" in plan["info"] and "备用桶" in plan["info"]
    assert plan["notices"] == []


def test_deeplink_unknown_bucket_never_falls_back_to_default():
    """★结局②(本轮要堵的洞):桶不认识 → 明说"未接入",**绝不回落到默认桶里
    找同名的**。

    防的正是那次现场提出的事故路径:rerun 把桶名丢掉/桶没接入时,默认桶里恰好
    有个同名 demo_v2,旧写法会静默选中它 → 跑错数据。本用例默认桶里就放着同名
    诱饵:回落写法一恢复,datasets 就不再是空,这条立刻变红。
    """
    sources = [{"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
                "datasets_path": "/a"},
               {"name": "备用桶", "bucket": "bucketa", "tos_prefix": "datasets",
                "datasets_path": "/b"}]
    lister = lambda p: {"/a": ["demo_v2"], "/b": ["demo_v2"]}[p]   # 同名诱饵在场
    plan = runner.prefill_plan(["tos://strange/datasets/demo_v2"], sources, lister)
    assert plan["datasets"] == [] and plan["source"] is None
    assert any("strange" in n and "未接入" in n for n in plan["notices"])
    # 合成单源(桶未知)同样不许假装匹配
    synth = runner.tos_buckets(None, "/a")
    plan2 = runner.prefill_plan(["tos://curation/datasets/demo_v2"], synth, lister)
    assert plan2["datasets"] == []
    assert any("未接入" in n for n in plan2["notices"])


def test_deeplink_known_bucket_missing_dataset_says_so():
    """结局③:桶认识、前缀也对,但数据集不在 → 明说"桶 X 的数据集目录里没有
    Y",不预选(措辞维持 2026-08-17 上一轮已验证的原文)。"""
    sources = [{"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
                "datasets_path": "/a"}]
    plan = runner.prefill_plan(["tos://curation/datasets/nope"], sources,
                               lambda p: ["demo_v2"])
    assert plan["datasets"] == []
    assert any("curation" in n and "nope" in n and "没有" in n
               for n in plan["notices"])


def test_deeplink_prefix_mismatch_is_rejected_not_matched():
    """★本轮的核心洞:桶认识、**前缀对不上** → 不选中,明说"只接入了
    datasets/,链接指向的 raw/ 没有接入"。

    防的事故与"桶不认识就回落"同族、只低一层:旧写法只取 URL 最后一段当数据集
    名,中间前缀整个丢掉 —— `tos://curation/raw/droid_lerobot` 会静默匹配到我们
    挂的 `datasets/droid_lerobot`(客户桶里 raw/ 与 curated/ 同名数据集很常见)。
    本用例在挂载目录里就放着同名诱饵:只比桶的老写法一恢复,datasets 就不再是
    空,这条立刻变红(已实测:对着改动前的 runner.py 跑,预选成功、断言炸)。
    """
    sources = [{"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
                "datasets_path": "/a"}]
    lister = lambda p: ["droid_lerobot"]                 # 同名诱饵在挂载目录里
    plan = runner.prefill_plan(["tos://curation/raw/droid_lerobot"], sources,
                               lister)
    assert plan["datasets"] == [] and plan["source"] is None
    assert any("只接入了「datasets/」" in n and "「raw/」" in n
               and "droid_lerobot" in n for n in plan["notices"])
    # 数据集直接在桶根(前缀空串)≠ 接入的 datasets/:同样拒,文案说得出"桶根"
    plan2 = runner.prefill_plan(["tos://curation/droid_lerobot"], sources, lister)
    assert plan2["datasets"] == []
    assert any("桶根" in n for n in plan2["notices"])
    # 尾斜杠容忍:rerun 发来的就是 tos://…/数据集/ 形态,前缀照样对得上
    plan3 = runner.prefill_plan(["tos://curation/datasets/droid_lerobot/"],
                                sources, lister)
    assert plan3["datasets"] == ["droid_lerobot"]


def test_deeplink_unknown_local_prefix_never_admits_match():
    """★前缀未知(配置缺 mount_root/tos_prefix)→ 不放行,明说是**本站配置
    不全**并点名缺哪个键 —— 不许因为"不知道"就当对上了(诚实弃权,与
    fps 不猜 30 是同一条纪律)。"""
    sources = [{"name": "默认", "bucket": "curation", "tos_prefix": None,
                "datasets_path": "/a"}]
    plan = runner.prefill_plan(["tos://curation/datasets/demo_v2"], sources,
                               lambda p: ["demo_v2"])
    assert plan["datasets"] == [] and plan["source"] is None
    assert any("配置" in n and "mount_root" in n and "tos_prefix" in n
               for n in plan["notices"])
    # 旧形态的字典(压根没有 tos_prefix 键)按同样的"未知"处理,不炸
    plan2 = runner.prefill_plan(
        ["tos://curation/datasets/demo_v2"],
        [{"name": "默认", "bucket": "curation", "datasets_path": "/a"}],
        lambda p: ["demo_v2"])
    assert plan2["datasets"] == [] and plan2["notices"]


def test_deeplink_bare_name_keeps_todays_behavior():
    """裸名字(今天的写法)照旧:在默认桶里找,提示措辞与旧版逐字一致。

    防:这轮改造把 rerun 现网还在发的老链接改坏 —— 兼容性不许破。
    """
    sources = runner.tos_buckets(None, "/a")            # 老部署的合成单源
    lister = lambda p: ["demo_v2", "other"]
    plan = runner.prefill_plan(["demo_v2"], sources, lister)
    assert plan["source"] == "默认" and plan["datasets"] == ["demo_v2"]
    assert plan["info"] == "已按链接选中数据集:demo_v2。确认参数后点「开始质检」。"
    assert plan["notices"] == []
    miss = runner.prefill_plan(["demo_v2", "gone"], sources, lister)
    assert miss["datasets"] == ["demo_v2"]               # 找得到的照选
    assert miss["notices"] == ["链接里的数据集在本站找不到:gone(数据集根 /a)"]


def test_deeplink_never_silent_when_nothing_selected():
    """★不许静默无动作:深链键出现了而最终没预选上,notices 必不为空。

    防:解析不出来/值是空的 → 界面一声不吭,「深链没生效」比「生效但选错」
    还难查(用户只看到一个什么都没选的页面)。
    """
    sources = runner.tos_buckets(None, "/a")
    for wanted in ([], ["tos://"], ["tos://bucketonly"],
                   ["https://not-tos/x"], ["tos://b/datasets/demo%20v2"]):
        plan = runner.prefill_plan(wanted, sources, lambda p: ["demo_v2"])
        assert plan["datasets"] == [] and plan["notices"], wanted


def test_deeplink_refs_spanning_two_sources_abstain_with_notice(tmp_path):
    """引用分属两个 TOS 桶 → 不预选并明说:一个下拉一次只能停在一个桶上,
    替用户挑其中一个就是猜(诚实弃权,不硬选)。"""
    sources = _two_sources(tmp_path)
    plan = runner.prefill_plan(
        ["tos://curation/datasets/only_a", "tos://bucketA/prefix/only_b"],
        sources)
    assert plan["datasets"] == []
    assert any("分属不同 TOS 桶" in n for n in plan["notices"])


def test_deeplink_real_listing_end_to_end(tmp_path):
    """用真目录(meta/info.json 判据)走一遍:tos:// 深链选中备用桶的数据集,
    choices 也换成备用桶的清单 —— lister 默认值接的就是 list_datasets。"""
    sources = _two_sources(tmp_path)
    plan = runner.prefill_plan(["tos://bucketa/prefix/demo_v2"], sources)
    assert plan["source"] == "备用桶" and plan["datasets"] == ["demo_v2"]
    assert plan["choices"] == ["demo_v2", "only_b"]


# ───────── 起跑批用选中 TOS 桶的路径 ─────────

def test_selected_source_root_reaches_input_argv(tmp_path):
    """判据:argv 里的 --input 必须落在**选中 TOS 桶**的 datasets_path 下。

    防:界面加了 TOS 桶下拉、起跑批却仍拿默认根拼路径 —— 选了备用桶,跑的还是
    默认桶里的同名数据集(与深链回落是同一类静默跑错)。
    """
    sources = _two_sources(tmp_path)
    root = runner.bucket_path(sources, "备用桶")
    argv = runner.build_argv("run", input=runner.resolve_under(root, "demo_v2"),
                             output=str(tmp_path / "out"))
    inp = argv[argv.index("--input") + 1]
    assert inp == os.path.realpath(str(tmp_path / "rootb" / "demo_v2"))
    assert not inp.startswith(os.path.realpath(str(tmp_path / "roota")))
    # 多数据集作业表同理:每个 job 的 --input 都在选中源之下
    jobs = runner.build_dataset_jobs(root, str(tmp_path / "deliv"),
                                     ["demo_v2", "only_b"], "d1")
    for job in jobs:
        i = job["steps"][0].index("--input")
        assert job["steps"][0][i + 1].startswith(
            os.path.realpath(str(tmp_path / "rootb")) + os.sep)


# ───────── Gradio 层:下拉的显隐与接线 ─────────

@pytest.fixture
def delivery(tmp_path):
    d = tmp_path / "deliv"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps(
        {"数据集": "x", "episodes": {}}, ensure_ascii=False))
    return str(d)


def _src_dropdowns(app):
    """Blocks 配置里全部「数据集根目录」下拉的 props(跑质检 + 裁决两处)。"""
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    return [c["props"] for c in cfg["components"]
            if c["props"].get("label") == "数据集根目录"]


def test_single_source_still_shows_source_dropdown(delivery, tmp_path):
    """★单桶部署:跑质检侧是「数据集目录」文本框(2026-08-20 融合改版,
    取代原「数据集根目录」下拉),始终可见、默认预填本桶地址 —— "随时看得见
    数据来自哪个桶"(2026-08-17 用户拍板)这条以"框里永远写着地址"的形态延续。
    裁决侧仍是「数据集根目录」下拉(它选的是本地源数据集,不收 URL)。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    (tmp_path / "ds").mkdir()       # 目录在 → 合成单桶按原样路径;不在且有
    # TOS_BUCKET 会切到直连默认(见 test_ui_perf 的部署感知用例)
    app = build_app(delivery, data_root=str(tmp_path / "ds"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    tin = [c["props"] for c in cfg["components"]
           if c["props"].get("label") == "数据集目录"]
    assert len(tin) == 1 and tin[0].get("visible", True)
    # 合成单桶(桶名未知)→ 默认值退回 datasets_path 原样(白名单精确匹配放行)
    assert str(tin[0].get("value") or "").endswith("ds")
    assert len(_src_dropdowns(app)) == 1          # 裁决侧那个还在


def test_multi_source_shows_dropdown_and_wires_it(delivery, tmp_path):
    """配了多个 TOS 桶(2026-08-20 融合改版):路径框默认预填第一桶的
    tos:// 地址,第二桶的地址经 resolve_root_input 对表到它的挂载 —— 多桶
    能力以"换个地址就换桶"的形态延续;路径框真接进了回调的输入。

    防:控件摆出来了却没接线(同 test_probe_buttons_are_wired 那次)。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    srcs = _two_sources(tmp_path)
    cfg_path = _site_yaml(tmp_path, srcs)
    app = build_app(delivery, config_path=cfg_path)
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    tin = [c["props"] for c in cfg["components"]
           if c["props"].get("label") == "数据集目录"]
    assert len(tin) == 1
    assert tin[0].get("value") == "tos://curation/datasets"   # 第一桶
    # 第二桶地址对表到它的挂载(快路径),陌生地址走直连
    bks = runner.tos_buckets(cfg_path, "/x")
    assert runner.resolve_root_input("tos://bucketa/prefix", bks) \
        == {"kind": "mount", "path": srcs[1]["datasets_path"], "bucket": bks[1]}
    assert runner.resolve_root_input("tos://strange/prefix", bks)["kind"] == "tos"
    tin_ids = {i for i, c in app.blocks.items()
               if getattr(c, "label", None) == "数据集目录"}
    used = {getattr(c, "_id", None) for fn in app.fns.values()
            for c in (getattr(fn, "inputs", []) or [])}
    assert tin_ids and tin_ids <= used, "「数据集目录」框没接进任何回调"


def test_deeplink_switches_source_dropdown_to_matching_bucket(delivery, tmp_path,
                                                              monkeypatch):
    """★深链(tos:// 完整地址)要同时落到**三处**:路径框切到根前缀、数据集
    下拉预选、说明行跟着换 —— 不许"数据集选上了、路径框还停在默认桶"
    (界面显示的桶和实际要跑的桶对不上,2026-08-17 用户点名要咬住的静默错)。

    2026-08-20 融合改版:known 桶 → 路径框=根前缀 URL、挂载清单;陌生桶不再
    整链拒绝(允许直连是本次融合的拍板),路径框照样切过去、清单走网络列
    (测试环境没凭证 → 列表失败落到说明行的 ⚠️ 一句话,数据集下拉为空+警告
    点名没找到 —— 全程无红框、无静默)。
    直接调 app.load 上挂的预填回调(零输入、输出 5 个:路径框/数据集/地区/
    两列说明),不起服务器;gr.Info/Warning 替换成记录器。
    """
    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_app
    warned = []
    monkeypatch.setattr(gr, "Info", lambda *a, **k: None)
    monkeypatch.setattr(gr, "Warning", lambda m, *a, **k: warned.append(str(m)))
    # ⚠️ 单测不许碰真网络:pod 上 TOS 凭证 env 齐全,不拦的话陌生桶那条分支
    # 会真去 list 一个不存在的桶 —— 网络一抖整个测试文件吊死(实测 5 分钟)。
    # 换成确定性假件:直连列表一律"列不出",正好也是无凭证环境的真实归宿。
    monkeypatch.setattr(runner, "tos_list_datasets",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("单测环境不出网")))
    cfg_path = _site_yaml(tmp_path, _two_sources(tmp_path))
    app = build_app(delivery, config_path=cfg_path)
    fns = [f for f in app.fns.values()
           if not (getattr(f, "inputs", []) or [])
           and [getattr(c, "label", None)
                for c in (getattr(f, "outputs", []) or [])][:3]
           == ["数据集目录", "数据集", "地区"]
           and len(getattr(f, "outputs", []) or []) == 5]
    assert len(fns) == 1, "应恰有一个输出为(路径框, 数据集, 地区, 两列说明)的预填回调"

    class Req:                                   # 新契约:完整地址 + 地区
        query_params = {"dataset": "tos://bucketa/prefix/only_b",
                        "region": "cn-beijing"}

    tin_upd, ds_upd, rg_upd, src_note, _ds_note = fns[0].fn(Req())
    assert tin_upd.get("value") == "tos://bucketa/prefix"     # 路径框切过去了
    assert ds_upd.get("value") == ["only_b"]
    assert rg_upd.get("value") == "cn-beijing"
    # 说明行跟着切过去的根走(备用桶只配了挂载,没配端点)
    assert src_note == ("挂载路径:"
                        f"{runner.tos_buckets(cfg_path, '/x')[1]['datasets_path']}")

    class Req2:                                  # 陌生桶:切过去 + 直连说明
        query_params = {"dataset": "tos://strange/prefix/demo_v2"}
    warned.clear()
    tin2, ds2, _rg2, note2, ds_note2 = fns[0].fn(Req2())
    assert tin2.get("value") == "tos://strange/prefix"
    assert str(note2).startswith("TOS 直连:")
    assert ds2.get("value") == [] and ds2.get("choices") == []
    assert any("demo_v2" in w for w in warned), "链接指的名字没找到必须点名"
    assert "⚠️" in str(ds_note2), "列不出清单要落到说明行,不许静默"


def test_single_source_dataset_choices_match_old_data_root(delivery, tmp_path):
    """没配 tos_buckets 的老部署:数据集下拉的候选仍来自 --data-root(逐项一致)
    —— 向后兼容的判据是**跑批用的路径与数据集清单不变**;界面多出的「TOS 桶」
    下拉是有意的(见 test_single_source_still_shows_source_dropdown)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    root = tmp_path / "ds"
    (root / "old_one" / "meta").mkdir(parents=True)
    (root / "old_one" / "meta" / "info.json").write_text("{}")
    app = build_app(delivery, data_root=str(root))
    cfg = json.dumps(json.loads(json.dumps(app.get_config_file(), default=str)),
                     ensure_ascii=False)
    assert "old_one" in cfg


# ───────── 下拉显示文本(2026-08-17 两轮:「默认」零信息量 → tos://桶/前缀)─────────

def test_declared_bucket_label_is_tos_url_not_mixed_address(tmp_path):
    """声明了 bucket → 显示 `tos://桶名/桶内前缀`,本地挂载路径不进显示串。

    防两次用户吐槽的回归:①「默认」占位零信息量;②`curation:/mnt/tos/datasets`
    把桶名和容器内挂载路径用冒号混成一串(curation 才是桶,datasets 是桶内
    前缀,/mnt/tos/datasets 是另一套地址空间里的本地路径)。"""
    cfg = _site_yaml(tmp_path, [{"bucket": "curation",
                                 "mount_root": "/mnt/tos",
                                 "datasets_path": "/mnt/tos/datasets"}])
    got = runner.tos_buckets(cfg, "/ignored")
    assert got[0]["label"] == "tos://curation/datasets"
    assert "/mnt/tos" not in got[0]["label"]
    # 挂整桶当数据集根(datasets_path == mount_root)→ 前缀空串,只印 tos://桶
    cfg2 = _site_yaml(tmp_path, [{"bucket": "curation",
                                  "mount_root": "/mnt/tos",
                                  "datasets_path": "/mnt/tos"}])
    got2 = runner.tos_buckets(cfg2, "/ignored")
    assert got2[0]["tos_prefix"] == "" and got2[0]["label"] == "tos://curation"


def test_bucket_label_admits_unknown_prefix_instead_of_guessing(tmp_path):
    """桶名声明了但前缀推导不出(缺 mount_root/tos_prefix)→ 显示串如实标
    「桶内前缀未知」,**不**回退到冒号混拼、也不编一个前缀顶上(诚实弃权)。"""
    cfg = _site_yaml(tmp_path, [{"bucket": "curation",
                                 "datasets_path": "/mnt/tos/datasets"}])
    got = runner.tos_buckets(cfg, "/ignored")
    assert got[0]["tos_prefix"] is None
    assert got[0]["label"] == "tos://curation(桶内前缀未知)"


def test_synthesized_bucket_label_admits_unknown_bucket():
    """合成桶(没配 tos_buckets)桶名未知 → 显示「(未声明桶):目录」。

    防:桶名不知道时编一个像样的顶上 —— 如实说不知道(诚实弃权),
    显示假桶名会让用户以为深链能对上它,实际全被拒。"""
    got = runner.tos_buckets(None, "/mnt/tos/datasets")
    assert got[0]["label"] == "(未声明桶):/mnt/tos/datasets"


def test_alias_only_shown_when_informative(tmp_path):
    """别名判别(规则写在 runner._bucket_label):等于桶名(大小写不敏感)、
    「默认」/「default」占位、由路径推导的名字,都不印;真别名(客户名)才前缀。

    ★钉住:别名=「默认」时显示文本里**不出现「默认」字样** —— 这正是用户吐槽
    的那个零信息占位,改显示规则时不许让它从别名口再漏回来。"""
    cfg = _site_yaml(tmp_path, [
        {"name": "默认", "bucket": "curation", "tos_prefix": "a",
         "datasets_path": "/a"},
        {"name": "Curation", "bucket": "curation", "tos_prefix": "b",
         "datasets_path": "/b"},
        {"bucket": "c3", "mount_root": "/mnt/tos3",
         "datasets_path": "/mnt/tos3/datasets"},   # 名从路径推导
        {"name": "客户A", "bucket": "cust-a-data", "mount_root": "/mnt/tos-a",
         "datasets_path": "/mnt/tos-a/datasets"},
        {"name": "default", "datasets_path": "/e"},                # 占位 + 桶未知
    ])
    got = runner.tos_buckets(cfg, "/ignored")
    assert [b["label"] for b in got] == [
        "tos://curation/a",                        # 「默认」占位不印
        "tos://curation/b",                        # 别名=桶名(忽略大小写)不重复印
        "tos://c3/datasets",                       # 路径推导名不印(零增量)
        "客户A · tos://cust-a-data/datasets",      # 真别名才前缀
        "(未声明桶):/e",                           # 占位不印 + 桶名如实未知
    ]
    assert "默认" not in got[0]["label"]
    # 内部标识不受显示规则影响(value 稳定,深链/白名单还靠它)
    assert [b["name"] for b in got] == ["默认", "Curation", "datasets",
                                        "客户A", "default"]


def test_dropdown_value_is_stable_id_and_label_never_a_key(delivery, tmp_path):
    """★两条硬约束一起钉:①下拉 choices 是 (显示文本, 内部标识) 成对,value 仍是
    配置里的稳定标识;②白名单查表只认标识,把显示文本当 key 传必须被拒。

    防:把显示串当 key 用 —— 改个显示格式(加别名/换分隔符)就让选中项对不上
    白名单,或者反过来让伪造的"像显示文本的字符串"绕进 bucket_path。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg_path = _site_yaml(tmp_path, _two_sources(tmp_path))
    app = build_app(delivery, config_path=cfg_path)
    for props in _src_dropdowns(app):
        pairs = [tuple(c) for c in props["choices"]]
        assert [v for _lab, v in pairs] == ["默认", "备用桶"]     # value=内部标识
        assert all(lab.startswith(("tos://", "备用桶 · tos://"))
                   for lab, _v in pairs)                         # 显示=tos://桶/前缀
    buckets = runner.tos_buckets(cfg_path, "/ignored")
    for b in buckets:
        assert runner.bucket_path(buckets, b["name"]) == b["datasets_path"]
        with pytest.raises(ValueError):
            runner.bucket_path(buckets, b["label"])              # 显示文本不是 key


def test_deeplink_info_uses_display_label(tmp_path):
    """深链命中非默认桶时,Info 里的桶用**显示文本**(bucket_label),不是内部
    标识 —— 用户在下拉里看见的就是显示文本,提示印内部名对不上号。"""
    cfg = _site_yaml(tmp_path, [
        {"bucket": "curation", "tos_prefix": "datasets", "datasets_path": "/a"},
        {"name": "客户A", "bucket": "cust-a-data", "tos_prefix": "x",
         "datasets_path": "/b"},
    ])
    buckets = runner.tos_buckets(cfg, "/ignored")
    lister = lambda p: {"/a": ["demo_v2"], "/b": ["demo_v2"]}[p]
    plan = runner.prefill_plan(["tos://cust-a-data/x/demo_v2"], buckets, lister)
    assert plan["source"] == "客户A"                    # 返回的仍是内部标识
    assert "客户A · tos://cust-a-data/x" in plan["info"]  # 文案用显示文本
    assert "TOS 桶" in plan["info"]


# ───────── 配置键改名(data_sources → tos_buckets,2026-08-17)─────────

def test_old_config_key_data_sources_errors_loudly(tmp_path):
    """★旧键 `data_sources` 出现 → 明确报错点名"已改名 tos_buckets"。

    防:静默当没配置 → 退化成合成单桶(桶名未知),所有 tos:// 深链被如实拒为
    "未接入" —— 表象是"深链坏了",真因是配置键名过期,这种错最难查。改名当天
    没有任何存量部署,所以不做兼容,只做响亮的报错。"""
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump({"data_sources": [
        {"bucket": "curation", "datasets_path": "/a"}]}, allow_unicode=True),
        encoding="utf-8")
    with pytest.raises(ValueError, match="tos_buckets"):
        runner.tos_buckets(str(p), "/a")


# ───────── 桶内前缀推导(2026-08-17:配置补齐"桶内前缀"这一环)─────────

def test_prefix_derivation_prefers_explicit_then_mount_root(tmp_path):
    """前缀来源优先级:显式 tos_prefix > 由 datasets_path 相对 mount_root 推导
    > 未知(None)。推导优于再写一遍 —— 两处各写一份迟早漂移;但显式给了就
    以显式为准(只补空缺不覆盖:用户明说的信号永远最强)。"""
    cfg = _site_yaml(tmp_path, [
        # 显式 tos_prefix 覆盖推导(推导会得出 datasets,显式说是 raw)
        {"bucket": "b1", "mount_root": "/mnt/tos", "tos_prefix": "raw",
         "datasets_path": "/mnt/tos/datasets"},
        # 只有 mount_root → 推导;多级前缀、尾斜杠都要规范化干净
        {"bucket": "b2", "mount_root": "/mnt/tos/",
         "datasets_path": "/mnt/tos/a/b/"},
        # 两者都没有 → 未知,不许猜
        {"bucket": "b3", "datasets_path": "/somewhere/else"},
    ])
    got = runner.tos_buckets(cfg, "/ignored")
    assert [b["tos_prefix"] for b in got] == ["raw", "a/b", None]


def test_prefix_derivation_bad_mount_root_is_unknown_and_logged(tmp_path, capsys):
    """datasets_path 不在 mount_root 之下(配置写错)→ 前缀按**未知**处理并在
    启动日志点名,绝不算出个负数层级的鬼东西;深链对这条按"配置不全"拒。"""
    cfg = _site_yaml(tmp_path, [
        {"bucket": "curation", "mount_root": "/mnt/tos",
         "datasets_path": "/data/datasets"},
    ])
    got = runner.tos_buckets(cfg, "/ignored")
    assert got[0]["tos_prefix"] is None
    assert got[0]["label"] == "tos://curation(桶内前缀未知)"
    err = capsys.readouterr().err
    assert "mount_root" in err and "/data/datasets" in err


# ───────── 下拉底下的只读说明(读取端点 + 挂载,两行)─────────

def test_bucket_info_line_two_lines_no_network_scope_tag(tmp_path):
    """说明 = 两行:`端点:主机名` 与 `挂载路径:本地目录`(\\n 分隔,端点在上)。

    「(内网)/(公网)」标注**必须没有**(2026-08-17 用户实机点名删):它描述
    的是 pod→TOS 的读取路径,不是用户→界面的连接方式,用户浏览器根本不碰这个
    端点 —— 看到「内网」会误以为在说自己怎么连的界面。谁"顺手补全"加回来,
    这条就咬谁。「端点」保留(用户拍板):它是 tos://桶/前缀 里唯一缺的
    那半截信息 —— 地域。"""
    cfg = _site_yaml(tmp_path, [
        {"bucket": "curation", "mount_root": "/mnt/tos",
         "datasets_path": "/mnt/tos/datasets",
         "endpoint": "https://tos-s3-cn-beijing.ivolces.com"},
        {"bucket": "pub", "mount_root": "/mnt/pub",
         "datasets_path": "/mnt/pub/datasets",
         "endpoint": "https://tos-cn-beijing.volces.com"},
    ])
    got = runner.tos_buckets(cfg, "/ignored")
    assert runner.bucket_info_line(got[0]) == (
        "端点:tos-s3-cn-beijing.ivolces.com\n挂载路径:/mnt/tos/datasets")
    line_pub = runner.bucket_info_line(got[1])
    assert line_pub == "端点:tos-cn-beijing.volces.com\n挂载路径:/mnt/pub/datasets"
    for line in (runner.bucket_info_line(got[0]), line_pub):
        assert "内网" not in line and "公网" not in line


def test_bucket_info_line_without_endpoint_keeps_mount_line(tmp_path):
    """配置没给 endpoint → **只印挂载那行**(挂载路径始终是真话),不编端点
    (tos:// 里不含地域,没有就是没有,诚实弃权)。合成单桶(老部署)同理。"""
    cfg = _site_yaml(tmp_path, [
        {"bucket": "bare", "mount_root": "/mnt/b",
         "datasets_path": "/mnt/b/datasets"},              # 没给端点
    ])
    got = runner.tos_buckets(cfg, "/ignored")
    assert runner.bucket_info_line(got[0]) == "挂载路径:/mnt/b/datasets"
    assert runner.bucket_info_line(runner.tos_buckets(None, "/a")[0]) == "挂载路径:/a"


def test_bucket_info_line_empty_when_nothing_to_say():
    """端点、挂载路径都没有 → 整段空串(界面渲染零高度,不摆一个空壳说明)。"""
    assert runner.bucket_info_line({}) == ""
    assert runner.bucket_info_line({"endpoint": "", "datasets_path": "  "}) == ""


def test_dataset_pickers_carry_no_info_and_notes_moved_below(delivery, tmp_path):
    """★防回退:两级下拉(数据集根目录/数据集/原始数据集)**不再带 info=**。

    Gradio 把 info 渲染在标签与控件**之间**:三列同一行时,谁带说明谁的控件就
    被往下推,另两列停在原位 → 下拉根本没对齐(2026-08-17 用户实机点名)。
    说明挪到了控件下方独立的 Markdown 说明行;这里断言 info 为空,既是"说明已
    挪出控件"的判据,也是三列对齐的代理判据(单测起不了浏览器量像素)。
    顺带钉住:说明组件真的存在且初值非空 —— "挪出去"不许做成"删没了"。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    cfg_path = _site_yaml(tmp_path, _two_sources(tmp_path))
    app = build_app(delivery, config_path=cfg_path)
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    picked = [c["props"] for c in cfg["components"]
              if c["props"].get("label") in {"数据集目录", "数据集根目录",
                                             "数据集", "原始数据集"}]
    assert len(picked) == 4      # 跑质检(路径框+数据集)+ 裁决(根+原始数据集)
    assert all(not p.get("info") for p in picked), "下拉不许再带 info="
    notes = {c["props"].get("elem_id"): c["props"] for c in cfg["components"]
             if c["props"].get("elem_id") in {"rn-src-note", "rn-ds-note",
                                              "rj-src-note", "rj-ds-note"}}
    assert set(notes) == {"rn-src-note", "rn-ds-note", "rj-src-note", "rj-ds-note"}
    # 根目录那列的说明初值 = 挂载路径(本夹具没配端点 → 只有挂载那行)
    assert str(notes["rn-src-note"].get("value") or "").startswith("挂载路径:")


# ───────── 根目录三态探测(没挂上 ≠ 挂了但空)─────────

def test_probe_dataset_root_distinguishes_three_states(tmp_path):
    """★三态分明:目录不存在/读不了 = unmounted(部署事故),存在但没有有效
    数据集 = empty(正常状态),有数据集 = ok。

    防的事故(2026-08-17):FSX 没挂上时界面只是"数据集下拉是空的",和"目录
    确实还没数据"一个模样 —— 部署事故被当成正常,没人去查挂载。"""
    missing = tmp_path / "not-mounted"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "junk.txt").write_text("非数据集文件不算数")
    ok = tmp_path / "ok"
    (ok / "d1" / "meta").mkdir(parents=True)
    (ok / "d1" / "meta" / "info.json").write_text("{}")
    assert runner.probe_dataset_root(str(missing), ttl=0) == "unmounted"
    assert runner.probe_dataset_root(str(empty), ttl=0) == "empty"
    assert runner.probe_dataset_root(str(ok), ttl=0) == "ok"
    # 三态各配一句话:未挂载要人去查部署;空目录明说挂载正常;正常不打扰
    assert "请检查部署" in runner.dataset_root_note(str(missing), ttl=0)
    assert runner.dataset_root_note(str(empty), ttl=0) == "挂载正常,里面还没有数据集"
    assert runner.dataset_root_note(str(ok), ttl=0) == ""


def test_probe_dataset_root_caches_within_ttl(tmp_path, monkeypatch):
    """探测结果在 TTL 内走缓存,不重复打盘 —— 对象存储挂载上 stat/listdir 不
    便宜,而探测出现在每次下拉渲染里;TTL 过后要能看见挂载修好(不用重启)。"""
    root = tmp_path / "r"
    calls = {"n": 0}
    real_listdir = os.listdir

    def counting_listdir(p):
        if str(p) == str(root):
            calls["n"] += 1
        return real_listdir(p)

    monkeypatch.setattr(os, "listdir", counting_listdir)
    runner._root_probe_cache.pop(str(root), None)
    assert runner.probe_dataset_root(str(root)) == "unmounted"
    assert runner.probe_dataset_root(str(root)) == "unmounted"
    assert calls["n"] == 1                       # 第二次没打盘
    root.mkdir()
    assert runner.probe_dataset_root(str(root)) == "unmounted"   # 缓存内旧值
    assert runner.probe_dataset_root(str(root), ttl=0) == "empty"  # 绕缓存见新值


def test_unmounted_root_is_marked_in_choices_and_does_not_crash_ui(delivery,
                                                                   tmp_path):
    """★未挂载的根:下拉项标「⚠️ 未挂载」,但 UI 照常建起来(别的桶可能是好
    的,炸掉整个界面是过度反应);正常挂载的项不带标记。"""
    sources = _two_sources(tmp_path)
    sources[1]["datasets_path"] = str(tmp_path / "gone")   # 备用桶没挂上
    choices = runner.bucket_dropdown_choices(sources)
    assert [v for _lab, v in choices] == ["默认", "备用桶"]  # value 仍是稳定标识
    assert "⚠️ 未挂载" not in choices[0][0]
    assert choices[1][0].endswith("⚠️ 未挂载")
    # Gradio 层:同样的配置建 app 不炸,且标记随显示文本进了下拉
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    runner._root_probe_cache.clear()             # 上面探测过的路径别吃旧缓存
    cfg_path = _site_yaml(tmp_path, sources)
    app = build_app(delivery, config_path=cfg_path)
    labels = [lab for props in _src_dropdowns(app)
              for lab, _v in (tuple(c) for c in props["choices"])]
    assert any(lab.endswith("⚠️ 未挂载") for lab in labels)


def test_switching_root_clears_stale_notes_instead_of_leaving_them(delivery,
                                                                   tmp_path):
    """★切根目录时,两列说明"该变什么"就必须真的变过去 —— 不许残留上一个根的话。

    2026-08-17 实机复现的 bug(info= 时代):回调里写的是 `info=... or None`,
    本意"没内容就清掉",但 **gradio 把 None 当成"这个字段不用改"**,于是:切到
    没配端点的根,仍印上一个根的端点;从未挂载的根切回正常根,仍挂着
    「⚠️ 这个根目录没挂上」。界面拿旧信息冒充当前状态,和要消灭的"静默跑错
    数据"是同一类错。

    说明如今是独立 Markdown 组件(同日对齐返工挪出 info=),同一事故的新判据:
    **回调对说明列返回的必须是字符串本身**(该空时就是空串)—— 返回 None 或
    gr.update() 跳过,都会让旧文字原地不动,原 bug 换个壳还魂。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    srcs = _two_sources(tmp_path)
    srcs[0]["endpoint"] = "https://tos-s3-cn-beijing.ivolces.com"   # 有端点
    srcs[1].pop("endpoint", None)                                    # 没端点
    app = build_app(delivery, config_path=_site_yaml(tmp_path, srcs))
    fns = [f for f in app.fns.values()
           if (getattr(f, "inputs", []) or [])
           and [getattr(c, "elem_id", None)
                for c in (getattr(f, "outputs", []) or [])]
           == ["rn-src-note", None, "rn-ds-note"]]
    assert fns, "没找到切根路径的回调(输出应为 根说明/数据集下拉/数据集说明)"
    fn = fns[0].fn                       # 2026-08-20 起 = _root_changed(url, region)

    note_a, _ds_a, ds_note_a = fn("tos://curation/datasets", "")
    assert "端点:" in note_a and "挂载路径:" in note_a, "配了端点的根该印两行"

    note_b, _ds_b, ds_note_b = fn("tos://bucketa/prefix", "")
    assert isinstance(note_b, str) and "读取端点" not in note_b, \
        "没配端点的根:端点那行必须消失,不许残留上一个根的"
    assert note_b == f"挂载路径:{srcs[1]['datasets_path']}"
    assert ds_note_b == "", "根目录正常:状态提示必须是空串,不能 None/跳过"


# ───────── 深链可选端点(2026-08-17:rerun 把「Open from TOS」的端点一并传来)─────────
#
# 背景:tos:// URL 里不含地域,链接指向没接入的桶时答不出"它在哪儿"。rerun 侧
# 加 endpoint 参数、这边加**可选**解析 —— 带不带都要能跑,她那边什么时候改都
# 不会坏。该值来自 URL,是不可信输入:只进提示文案,绝不进任何读取路径。


class _EPQP(dict):
    """带 getlist 的 query 参数替身(同 deeplink_values 那组用例的手法)。"""

    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v is not None else [])


def _ep_sources(endpoint="https://tos-cn-beijing.ivolces.com"):
    """单桶夹具:已接入 curation 桶 datasets/ 前缀,端点可换可删(None=不配)。"""
    b = {"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
         "datasets_path": "/a"}
    if endpoint:
        b["endpoint"] = endpoint
    return [b]


def test_deeplink_without_endpoint_four_wordings_verbatim():
    """★兼容红线(硬):链接没给端点 → 四种既有提示**逐字不变**。

    防:这轮给提示语"顺手润色"或无条件拼上端点后缀 —— rerun 现网还在发不带
    endpoint 的深链,措辞是上一轮刚验收过的,差一个字都算回归。调用刻意用
    旧签名(不传 link_endpoint),同时钉死默认值就是"没给"。
    """
    lister = lambda p: ["demo_v2"]
    # ① 桶未接入
    plan = runner.prefill_plan(["tos://strange/datasets/demo_v2"],
                               _ep_sources(), lister)
    assert plan["notices"] == [
        "链接指向的桶「strange」未接入本实例,没有预选「demo_v2」"
        "(不会在默认桶里找同名数据集)"]
    # ② 前缀对不上
    plan = runner.prefill_plan(["tos://curation/raw/demo_v2"],
                               _ep_sources(), lister)
    assert plan["notices"] == [
        "桶「curation」已接入本实例,但只接入了「datasets/」,"
        "链接指向的「raw/」没有接入 —— 没有预选「demo_v2」"]
    # ③ 本站前缀未知
    plan = runner.prefill_plan(["tos://curation/datasets/demo_v2"],
                               [{"name": "默认", "bucket": "curation",
                                 "tos_prefix": None, "datasets_path": "/a"}],
                               lister)
    assert plan["notices"] == [
        "桶「curation」已接入本实例,但本站配置没写清它的桶内前缀"
        "(tos_buckets 缺 mount_root 或 tos_prefix),没法核对链接指向的"
        "「datasets/」—— 没有预选「demo_v2」。这是本站配置不全,请补配置"]
    # ④ 数据集不在
    plan = runner.prefill_plan(["tos://curation/datasets/nope"],
                               _ep_sources(), lister)
    assert plan["notices"] == [
        "桶「curation」的数据集目录里没有「nope」(TOS 桶「默认」)"]
    # 预选成功的提示语同样一字不动
    ok = runner.prefill_plan(["tos://curation/datasets/demo_v2"],
                             _ep_sources(), lister)
    assert ok["info"] == "已按链接选中数据集:demo_v2。确认参数后点「开始质检」。"
    assert ok["notices"] == []


def test_deeplink_endpoint_same_region_cross_network_domain_is_silent():
    """桶已接入、链接端点与本实例**同地域**、只是内外网域不同(volces vs
    ivolces)→ 一声不提。

    防:把良性差异当矛盾刷警告 —— 同一个桶、同一份数据,只是 pod 走内网、
    rerun 走公网,提示了反而让人以为配置错了。
    """
    plan = runner.prefill_plan(
        ["tos://curation/datasets/demo_v2"], _ep_sources(),
        lambda p: ["demo_v2"], link_endpoint="tos-cn-beijing.volces.com")
    assert plan["datasets"] == ["demo_v2"] and plan["notices"] == []
    # 主机名完全一致更不必说
    plan2 = runner.prefill_plan(
        ["tos://curation/datasets/demo_v2"], _ep_sources(),
        lambda p: ["demo_v2"], link_endpoint="tos-cn-beijing.ivolces.com")
    assert plan2["notices"] == []


def test_deeplink_endpoint_region_conflict_names_both_regions_keeps_selection():
    """★桶已接入、链接端点与本实例**地域不同** → 提示矛盾且两个地域都印出来;
    **只提示,不改变选中行为**。

    防两头:① 桶名全局唯一,同名桶不可能在两个地域 —— 必有一错(链接错或
    本站配置错),不点名就是放过一处错配;② 因为矛盾就取消预选 —— 数据在
    挂载里好好的,读取根本不走端点,砍掉预选是把诊断信息当成了门禁。
    """
    plan = runner.prefill_plan(
        ["tos://curation/datasets/demo_v2"], _ep_sources(),
        lambda p: ["demo_v2"], link_endpoint="tos-ap-southeast-1.volces.com")
    assert plan["source"] == "默认" and plan["datasets"] == ["demo_v2"]
    assert plan["info"]                          # 预选照常
    conflicts = [n for n in plan["notices"]
                 if "ap-southeast-1" in n and "cn-beijing" in n]
    assert len(conflicts) == 1
    assert "tos-ap-southeast-1.volces.com" in conflicts[0]
    assert "tos-cn-beijing.ivolces.com" in conflicts[0]
    # 同桶多个数据集:矛盾是桶级的,只提一次,不逐条刷屏
    plan2 = runner.prefill_plan(
        ["tos://curation/datasets/demo_v2", "tos://curation/datasets/x2"],
        _ep_sources(), lambda p: ["demo_v2", "x2"],
        link_endpoint="tos-ap-southeast-1.volces.com")
    assert len([n for n in plan2["notices"] if "不在同一地域" in n]) == 1


def test_deeplink_endpoint_appended_to_unmatched_bucket_notice():
    """桶未接入、链接给了端点 → "未接入"那句**原文不动**,后面补上链接给的
    端点主机名,让人知道该去哪儿要权限。

    防:①改动原句(兼容红线);②端点信息丢掉 —— tos:// 里不含地域,这是
    唯一能回答"没接入的桶在哪儿"的线索。
    """
    plan = runner.prefill_plan(
        ["tos://strange/datasets/demo_v2"], _ep_sources(),
        lambda p: ["demo_v2"], link_endpoint="tos-cn-shanghai.volces.com")
    assert len(plan["notices"]) == 1
    n = plan["notices"][0]
    assert n.startswith(
        "链接指向的桶「strange」未接入本实例,没有预选「demo_v2」"
        "(不会在默认桶里找同名数据集)")
    assert "tos-cn-shanghai.volces.com" in n


def test_deeplink_endpoint_two_key_names_both_recognized():
    """endpoint / tos_endpoint 两个键名都认。

    防上一轮的原教训重演:只认一个键名,rerun 侧换个写法这边就静默什么都
    不做 ——"深链没生效"和"深链选错"一样难查。
    """
    host = "tos-cn-beijing.volces.com"
    assert runner.deeplink_endpoint({"endpoint": host}) == (host, True)
    assert runner.deeplink_endpoint(
        {"tos_endpoint": f"https://{host}"}) == (host, True)
    # 并存按 ENDPOINT_KEYS 键序取第一个干净值;前面的脏、后面的干净也不丢
    assert runner.deeplink_endpoint(
        {"endpoint": "a.volces.com", "tos_endpoint": "b.volces.com"}
    ) == ("a.volces.com", True)
    assert runner.deeplink_endpoint(
        {"endpoint": "<img onerror=x>", "tos_endpoint": "b.volces.com"}
    ) == ("b.volces.com", True)
    # 键出现但消不干净:present=True(界面据此说"看不懂已忽略",不静默)
    assert runner.deeplink_endpoint({"endpoint": "<img onerror=x>"}) == (None, True)
    assert runner.deeplink_endpoint({}) == (None, False)
    # starlette 形态(getlist)也走得通
    assert runner.deeplink_endpoint(_EPQP({"endpoint": [host]})) == (host, True)


def test_endpoint_sanitizer_strips_or_rejects_untrusted_input():
    """★消毒(安全):该值来自 URL,只许留干净主机名,消不净当没给。

    防:提示语走 Markdown 组件渲染,原样回显 query 参数就是注入面(<img
    onerror=…> / [x](javascript:…) 这类);另外凭据、端口、路径、超长串都
    不许透传。断言分两层:sanitize 的产出要么 None 要么纯主机名字符;把脏串
    喂满整条管线后,任何提示里都不得出现原样串。
    """
    import re as _re
    san = runner.sanitize_endpoint
    # 合法输入:只留主机名(丢 scheme/凭据/端口/路径/query,统一小写)
    assert san("https://u:p@tos-cn-beijing.volces.com/a/b?c=1") == \
        "tos-cn-beijing.volces.com"
    assert san("tos-cn-beijing.ivolces.com:443/bucket") == \
        "tos-cn-beijing.ivolces.com"
    assert san("TOS-CN-BEIJING.VOLCES.COM") == "tos-cn-beijing.volces.com"
    evils = ["<img onerror=alert(1)>",
             "[x](javascript:alert(1))",       # 3.11+ 拒括号主机;老版剩纯净段
             "[x](https://evil.com/a)",
             "a" * 2000,                       # 超总长上限
             "b" * 300 + ".volces.com",        # 主机名超 253
             "host name with spaces",
             ""]
    for evil in evils:
        got = san(evil)
        assert got is None or _re.fullmatch(r"[a-z0-9.-]{1,253}", got), evil
        assert got != evil.lower(), evil       # 原样串绝不整串透传
    # 管线级:脏端点 + 未接入桶,提示里不得出现原样串(入口消毒挡在 plan 之前)
    for evil in evils[:3]:
        host, present = runner.deeplink_endpoint({"endpoint": evil,
                                                  "dataset": "x"})
        assert present
        plan = runner.prefill_plan(["tos://strange/datasets/demo_v2"],
                                   _ep_sources(), lambda p: ["demo_v2"],
                                   link_endpoint=host)
        assert all(evil not in n for n in plan["notices"]), evil


def test_endpoint_region_unrecognized_skips_conflict_and_never_crashes():
    """地域认不出(链接侧或本站侧)→ 跳过矛盾比对,预选照常,不崩。

    防:拿 example.com 这类主机名硬凑地域段,或没配端点的桶去比对时炸掉 ——
    "宽容且不猜":认得出才比,认不出就闭嘴(诚实弃权,不是硬凑一个地域)。
    """
    assert runner.endpoint_region("tos-cn-beijing.volces.com") == "cn-beijing"
    assert runner.endpoint_region("tos-ap-southeast-1.ivolces.com") == \
        "ap-southeast-1"
    assert runner.endpoint_region("tos-s3-cn-north-1.volces.com") == "cn-north-1"
    assert runner.endpoint_region("example.com") is None
    assert runner.endpoint_region(None) is None
    # 链接侧认不出地域:不提示不崩,预选照常
    plan = runner.prefill_plan(["tos://curation/datasets/demo_v2"],
                               _ep_sources(), lambda p: ["demo_v2"],
                               link_endpoint="example.com")
    assert plan["datasets"] == ["demo_v2"] and plan["notices"] == []
    # 本站没配端点:没有比对基准,同样闭嘴
    plan2 = runner.prefill_plan(["tos://curation/datasets/demo_v2"],
                                _ep_sources(endpoint=None),
                                lambda p: ["demo_v2"],
                                link_endpoint="tos-ap-southeast-1.volces.com")
    assert plan2["datasets"] == ["demo_v2"] and plan2["notices"] == []


def test_info_line_endpoint_always_instance_config_not_link(delivery, tmp_path,
                                                            monkeypatch):
    """★说明行的「端点:」永远印**本实例配置**的值,不被链接里的端点顶掉。

    防:那一行回答的是"**我们**从哪儿读这份数据",链接给的是"**他们**从哪儿
    读的"—— 拿链接值去填说明行,就是拿别人的读取路径冒充自己的,新的一类
    假信息。链接的端点只许出现在矛盾/未接入两种提示语里。
    """
    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_app
    warned = []
    monkeypatch.setattr(gr, "Info", lambda *a, **k: None)
    monkeypatch.setattr(gr, "Warning", lambda msg, *a, **k: warned.append(msg))
    srcs = _two_sources(tmp_path)
    srcs[1]["endpoint"] = "https://tos-cn-beijing.ivolces.com"
    app = build_app(delivery, config_path=_site_yaml(tmp_path, srcs))
    fns = [f for f in app.fns.values()
           if not (getattr(f, "inputs", []) or [])
           and [getattr(c, "label", None)
                for c in (getattr(f, "outputs", []) or [])][:3]
           == ["数据集目录", "数据集", "地区"]
           and len(getattr(f, "outputs", []) or []) == 5]
    assert len(fns) == 1

    class Req:                       # 深链带了另一地域的端点(旧契约键,仍兼容)
        query_params = {"dataset": "tos://bucketa/prefix/only_b",
                        "endpoint": "tos-ap-southeast-1.volces.com"}

    src_upd, ds_upd, _rg, src_note, _ = fns[0].fn(Req())
    assert src_upd.get("value") == "tos://bucketa/prefix"
    assert ds_upd.get("value") == ["only_b"]
    # 说明行 = 本实例配置的端点 + 挂载路径;链接的端点一个字不进说明行
    assert src_note == ("端点:tos-cn-beijing.ivolces.com\n"
                        f"挂载路径:{srcs[1]['datasets_path']}")
    # 矛盾提示照发(两个地域都点名),但只以 Warning 形态出现
    assert any("cn-beijing" in w and "ap-southeast-1" in w for w in warned)

    class Req2:                      # 端点消不干净:说一声"已忽略",不回显原样串
        query_params = {"dataset": "tos://bucketa/prefix/only_b",
                        "endpoint": "<img onerror=alert(1)>"}

    warned.clear()
    fns[0].fn(Req2())
    assert any("看不懂" in w for w in warned)
    assert all("<img" not in w for w in warned)
