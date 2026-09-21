"""RRD 输入在**管线周边命令**上的完整性(P5,2026-08-10)。

P1-P4 把 RRD 接进了 run(读)与交付(写),但周边三条路还只认 LeRobot:
① rejudge 的**重判**(采纳改标 → 用新标注重跑成败判定)—— 最要命的一条,
   断了的话 RRD 客户的人工裁决只能同步交付集,新标注永远没被机器复核过;
② `--batch` 的数据集清单;③ `review-page` 审片站。

本文件按"这三条路对 RRD 真的走通了"来钉,而不是只钉分派函数选对了谁:
重判用**生产重判器 `_build_rerun`**(只把 VLM 换成假的),所以回源重读、解码、
把新标注喂给判定这一整条链都是真跑的 —— 断在哪一环都会红。

合成数据复用 test_rrd_reader 的写盘工具(同一份约定,不另起炉灶)。
"""
from __future__ import annotations

import json
import os
import types

import pytest

pytest.importorskip("rerun", reason="未装 rerun-sdk(RRD 是可选格式)")

from curation.export.rrd_writer import export_rrd_curated  # noqa: E402
from curation.ingest.rrd_reader import read_rrd_rows  # noqa: E402
from curation.tests.test_rrd_reader import _write_rrd  # noqa: E402

RRD_SO101 = "/mnt/tos/datasets/rrd/so101-pick-place"

TASKS = {0: "put spoon in tray", 1: "open the drawer"}
NEW_LABEL = "把蓝色积木放进托盘"

#: 重判器要的最小配置(VLM 那两格在测试里被换成假的,但键必须在——生产读的就是它们)
CFG = {"pipeline": {}, "ingest": {"rrd_fps": None},
       "checks": {"task_success": {"params": {},
                                   "vlm": {"endpoint": "http://fake/v1",
                                           "model": "fake-vlm"}}}}


@pytest.fixture(autouse=True, scope="module")
def _clean_video_cache():
    """本模块跑完清掉 reader 落的临时 mp4(与另两个 rrd 测试同一道纪律)。"""
    yield
    from curation.ingest.rrd_reader import cleanup_video_cache
    cleanup_video_cache()


@pytest.fixture()
def src_dir(tmp_path):
    """两条 episode 的合成 RRD 源数据集(模式B,自带帧时间戳 ⇒ fps 不用给)。"""
    d = tmp_path / "src"
    d.mkdir()
    for i in TASKS:
        _write_rrd(str(d / f"episode_{i}.rrd"), n=8, fps=5.0, task=TASKS[i])
    return str(d)


def _fake_vlm(monkeypatch) -> dict:
    """把重判器里的两个 VLM 构件换成假的,其余(重读/解码/编排)照常真跑。

    返回一个记事本 dict:判定函数收到的标注与画面都记在里面,供断言"重判真的
    用新标注跑过一遍",而不是"函数被调用过"。
    """
    import curation.adapters.vlm_client as vc
    import curation.core.checks.task_success as ts

    seen: dict = {}

    def fake_task_success(multiview, instruction, vlm, **kw):
        seen["instruction"] = instruction
        seen["n_frames"] = len(multiview)
        seen["cams"] = [n for n, _ in multiview[0]] if multiview else []
        return types.SimpleNamespace(passed=True, detail={"verdict": "success"})

    monkeypatch.setattr(vc, "vlm_completion_from_config", lambda cfg: object())
    monkeypatch.setattr(vc, "make_endstate_voter", lambda *a, **kw: object())
    monkeypatch.setattr(ts, "task_success", fake_task_success)
    monkeypatch.setattr(ts, "endstate_review", lambda res, *a, **kw: res)
    return seen


def _fake_delivery(delivery: str, src: str, adopt_eid: str, new_label: str) -> None:
    """一份最小但完整的交付 + 一条"采纳改标"裁决(三件套 + parquet + rrd_curated)。"""
    daft = pytest.importorskip("daft")
    det = os.path.join(delivery, "details")
    os.makedirs(det)
    with open(os.path.join(det, "label_decisions.csv"), "w", encoding="utf-8") as f:
        f.write("episode_id,decision,new_label,note,at\n"
                f"{adopt_eid},采纳建议改标,{new_label},,2026-08-10 13:00:00\n")
    for name in ("passed", "review", "reject"):
        payload = ({"episodes": {f"ep{i:06d}": {"判决": "通过", "综合软分": 0.8}
                                 for i in TASKS}} if name == "passed"
                   else {"episodes": {}})
        with open(os.path.join(delivery, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    daft.from_pydict({
        "episode_id": [f"ep{i:06d}" for i in TASKS],
        "instruction": [TASKS[i] for i in TASKS],
        "instruction_source": ["原始标注"] * len(TASKS),
    }).write_parquet(os.path.join(delivery, "episodes_parquet"))
    export_rrd_curated(delivery, src, [f"ep{i:06d}" for i in TASKS], generated_at="t0")


# ---------------------------------------------------------------------------
# ① rejudge 的重判(RRD 输入)
# ---------------------------------------------------------------------------

def test_rejudge_rereads_rrd_and_rejudges_with_new_label(src_dir, tmp_path, monkeypatch):
    """RRD 输入走**生产重判器**:回源重读 .rrd → 解码 → 用**新标注**重判 → 落交付。

    最关键的一条断言是 `seen["instruction"] == NEW_LABEL`:它证明重判不是走过场,
    而是真的拿人工确认过的新标注问了一遍(P4 之前这里会因为写死 read_lerobot_rows
    抛 NotADatasetError,整条 episode 被"重判失败,原样不动"吞掉)。
    """
    from curation.pipeline.rejudge import _build_rerun, run_rejudge

    seen = _fake_vlm(monkeypatch)
    delivery = str(tmp_path / "dlv")
    _fake_delivery(delivery, src_dir, "ep000000", NEW_LABEL)

    summary = run_rejudge(delivery, src_dir, CFG, rerun_fn=_build_rerun(CFG))

    # 重判真的执行了,且吃的是新标注 + 真解出来的画面
    assert seen["instruction"] == NEW_LABEL
    assert seen["n_frames"] >= 1 and seen["cams"] == ["cam0"]
    assert summary["adopted_pass"] == ["ep000000"] and not summary["skipped"]

    # 判定结果落进三件套(带重判溯源)
    with open(os.path.join(delivery, "passed.json"), encoding="utf-8") as f:
        entry = json.load(f)["episodes"]["ep000000"]
    assert entry["判决"] == "通过(标注修正后)"
    assert entry["标注修正"]["新标注"] == NEW_LABEL
    assert entry["标注修正"]["重判判定"] == "success"
    assert entry["checks"]["任务成败判定"]["结果"] == "pass"

    # 交付集同步:rrd_curated 里该条的 /task 已是新标注,另一条原样
    rows = {r["episode_id"]: r for r in
            read_rrd_rows(os.path.join(delivery, "rrd_curated"))}
    assert rows["ep000000"]["instruction"] == NEW_LABEL
    assert rows["ep000001"]["instruction"] == TASKS[1]


def test_rejudge_cleans_rrd_video_cache(src_dir, tmp_path, monkeypatch):
    """收尾清理:rejudge 为重判解出的临时 mp4 目录跑完必须消失(容器可写层会被撑满)。"""
    from curation.ingest import rrd_reader as R
    from curation.pipeline.rejudge import _build_rerun, run_rejudge

    _fake_vlm(monkeypatch)
    delivery = str(tmp_path / "dlv")
    _fake_delivery(delivery, src_dir, "ep000000", NEW_LABEL)
    R.cleanup_video_cache(src_dir)                     # 从干净状态起算

    run_rejudge(delivery, src_dir, CFG, rerun_fn=_build_rerun(CFG))
    assert os.path.abspath(src_dir) not in R._VIDEO_DIRS


def test_missing_fps_error_points_at_config(tmp_path):
    """RRD 无时间信息 + 配置没给 fps → 报错要说清"在 rejudge 的 --config 里给"。

    reader 自己给的出路是 run 的 `--set`,rejudge 没有这个参数;照抄跑不通的命令
    比不报错更耗人。
    """
    from curation.ingest.lerobot_reader import NotADatasetError
    from curation.pipeline.rejudge import _episode_row_reader

    d = tmp_path / "notime"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), n=8, fps=5.0, with_frame_ts=False)

    read = _episode_row_reader(str(d), {})
    with pytest.raises(NotADatasetError) as e:
        read(str(d), episode_indices={0})
    assert "ingest.rrd_fps" in str(e.value)

    # 配置里给了就能读(与原 run 同一个键)
    rows = _episode_row_reader(str(d), {"ingest": {"rrd_fps": 10.0}})(
        str(d), episode_indices={0})
    assert rows[0]["fps"] == 10.0


def test_lerobot_input_still_uses_lerobot_reader(tmp_path):
    """零回归:非 RRD 目录仍原样交给 read_lerobot_rows(嗅探不许改变老路径)。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows
    from curation.pipeline.rejudge import _episode_row_reader

    assert _episode_row_reader(str(tmp_path), CFG) is read_lerobot_rows


# ---------------------------------------------------------------------------
# ② --batch 的数据集清单
# ---------------------------------------------------------------------------

def test_list_datasets_counts_rrd_dirs(tmp_path):
    """父目录下 LeRobot 集与 RRD 集都要被 --batch 认出来(非数据集目录不许混进来)。"""
    from curation.cli import _list_datasets

    (tmp_path / "lerobot_ds" / "meta").mkdir(parents=True)
    (tmp_path / "lerobot_ds" / "meta" / "info.json").write_text("{}", encoding="utf-8")
    (tmp_path / "rrd_ds").mkdir()
    (tmp_path / "rrd_ds" / "episode_0.rrd").write_bytes(b"")
    (tmp_path / "not_a_dataset").mkdir()
    (tmp_path / "not_a_dataset" / "readme.txt").write_text("x", encoding="utf-8")

    assert _list_datasets(str(tmp_path)) == ["lerobot_ds", "rrd_ds"]


# ---------------------------------------------------------------------------
# ③ review-page 审片站
# ---------------------------------------------------------------------------

def test_review_page_from_rrd(src_dir, tmp_path):
    """RRD 数据集 → 静态审片站:索引页 + 逐条页 + 真切片,全部产出。"""
    from curation.cli import main

    out = str(tmp_path / "site")
    assert main(["review-page", "--input", src_dir, "--output", out,
                 "--title", "RRD 审片"]) == 0

    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "RRD 审片" in index and "2 条" in index
    for i in TASKS:
        eid = f"ep{i:06d}"
        assert f'href="ep/{eid}.html"' in index
        page = (tmp_path / "site" / "ep" / f"{eid}.html").read_text(encoding="utf-8")
        assert TASKS[i] in page                        # 标注从 .rrd 的 /task 来
        clip = tmp_path / "site" / "details" / "audit_clips" / f"{eid}__cam0.mp4"
        assert clip.exists() and clip.stat().st_size > 0


def test_review_page_rrd_fps_flag(tmp_path, capsys):
    """无时间戳的 RRD:不给 --rrd-fps 就响亮失败(且给的是**本命令**的写法),给了就出站。"""
    from curation.cli import main

    d = tmp_path / "notime"
    d.mkdir()
    _write_rrd(str(d / "episode_0.rrd"), n=8, fps=5.0, with_frame_ts=False)
    out = str(tmp_path / "site")

    assert main(["review-page", "--input", str(d), "--output", out]) == 2
    err = capsys.readouterr().err
    assert "--rrd-fps" in err and "没有任何时间信息" in err

    assert main(["review-page", "--input", str(d), "--output", out,
                 "--rrd-fps", "10"]) == 0
    assert (tmp_path / "site" / "index.html").exists()


# ---------------------------------------------------------------------------
# integration:真数据(so101 = VideoStream 重封装那一路,合成数据造不出来)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.isdir(RRD_SO101), reason="无 so101 RRD 数据")
def test_real_so101_rejudge_rereads(tmp_path, monkeypatch):
    """真 so101 交付上造一条改标裁决:重判回源重读 .rrd、两路相机都解出画面。"""
    from curation.pipeline.rejudge import _build_rerun, run_rejudge

    seen = _fake_vlm(monkeypatch)
    src = str(tmp_path / "src")
    os.makedirs(src)
    # 只取两条(重判只碰被点名的那条,交付集里第二条用来验"没点名的不许被动")
    for i in TASKS:
        os.symlink(os.path.join(RRD_SO101, f"episode_{i}.rrd"),
                   os.path.join(src, f"episode_{i}.rrd"))
    delivery = str(tmp_path / "dlv")
    _fake_delivery(delivery, src, "ep000000", NEW_LABEL)

    cfg = json.loads(json.dumps(CFG))
    cfg["ingest"]["rrd_fps"] = 30.0                    # so101 无时间信息,与原 run 一致
    summary = run_rejudge(delivery, src, cfg, rerun_fn=_build_rerun(cfg))

    assert summary["adopted_pass"] == ["ep000000"] and not summary["skipped"]
    assert seen["instruction"] == NEW_LABEL
    assert seen["n_frames"] > 1
    assert sorted(seen["cams"]) == ["front", "wrist"]  # 两路都重封装并解码成功
