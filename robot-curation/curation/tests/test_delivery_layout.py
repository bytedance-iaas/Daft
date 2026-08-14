"""交付目录布局(2026-08-14 breaking change)的纯函数测试。

防的是这次事故:一份交付就是一个目录,同名重跑必须 `--overwrite`,而覆盖会
rmtree 掉整个 `details/` —— 人工裁决的三张 CSV 就住在里面。用户 2026-08-14 只是
想看看新报告长什么样而重跑了一次,旧结果连同**人花时间产生的裁决**一起没了。

因此这里钉死四件事:①每次跑批各进各的子目录,永不覆盖;②`latest` 只是一条记录
(而且写法必须扛得住 FSX);③裁决 CSV 在交付根,老位置只读兜底;④prune 默认只列
不删,且绝不"按最新自动顶掉旧的"。
"""
from __future__ import annotations

import datetime
import json
import os

import pytest

from curation.delivery import (allocate_run_dir, decisions_read_path,
                               decisions_write_path, delivery_root_of,
                               is_delivery, list_runs, prune_plan, read_latest,
                               resolve_run, run_choices, run_facts, run_label,
                               run_name_of_run_id, write_latest,
                               write_run_facts)


def _make_run(delivery, name, *, processed=None, total=None, exported=True,
              at="2026-08-14 07:40:45"):
    """造一次跑批:三件套里只放报告页真正用到的那个标志文件 + 事实卡。"""
    d = os.path.join(str(delivery), name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "passed.json"), "w", encoding="utf-8") as f:
        json.dump({"数据集": "droid", "生成时间": at,
                   "dataset": {"input_episodes": processed}}, f, ensure_ascii=False)
    facts = {"跑批": name, "数据集": "droid", "生成时间": at,
             "本次处理条数": processed, "导出数据集": exported}
    if total is not None:
        facts["数据集总条数"] = total
    write_run_facts(d, facts)
    return d


# ── ① 每次跑批各进各的目录 ────────────────────────────────────────────

def test_two_runs_never_share_a_directory(tmp_path):
    """同名重跑两次 = 两个目录,第一次的东西一个字不动(旧版会 rmtree 掉它)。"""
    deliv = tmp_path / "droid-200-full"
    a = allocate_run_dir(str(deliv), "20260814-074045")
    (open(os.path.join(a, "passed.json"), "w")).write("{}")
    b = allocate_run_dir(str(deliv), "20260814-131200")
    assert a != b
    assert os.path.exists(os.path.join(a, "passed.json"))       # 第一次仍在
    # 同一秒又起一次(或任务台重发同一个编号):加后缀,绝不往已有目录里写
    c = allocate_run_dir(str(deliv), "20260814-074045")
    assert os.path.basename(c) == "20260814-074045-2"


def test_run_name_comes_from_run_id_timestamp():
    """任务台的编号 `20260814-074045-run` → 目录名只取时间戳。

    `-run` 是我们的内部词(子命令名),不该出现在客户看得见的目录名里;而前缀相同
    就足以让"哪次跑批产生了这份结果"对得上号。
    """
    assert run_name_of_run_id("20260814-074045-run") == "20260814-074045"
    assert run_name_of_run_id("20260814-074045-run-2") == "20260814-074045"
    assert run_name_of_run_id("怪编号") == "怪编号"           # 认不出就原样,不编时间


def test_new_run_name_has_no_timezone_word():
    """目录名里不写时区缩写:现在是夏令时,写死 PST 半年后就是错的。"""
    from curation.delivery import new_run_name
    name = new_run_name(datetime.datetime(2026, 8, 14, 7, 40, 45))
    assert name == "20260814-074045"


# ── ② latest 只是一条记录 ────────────────────────────────────────────

def test_latest_roundtrip_and_pointing_nowhere(tmp_path):
    """写完读回必须一致(FSX 上库直写曾静默产出零填充坏文件);指向不存在的目录
    等于没记录 —— 报告页宁可退回"最新的一次",也不能默认打开一份空页。"""
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-074045")
    write_latest(str(deliv), "20260814-074045")
    assert read_latest(str(deliv)) == "20260814-074045"
    assert (deliv / "latest").read_text(encoding="utf-8").strip() == "20260814-074045"
    (deliv / "latest").write_text("20260101-000000\n")          # 指向不存在的一次
    assert read_latest(str(deliv)) == ""


def test_write_latest_does_not_fail_on_fsx_visibility_delay(tmp_path, monkeypatch):
    """写完立刻读回是**空的**不算失败(2026-08-14 e2e 实测):FSX 上新文件有 20-60s
    读不回来,第一版在这里硬校验,把一次跑得好好的批报成了"latest 没写成"。
    内容对不对由回验那一关轮询着看(latest_matches)。"""
    from curation import delivery as dl
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-074045")
    real_open = open

    def _blind_open(path, *a, **kw):          # 模拟"写得进去、还读不回来"
        mode = (a[0] if a else kw.get("mode", "r"))
        if str(path).endswith("latest") and "w" not in mode and "a" not in mode:
            raise OSError(2, "No such file or directory")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(dl, "open", _blind_open, raising=False)
    monkeypatch.setattr("builtins.open", _blind_open)
    dl.write_latest(str(deliv), "20260814-074045")        # 不许抛
    assert dl.latest_matches(str(deliv), "20260814-074045") is False   # 诚实说没读到
    monkeypatch.undo()
    assert dl.latest_matches(str(deliv), "20260814-074045") is True    # 可见之后就对上


def test_resolve_run_prefers_latest_then_newest(tmp_path):
    """默认打开哪一次:latest 记的那次 > 最新的一次。**它不是"该用这份"的意思**
    —— 1812 跑 20 条不比 0530 跑 200 条更值得用,所以口径只有"最近",没有"最好"。"""
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    assert resolve_run(str(deliv)).endswith("20260814-181200")   # 无 latest → 最新
    write_latest(str(deliv), "20260814-053000")
    assert resolve_run(str(deliv)).endswith("20260814-053000")   # 有记录 → 听记录的


def test_legacy_delivery_still_opens(tmp_path):
    """老布局(passed.json 直接在交付目录里)必须照常打得开:别人的旧交付不许因为
    我们改了目录形状就变成打不开的东西。"""
    d = tmp_path / "aloha-10"
    d.mkdir()
    (d / "passed.json").write_text("{}")
    assert is_delivery(str(d)) and resolve_run(str(d)) == str(d)
    assert list_runs(str(d)) == []
    assert len(run_choices(str(d))) == 1                        # 「运行」只有一项


# ── ③ 人工裁决:新位置写,老位置读得到 ──────────────────────────────

def test_decisions_write_to_delivery_root(tmp_path):
    """裁决写的是交付根的 human-decisions/,哪怕调用方拿着的是某一次跑批的目录。

    人裁的是"这条 episode 到底怎么回事",数据没变时重跑一百次都还算数;混在会被
    清理的机器产出里,就是这次事故的成因。
    """
    deliv = tmp_path / "d"
    run = _make_run(deliv, "20260814-074045")
    assert delivery_root_of(run) == str(deliv)
    assert decisions_write_path(run, "label_decisions.csv") == \
        str(deliv / "human-decisions" / "label_decisions.csv")
    # 交付目录传进来也是同一个答案(老交付走这条)
    assert decisions_write_path(str(deliv), "label_decisions.csv") == \
        str(deliv / "human-decisions" / "label_decisions.csv")


@pytest.mark.parametrize("where", ["run", "delivery"])
def test_decisions_fall_back_to_old_location(tmp_path, where):
    """老交付的裁决还躺在 details/ 里:读得到(新位置优先,老位置兜底),
    而且要能告诉调用方"这是老位置",好留一行日志。老交付**不自动迁移**。"""
    deliv = tmp_path / "d"
    run = _make_run(deliv, "20260814-074045")
    base = run if where == "run" else str(deliv)
    old = os.path.join(base, "details", "task_verdicts.csv")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with open(old, "w") as f:
        f.write("episode_id,verdict,note,at\nep000001,判成功,,t\n")
    path, legacy = decisions_read_path(run, "task_verdicts.csv")
    assert (path, legacy) == (old, True)
    # 新位置一旦有文件,它赢(写入一律写新位置,老的只是历史)
    new = decisions_write_path(run, "task_verdicts.csv")
    os.makedirs(os.path.dirname(new), exist_ok=True)
    with open(new, "w") as f:
        f.write("episode_id,verdict,note,at\n")
    assert decisions_read_path(run, "task_verdicts.csv") == (new, False)


def test_decisions_survive_a_new_run(tmp_path):
    """本次改造要防住的原始事故:裁决完再跑一次,裁决还在。"""
    from curation.dataset_level.decisions import (load_task_verdicts,
                                                  record_task_verdict)
    deliv = tmp_path / "droid-200-full"
    run1 = _make_run(deliv, "20260814-074045")
    record_task_verdict(run1, "ep000007", "判成功", note="看了视频")
    run2 = allocate_run_dir(str(deliv), "20260814-131200")       # 重跑一次
    assert load_task_verdicts(run2)["ep000007"]["verdict"] == "判成功"
    assert load_task_verdicts(run1)["ep000007"]["verdict"] == "判成功"


# ── ④ 运行列表只陈述事实 ────────────────────────────────────────────

def test_run_label_states_facts_only(tmp_path):
    """列表上只写事实。**不许出现"抽查"/"完整"这类判断** —— 用户只说了"跑前 20 条",
    没说他为什么,系统不知道意图就不能替他下结论。数据集总条数拿得到才写,
    拿不到就只写本次条数,绝不编。"""
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-181200", processed=20, total=200)
    _make_run(deliv, "20260814-053000", processed=200, exported=False)
    labels = dict((v, lab) for lab, v in run_choices(str(deliv)))
    small = labels[str(deliv / "20260814-181200")]
    big = labels[str(deliv / "20260814-053000")]
    assert "本次处理 20 条(数据集共 200 条)" in small
    assert "本次处理 200 条" in big and "数据集共" not in big     # 总数未知就不说
    assert "只出报告(无导出数据集)" in big and "只出报告" not in small
    for word in ("抽查", "完整", "推荐", "建议用"):
        assert word not in small and word not in big


def test_run_choices_newest_first_and_marks_latest(tmp_path):
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    write_latest(str(deliv), "20260814-053000")
    items = run_choices(str(deliv))
    assert [os.path.basename(v) for _lab, v in items] == ["20260814-181200",
                                                          "20260814-053000"]
    assert "最近一次" in dict((v, lab) for lab, v in items)[
        str(deliv / "20260814-053000")]


def test_run_facts_fall_back_to_passed_json(tmp_path):
    """事实卡缺席(跑批被打断/更早的版本跑的)→ 退回 passed.json 里的读数,
    读不到的字段给 None,由渲染侧决定不说。"""
    deliv = tmp_path / "d"
    run = _make_run(deliv, "20260814-074045", processed=7)
    os.unlink(os.path.join(run, "run.json"))
    f = run_facts(run)
    assert f["processed"] == 7 and f["dataset_total"] is None
    assert f["exported"] is False                # 目录里确实没有导出数据集
    assert "本次处理 7 条" in run_label(f)


# ── ⑤ prune:先列出,默认不删 ───────────────────────────────────────

def test_prune_lists_without_deleting_by_default(tmp_path):
    """不给保留口径就只列不删:哪一份更值钱只有人知道。"""
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    plan = prune_plan(str(deliv))
    assert [f["name"] for f in plan["runs"]] == ["20260814-181200", "20260814-053000"]
    assert plan["delete"] == []
    assert all(os.path.isdir(f["path"]) for f in plan["runs"])   # 算清单不动盘


def test_prune_never_deletes_the_latest_record(tmp_path):
    """**绝不"按最新自动顶掉旧的"**(用户点名否掉):1812 跑 20 条不能顶掉 0530 跑
    200 条的成果。留最新 1 次时,latest 记的那次即使更旧也照样保留。"""
    deliv = tmp_path / "d"
    _make_run(deliv, "20260813-090000", processed=5)
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    write_latest(str(deliv), "20260814-053000")
    plan = prune_plan(str(deliv), keep_latest=1)
    assert [f["name"] for f in plan["delete"]] == ["20260813-090000"]
    assert {f["name"] for f in plan["keep"]} == {"20260814-181200", "20260814-053000"}


def test_prune_scope_excludes_human_decisions(tmp_path):
    """human-decisions/ 与 latest 永远不在删除范围内 —— 它们不是某一次跑批的产物。"""
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    (deliv / "human-decisions").mkdir()
    (deliv / "human-decisions" / "task_verdicts.csv").write_text("episode_id\n")
    write_latest(str(deliv), "20260814-181200")
    plan = prune_plan(str(deliv), keep_latest=1)
    names = {f["name"] for f in plan["runs"]}
    assert "human-decisions" not in names and "latest" not in names
    assert [f["name"] for f in plan["delete"]] == ["20260814-053000"]


def test_prune_refuses_to_empty_a_delivery(tmp_path):
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    with pytest.raises(ValueError):
        prune_plan(str(deliv), keep_latest=0)


# ── ⑥ 交付发现:列交付,不平铺跑批 ──────────────────────────────────

def test_discover_returns_deliveries_not_runs(tmp_path):
    """报告页顶部那个下拉列的是**交付名**;把每次跑批平铺成几十个条目正是这次要
    改掉的东西。老布局的交付照样被发现。"""
    from curation.ui.manifest import delivery_choices, discover_deliveries
    root = tmp_path / "deliveries"
    _make_run(root / "droid-200-full", "20260814-074045")
    _make_run(root / "droid-200-full", "20260814-131200")
    old = root / "aloha-10"
    old.mkdir(parents=True)
    (old / "passed.json").write_text("{}")
    found = discover_deliveries(str(root))
    assert sorted(os.path.basename(p) for p in found) == ["aloha-10", "droid-200-full"]
    labels = [lab for lab, _v in delivery_choices(str(root), found)]
    assert sorted(labels) == ["aloha-10", "droid-200-full"]


def test_runs_dir_stays_at_deliveries_root(tmp_path):
    """`.runs` 挂在**交付根的父目录**(交付扫描根),不许钻进某一份交付里 ——
    进去了下次扫描就会把它当成那份交付的一部分。"""
    from curation.ui.runner import deliveries_root_of, runs_root_of
    root = tmp_path / "deliveries"
    deliv = root / "droid-200-full"
    _make_run(deliv, "20260814-074045")
    assert deliveries_root_of(str(root)) == str(root)
    assert deliveries_root_of(str(deliv)) == str(root)          # 新布局的交付
    assert runs_root_of(str(root)) == os.path.join(str(root), ".runs")


# ── ⑦ prune 命令:两道闸门 ──────────────────────────────────────────

def test_prune_command_lists_only_by_default(tmp_path, capsys):
    """`curation prune <交付>` 默认只把事实摆出来(时间/条数/占用),一个都不删。"""
    from curation.cli import main
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    assert main(["prune", str(deliv)]) == 0
    out = capsys.readouterr().out
    assert "20260814-053000" in out and "本次处理 200 条" in out
    assert "只列不删" in out
    assert len(list_runs(str(deliv))) == 2


def test_prune_command_needs_both_gates(tmp_path, capsys):
    """要真删得过两道闸门:说清删哪几次(--keep-latest N)+ 明确点头(--yes)。
    光有 --yes 就是"没说清就动手",直接拒绝。"""
    from curation.cli import main
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    assert main(["prune", str(deliv), "--yes"]) == 2
    assert main(["prune", str(deliv), "--keep-latest", "1"]) == 0    # 只预演
    assert len(list_runs(str(deliv))) == 2
    assert "确认无误" in capsys.readouterr().out
    assert main(["prune", str(deliv), "--keep-latest", "1", "--yes"]) == 0
    assert list_runs(str(deliv)) == ["20260814-181200"]


def test_prune_command_keeps_human_decisions(tmp_path):
    """删跑批不碰人的产出:human-decisions/ 原封不动(这次改造的全部意义所在)。"""
    from curation.cli import main
    deliv = tmp_path / "d"
    _make_run(deliv, "20260814-053000", processed=200)
    _make_run(deliv, "20260814-181200", processed=20)
    (deliv / "human-decisions").mkdir()
    csv = deliv / "human-decisions" / "task_verdicts.csv"
    csv.write_text("episode_id,verdict,note,at\nep000007,判成功,,t\n", encoding="utf-8")
    assert main(["prune", str(deliv), "--keep-latest", "1", "--yes"]) == 0
    assert "判成功" in csv.read_text(encoding="utf-8")
