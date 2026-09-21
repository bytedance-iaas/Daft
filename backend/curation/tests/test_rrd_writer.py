"""RRD 交付出口验收(P3,2026-08-10)。

交付集 `rrd_curated/` 的四条硬要求,逐条钉住:
① 通过且未改标 = **与源逐字节一致**(md5);② 剔除的条目一个都不许出现;
③ 改标条目 /task 换成新文本、**其余数据(action)一个数不动**;④ index.json 齐全。
外加两条工程纪律:交付路径上只许顺序整写(FSX 挂载拒绝随机写),以及临时视频
缓存收尾能清干净。

合成数据复用 test_rrd_reader 的写盘工具(同一份约定,不另起炉灶)。
真数据 integration(/tmp/ep0.rrd = so101,/task 是 rows=0 的空 chunk)验证
"原文件没有任务文本也能写进去"这条最容易踩空的路径。
"""
from __future__ import annotations

import hashlib
import json
import os

import numpy as np
import pytest

pytest.importorskip("rerun", reason="未装 rerun-sdk(RRD 是可选格式)")

from curation.export.rrd_writer import export_rrd_curated  # noqa: E402
from curation.ingest.rrd_reader import read_rrd_rows  # noqa: E402
from curation.tests.test_rrd_reader import _write_rrd  # noqa: E402

REAL_EP0 = "/tmp/ep0.rrd"          # pod 上现成的 so101 单条样本(P1 留下的)

TASKS = {0: "put spoon in tray", 1: "open the drawer", 2: "wipe the table"}


@pytest.fixture(autouse=True, scope="module")
def _clean_video_cache():
    """本模块跑完清掉 reader 落的临时 mp4。不清的话每跑一次 pytest 就在 /tmp 留下
    一批目录(2026-08-10 pod 上实测积到 2GB),正是 P4 要堵的那个洞。"""
    yield
    from curation.ingest.rrd_reader import cleanup_video_cache
    cleanup_video_cache()


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


@pytest.fixture()
def src_dir(tmp_path):
    """三条 episode 的合成 RRD 源数据集(任务文本各不相同,便于分辨串条)。"""
    d = tmp_path / "src"
    d.mkdir()
    acts = {i: _write_rrd(str(d / f"episode_{i}.rrd"), n=8, fps=5.0, task=TASKS[i])
            for i in TASKS}
    return str(d), acts


def _index(delivery: str) -> dict:
    with open(os.path.join(delivery, "rrd_curated", "index.json"), encoding="utf-8") as f:
        return json.load(f)


def test_kept_are_byte_identical_and_dropped_absent(src_dir, tmp_path):
    """通过条目逐字节等于源(md5),被剔除的条目不出现,文件名保留源编号。"""
    src, _ = src_dir
    delivery = str(tmp_path / "deliver")
    stats = export_rrd_curated(delivery, src, ["ep000000", "ep000002"],
                               generated_at="2026-08-10 12:00:00")
    out = os.path.join(delivery, "rrd_curated")
    assert stats["out_dir"] == out
    files = sorted(f for f in os.listdir(out) if f.endswith(".rrd"))
    assert files == ["episode_0.rrd", "episode_2.rrd"]     # 保号,不重编号
    assert not os.path.exists(os.path.join(out, "episode_1.rrd"))
    for i in (0, 2):
        assert _md5(os.path.join(out, f"episode_{i}.rrd")) == \
            _md5(os.path.join(src, f"episode_{i}.rrd"))
    assert (stats["episodes"], stats["relabeled"], stats["removed"]) == (2, 0, 1)


def test_relabel_rewrites_task_only(src_dir, tmp_path):
    """改标条目:重读后 /task = 新文本,action/state 数值一个不差;同批未改标的仍等于源。"""
    src, acts = src_dir
    delivery = str(tmp_path / "deliver")
    new = "pick up the blue block and place it in the bin"
    export_rrd_curated(delivery, src, ["ep000000", "ep000001"],
                       relabels={"ep000001": new}, generated_at="t")
    out = os.path.join(delivery, "rrd_curated")

    rows = {r["episode_id"]: r for r in read_rrd_rows(out)}
    assert set(rows) == {"ep000000", "ep000001"}
    assert rows["ep000001"]["instruction"] == new
    assert rows["ep000000"]["instruction"] == TASKS[0]      # 没点名的不许被动到
    for eid, i in (("ep000000", 0), ("ep000001", 1)):
        assert np.allclose(rows[eid]["action"], acts[i], atol=1e-6)
        assert np.allclose(rows[eid]["proprio_state"], acts[i] + 0.5, atol=1e-6)
        assert len(rows[eid]["video"]) == 1                 # 视频 chunk 原样转交
        assert rows[eid]["fps"] == pytest.approx(5.0)       # 时间戳 chunk 也没丢
    # 未改标的那条仍与源逐字节一致(重写只发生在被点名的条目上)
    assert _md5(os.path.join(out, "episode_0.rrd")) == \
        _md5(os.path.join(src, "episode_0.rrd"))
    assert _md5(os.path.join(out, "episode_1.rrd")) != \
        _md5(os.path.join(src, "episode_1.rrd"))


def test_index_json_complete(src_dir, tmp_path):
    """清单每条要能独立回答:哪条、哪个文件、什么判决、什么标注、标注哪来的、改没改。"""
    src, _ = src_dir
    delivery = str(tmp_path / "deliver")
    export_rrd_curated(
        delivery, src, ["ep000000", "ep000002"],
        relabels={"ep000002": "new text"},
        episodes={"ep000000": {"verdict": "通过", "instruction": TASKS[0],
                               "instruction_source": "原始标注"},
                  "ep000002": {"verdict": "通过", "instruction": TASKS[2],
                               "instruction_source": "自产caption补标"}},
        generated_at="2026-08-10 12:00:00")
    idx = _index(delivery)
    assert idx["source_format"] == "rrd"
    assert (idx["n_kept"], idx["n_removed"]) == (2, 1)
    assert idx["generated_at"] == "2026-08-10 12:00:00"     # 时间由调用方注入
    e0, e2 = idx["episodes"]
    assert e0 == {"episode_id": "ep000000", "file": "episode_0.rrd",
                  "verdict": "通过", "instruction": TASKS[0],
                  "instruction_source": "原始标注", "relabeled": False}
    assert e2["relabeled"] is True and e2["instruction"] == "new text"
    assert e2["instruction_source"] == "自产caption补标"


def test_unknown_episode_and_nonempty_dir_refused(src_dir, tmp_path):
    """源里没有的条目 → 响亮报错;输出目录非空 → 拒绝(与 LeRobot 导出同一纪律)。"""
    src, _ = src_dir
    delivery = str(tmp_path / "deliver")
    with pytest.raises(KeyError):
        export_rrd_curated(delivery, src, ["ep000009"])
    export_rrd_curated(delivery, src, ["ep000000"])
    with pytest.raises(FileExistsError):
        export_rrd_curated(delivery, src, ["ep000001"])


def test_delivery_path_is_sequential_write_only(src_dir, tmp_path, monkeypatch):
    """FSX 安全:交付路径上不许出现追加/随机写,也不许让 rerun 直接对着交付目录写。

    TOS 的 FSX 挂载拒绝随机写(EINVAL),而 write_rrd 的写序在库内部、不受我们控制 ——
    所以必须"先写本地临时文件,再 copyfile 顺序整拷"。这里同时钉住两件事:
    ① 交付目录下的 open 只出现顺序写模式;② write_rrd 的落点不在交付目录内。
    """
    import builtins

    import rerun.experimental as rex

    src, _ = src_dir
    delivery = str(tmp_path / "deliver")

    opened: list = []
    real_open = builtins.open

    def spy_open(path, mode="r", *a, **kw):
        try:
            if str(path).startswith(delivery):
                opened.append((str(path), mode))
        except Exception:  # noqa: BLE001  非路径参数(fd)照常放行
            pass
        return real_open(path, mode, *a, **kw)

    written: list = []
    real_stream = rex.LazyChunkStream

    class _StreamSpy:
        @staticmethod
        def from_iter(chunks):
            return _Wrapped(real_stream.from_iter(chunks))

    class _Wrapped:
        def __init__(self, inner):
            self._inner = inner

        def write_rrd(self, path, **kw):
            written.append(str(path))
            return self._inner.write_rrd(path, **kw)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(rex, "LazyChunkStream", _StreamSpy)
    export_rrd_curated(delivery, src, ["ep000000", "ep000001"],
                       relabels={"ep000001": "brand new task"})
    monkeypatch.undo()

    assert written, "改标条目应经由 write_rrd 落到本地临时文件"
    for p in written:
        assert not p.startswith(delivery), f"rerun 直接写了交付路径(FSX 会 EINVAL):{p}"
    for path, mode in opened:
        assert "a" not in mode and "+" not in mode, f"交付路径上出现非顺序写:{path} {mode}"


def test_cleanup_video_cache(src_dir):
    """收尾清理:临时视频目录整个消失,且幂等(重复清/没读过的目录都不报错)。"""
    from curation.ingest import rrd_reader as R

    src, _ = src_dir
    row = read_rrd_rows(src, max_episodes=1)[0]
    vdir = os.path.dirname(row["video"]["observation.images.cam0"]["path"])
    assert os.path.isdir(vdir)
    assert R.cleanup_video_cache(src) == 1
    assert not os.path.exists(vdir)
    assert R.cleanup_video_cache(src) == 0          # 幂等:清过了不再报错
    assert R.cleanup_video_cache("/no/such/dataset") == 0


def test_rejudge_resyncs_rrd_curated(src_dir, tmp_path):
    """人工裁决后交付集自动同步:弃用条目从 rrd_curated 消失,改标条目 /task 换新。

    与 lerobot_curated 的 v2 重导出同一形态(rejudge 里紧挨着的那段)。
    本用例只管**交付集同步**,故注入假 rerun_fn;重判本体对 RRD 的走通见
    test_rrd_pipeline.py(P5 把重判器做成了格式无关的)。
    """
    daft = pytest.importorskip("daft")
    from curation.pipeline.rejudge import run_rejudge

    src, _ = src_dir
    delivery = tmp_path / "dlv"
    det = delivery / "details"
    det.mkdir(parents=True)
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000000,采纳建议改标,corrected task text,,2026-08-10 13:00:00\n"
        "ep000001,弃用该条,,,2026-08-10 13:00:05\n", encoding="utf-8")
    for n in ("passed", "review", "reject"):
        (delivery / f"{n}.json").write_text(json.dumps(
            {"episodes": {f"ep{i:06d}": {"判决": "通过"} for i in range(3)}}
            if n == "passed" else {"episodes": {}}, ensure_ascii=False),
            encoding="utf-8")
    daft.from_pydict({
        "episode_id": [f"ep{i:06d}" for i in range(3)],
        "instruction": [TASKS[i] for i in range(3)],
        "instruction_source": ["原始标注"] * 3,
    }).write_parquet(str(delivery / "episodes_parquet"))
    export_rrd_curated(str(delivery), src, [f"ep{i:06d}" for i in range(3)],
                       generated_at="t0")

    run_rejudge(str(delivery), src, {},
                rerun_fn=lambda i, e, nl: {"passed": True, "verdict": "success"})

    out = delivery / "rrd_curated"
    assert not (out / "episode_1.rrd").exists(), "弃用条目仍在 RRD 交付集里"
    rows = {r["episode_id"]: r for r in read_rrd_rows(str(out))}
    assert set(rows) == {"ep000000", "ep000002"}
    assert rows["ep000000"]["instruction"] == "corrected task text"
    assert rows["ep000002"]["instruction"] == TASKS[2]        # 没动的条目原样
    idx = _index(str(delivery))
    assert idx["n_kept"] == 2
    assert [e["relabeled"] for e in idx["episodes"]] == [True, False]


# ---------------------------------------------------------------------------
# integration:真数据(so101 的 /task 是 rows=0 的空 chunk —— 最容易写不进去的一路)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(REAL_EP0), reason="无 so101 单条 rrd 样本")
def test_real_episode_relabel_roundtrip(tmp_path):
    """真 so101 episode:原文件无任务文本,改标后重读 /task = 新文本、action 一个数不差。"""
    from curation.ingest.rrd_reader import DEFAULT_MAPPING, _scan_rrd

    src = tmp_path / "src"
    src.mkdir()
    os.symlink(REAL_EP0, str(src / "episode_0.rrd"))   # 不拷 50MB,导出照样顺序读
    delivery = str(tmp_path / "deliver")
    new = "用夹爪把勺子放进托盘"

    before = _scan_rrd(REAL_EP0, DEFAULT_MAPPING)
    assert not [t for t in before["task"] if t], "前提:so101 源的 /task 本来就是空的"

    export_rrd_curated(delivery, str(src), ["ep000000"],
                       relabels={"ep000000": new}, generated_at="t")
    out_rrd = os.path.join(delivery, "rrd_curated", "episode_0.rrd")
    after = _scan_rrd(out_rrd, DEFAULT_MAPPING)

    texts = [(t[0] if isinstance(t, (list, tuple)) else t) for t in after["task"]]
    assert new in [str(x) for x in texts]
    # action 逐帧逐值一致(改标只动 /task,数值 chunk 是原对象转交)
    assert sorted(after["action"]) == sorted(before["action"])
    assert all(after["action"][k] == before["action"][k] for k in before["action"])
    assert after["action_names"] == before["action_names"]
    assert set(after["video"]) == set(before["video"])
