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


def test_run_tab_is_pure_tos_no_mount_dropdown(delivery, tmp_path):
    """★「跑质检」是纯 TOS 直连(2026-08-19 用户拍板):输入 = 数据集 TOS 路径 +
    地区,输出 = TOS 路径 + 地区 + 交付名;挂载相关(数据集根目录下拉/挂载
    说明)一个都不出现 —— 那是部署细节,用户不需要关心。

    防两头:① 挂载 UI 回潮(「数据集根目录」悄悄回到跑质检页);② 新表单
    缺件(五个输入少一个,用户没法把一次跑批说完整)。
    「数据集根目录」只允许在裁决侧存在(交付没记源路径时用户得自己选)。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_console_app
    app = build_console_app(delivery, data_root=str(tmp_path / "ds"))
    assert len(_src_dropdowns(app)) == 1, "「数据集根目录」只该剩裁决侧那一个"
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    labels = {c["props"].get("label") for c in cfg["components"]}
    for need in ("数据集 TOS 路径", "数据集地区", "输出 TOS 路径",
                 "输出地区", "交付名"):
        assert need in labels, f"跑质检缺输入组件:{need}"


def test_region_dropdowns_match_rerun_list_and_allow_custom(delivery, tmp_path):
    """★两个地区下拉:选项与 rerun OpenTosModal 的 TOS_REGIONS 同值同序,且
    允许自由输入(列表可能落后于新开地区,rerun 侧同一条豁免)。

    防:两个产品各养一份地区清单,用户在深链间来回跳看到两套地区。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_console_app
    app = build_console_app(delivery, data_root=str(tmp_path / "ds"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    rgs = [c["props"] for c in cfg["components"]
           if c["props"].get("label") in ("数据集地区", "输出地区")]
    assert len(rgs) == 2
    for p in rgs:
        pairs = [tuple(c) for c in p["choices"]]
        assert [v for _lab, v in pairs] == list(runner.TOS_REGIONS), \
            "值必须是地区代码(后端/CLI/深链全用代码)"
        # 显示文本 =「中文名 (代码)」,中文名照火山控制台建桶页(2026-08-19 截图)
        labels = [lab for lab, _v in pairs]
        assert labels[0] == "华北2(北京) (cn-beijing)"
        assert all(v in lab for lab, v in pairs), "显示文本里必须能看到代码"
        assert p.get("allow_custom_value") is True


def _prefill_fn(app):
    """app.load 上挂的深链预填回调:无输入、输出 = (数据集 TOS 路径, 数据集地区)。"""
    fns = [f for f in app.fns.values()
           if not (getattr(f, "inputs", []) or [])
           and [getattr(c, "label", None)
                for c in (getattr(f, "outputs", []) or [])]
           == ["数据集 TOS 路径", "数据集地区"]]
    assert len(fns) == 1, "应恰有一个输出为(TOS 路径, 地区)的预填回调"
    return fns[0].fn


def test_deeplink_prefills_tos_url_for_any_bucket(delivery, tmp_path,
                                                  monkeypatch):
    """★深链预填(2026-08-19 纯 TOS 直连版):链接带 tos:// URL 就直接填进
    「数据集 TOS 路径」,**桶不再需要在本站"接入"**——链接指哪个桶就跑哪个桶,
    可达性由部署凭证决定。只预填不自动开跑(自动开跑 = 刷新一次页面就重复
    拉起任务,老规矩不变)。

    防两头:① 老的"桶不认识就拒绝"回潮(静态绑定复活);② 裸名字被硬塞进
    URL 框(裸名字没有桶信息,填进去就是一个必然跑失败的假地址 —— 要警告,
    不要编)。
    """
    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_console_app
    warned = []
    monkeypatch.setattr(gr, "Info", lambda *a, **k: None)
    monkeypatch.setattr(gr, "Warning", lambda msg, *a, **k: warned.append(msg))
    app = build_console_app(delivery, data_root=str(tmp_path / "ds"))
    fn = _prefill_fn(app)

    class Req:                                   # 只需 query_params 一个属性
        query_params = {"dataset": "tos://strange-bucket/prefix/demo_v2"}

    url_upd, _rg_upd = fn(Req())
    assert url_upd.get("value") == "tos://strange-bucket/prefix/demo_v2", \
        "任意桶的 tos:// URL 都该预填(桶自由是这次云产品化的目的本身)"

    class Req2:                                  # 裸名字:警告,不编地址
        query_params = {"dataset": "demo_v2"}

    warned.clear()
    url2, _rg2 = fn(Req2())
    assert "value" not in url2
    assert any("tos://" in w for w in warned), "裸名字必须有一句提示,不许静默"


def test_single_source_dataset_choices_match_old_data_root(delivery, tmp_path):
    """没配 tos_buckets 的老部署:数据集下拉的候选仍来自 --data-root(逐项一致)
    —— 向后兼容的判据是**跑批用的路径与数据集清单不变**;界面多出的「TOS 桶」
    下拉是有意的(见 test_single_source_still_shows_source_dropdown)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_console_app
    root = tmp_path / "ds"
    (root / "old_one" / "meta").mkdir(parents=True)
    (root / "old_one" / "meta" / "info.json").write_text("{}")
    app = build_console_app(delivery, data_root=str(root))
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

    2026-08-19 纯 TOS 直连收窄:跑质检侧的两级下拉与两列挂载说明整体退役
    (挂载是部署细节,不再见客),本条只剩裁决侧要钉 —— 挂载相关的说明
    (rn-src-note / rn-ds-note)在配置里**必须不存在**,防回潮。
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_console_app
    cfg_path = _site_yaml(tmp_path, _two_sources(tmp_path))
    app = build_console_app(delivery, config_path=cfg_path)
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    picked = [c["props"] for c in cfg["components"]
              if c["props"].get("label") in {"数据集根目录", "数据集", "原始数据集"}]
    assert len(picked) == 2          # 只剩裁决侧(根 + 原始数据集)
    assert all(not p.get("info") for p in picked), "下拉不许再带 info="
    ids = {c["props"].get("elem_id") for c in cfg["components"]}
    assert {"rj-src-note", "rj-ds-note"} <= ids
    assert not ({"rn-src-note", "rn-ds-note"} & ids), \
        "跑质检侧的挂载说明已退役,不许回潮"


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
    from curation.ui.app import build_console_app
    runner._root_probe_cache.clear()             # 上面探测过的路径别吃旧缓存
    cfg_path = _site_yaml(tmp_path, sources)
    app = build_console_app(delivery, config_path=cfg_path)
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
    (2026-08-19 起跑质检侧无挂载 UI,这个回调只剩裁决侧一处,判据不变。)
    """
    pytest.importorskip("gradio")
    from curation.ui.app import build_console_app
    srcs = _two_sources(tmp_path)
    srcs[0]["endpoint"] = "https://tos-s3-cn-beijing.ivolces.com"   # 有端点
    srcs[1].pop("endpoint", None)                                    # 没端点
    app = build_console_app(delivery, config_path=_site_yaml(tmp_path, srcs))
    fns = [f for f in app.fns.values()
           if (getattr(f, "inputs", []) or [])
           and [getattr(c, "elem_id", None)
                for c in (getattr(f, "outputs", []) or [])]
           == ["rj-src-note", None, "rj-ds-note"]]
    assert fns, "没找到切根目录的回调(输出应为 根说明/数据集下拉/数据集说明)"
    fn = fns[0].fn

    note_a, _ds_a, ds_note_a = fn("默认")
    assert "端点:" in note_a and "挂载路径:" in note_a, "配了端点的根该印两行"

    note_b, _ds_b, ds_note_b = fn("备用桶")
    assert isinstance(note_b, str) and "读取端点" not in note_b, \
        "没配端点的根:端点那行必须消失,不许残留上一个根的"
    assert note_b == f"挂载路径:{srcs[1]['datasets_path']}"
    assert ds_note_b == "", "根目录正常:状态提示必须是空串,不能 None/跳过"


def test_deeplink_region_param_presets_region_and_sanitizes(delivery, tmp_path,
                                                            monkeypatch):
    """★深链的 region 参数(2026-08-19 与 rerun 定的契约:Diagnose 按钮带
    `?dataset=tos://…&region=cn-beijing`):只用来预选「数据集地区」下拉,
    别的什么都不做。它是 URL 来的不可信输入:只认地区代码字符集
    (runner.deeplink_region),过不了就当没给。

    防两头:① 坏值被静默吞掉(要说"已忽略",且原样串一个字不回显 ——
    提示走 Markdown 组件,回显即注入面);② region 被接进任何请求路径
    (它只允许落到下拉的预选值上)。
    """
    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_console_app
    warned = []
    monkeypatch.setattr(gr, "Info", lambda *a, **k: None)
    monkeypatch.setattr(gr, "Warning", lambda msg, *a, **k: warned.append(msg))
    app = build_console_app(delivery, data_root=str(tmp_path / "ds"))
    fn = _prefill_fn(app)

    class Req:                       # 合法地区 → 下拉预选到它
        query_params = {"dataset": "tos://bucketa/prefix/only_b",
                        "region": "ap-southeast-1"}

    url_upd, rg_upd = fn(Req())
    assert url_upd.get("value") == "tos://bucketa/prefix/only_b"
    assert rg_upd.get("value") == "ap-southeast-1"

    class Req2:                      # 坏值:说一声"已忽略",不回显原样串
        query_params = {"dataset": "tos://bucketa/prefix/only_b",
                        "region": "<img onerror=alert(1)>"}

    warned.clear()
    url2, rg2 = fn(Req2())
    assert url2.get("value") == "tos://bucketa/prefix/only_b"   # URL 照填
    assert "value" not in rg2                                    # 地区不乱猜
    assert any("看不懂" in w for w in warned)
    assert all("<img" not in w for w in warned)

    class Req3:                      # 没带 region:地区下拉不动(部署缺省)
        query_params = {"dataset": "tos://bucketa/prefix/only_b"}

    warned.clear()
    url3, rg3 = fn(Req3())
    assert url3.get("value") and "value" not in rg3
    assert not warned                # 没给就没提示,别拿可选参数烦人


def test_deeplink_region_pure_function():
    """runner.deeplink_region 的口径:第一个合法值生效;坏值 = (None, True)
    (界面要据此提示);没这个键 = (None, False)(保持静默)。"""
    class QP(dict):
        def getlist(self, k):
            v = self.get(k)
            return v if isinstance(v, list) else ([v] if v is not None else [])

    assert runner.deeplink_region(QP({"region": "cn-beijing"})) == ("cn-beijing", True)
    assert runner.deeplink_region(QP({"region": ["坏值", "cn-shanghai"]})) \
        == ("cn-shanghai", True)
    assert runner.deeplink_region(QP({"region": "北京"})) == (None, True)
    assert runner.deeplink_region(QP({})) == (None, False)
    assert runner.deeplink_region({"region": "AP-SOUTHEAST-1"}) \
        == ("ap-southeast-1", True)          # 大小写宽容:统一小写后合法
