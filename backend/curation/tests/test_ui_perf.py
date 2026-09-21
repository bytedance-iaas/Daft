"""公网首屏的两道送分题(2026-08-20 APIG 实测):交付扫描缓存 + gzip/immutable。

实测账本(公网网关):首屏 ≈ 9 s,其中 SSE 首批回调 5.5 s = discover_deliveries
被调两遍 × 2.5 s(55 份交付在 FSX 上几百次 stat);HTML 532 KB + config 259 KB
未压缩穿网关;139 个 JS 分片没有缓存头。
"""
from __future__ import annotations

import json
import os

import pytest

from curation.ui import manifest as M


def _new_delivery(root, name):
    d = root / name / "20260820-000001"
    d.mkdir(parents=True)
    (d / "passed.json").write_text(json.dumps({"数据集": name, "episodes": {}}),
                                   encoding="utf-8")
    return str(root / name)


def test_discover_is_cached_within_ttl_but_sees_new_toplevel_immediately(
        tmp_path, monkeypatch):
    """同根 5 秒内不重扫;顶层新交付一落盘(根 mtime 变)立刻可见。"""
    root = tmp_path / "deliv"
    root.mkdir()
    a = _new_delivery(root, "a")
    calls = []
    real = os.scandir

    def spy(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    M.clear_discover_cache()
    assert M.discover_deliveries(str(root)) == [a]
    n1 = len(calls)
    assert n1 > 0
    assert M.discover_deliveries(str(root)) == [a]
    assert len(calls) == n1, "TTL 内第二次调用又扫盘了——缓存没生效"
    # 顶层新交付:根目录 mtime 变 → 缓存失效 → 立刻看见(不等 TTL)
    import time
    time.sleep(0.02)
    b = _new_delivery(root, "b")
    os.utime(str(root), None)
    assert M.discover_deliveries(str(root)) == [a, b]
    assert len(calls) > n1


def test_discover_cache_is_keyed_by_root_and_clearable(tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    r1.mkdir(); r2.mkdir()
    a = _new_delivery(r1, "a")
    b = _new_delivery(r2, "b")
    M.clear_discover_cache()
    assert M.discover_deliveries(str(r1)) == [a]
    assert M.discover_deliveries(str(r2)) == [b]      # 不同根不串
    M.clear_discover_cache()
    assert M.discover_deliveries(str(r1)) == [a]


def test_discover_semantics_unchanged_legacy_nested_and_incomplete(tmp_path):
    """提速不改判据:老布局(passed.json 直接在目录里)/嵌套三层/只有不完整跑批
    (没有 passed.json)的目录不算交付/找到交付不往里钻。"""
    root = tmp_path / "deliv"
    legacy = root / "old"
    legacy.mkdir(parents=True)
    (legacy / "passed.json").write_text("{}", encoding="utf-8")
    nested = _new_delivery(root / "exp" / "grp", "n1")
    incomplete = root / "broken" / "20260820-000002"
    incomplete.mkdir(parents=True)                 # 跑批目录但没有 passed.json
    (root / "n1_inner_should_not_be_scanned").mkdir()
    M.clear_discover_cache()
    found = M.discover_deliveries(str(root))
    assert str(legacy) in found and nested in found
    assert str(root / "broken") not in found
    # 交付内部不再往里钻:跑批子目录本身不会被当成交付
    assert not any(f.endswith("20260820-000001") for f in found)


# ── ASGI 层:gzip + 静态资产 immutable ─────────────────────────────────────


# ── 空交付根自举(2026-08-20 同事全新部署撞上的启动即退)────────────────────


# ── 部署感知默认值(2026-08-20,同事纯直连部署:/data/deliveries 无挂载)────────

def test_mount_backed_requires_under_mount_root_and_existing(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    (tmp_path / "mnt" / "deliveries").mkdir(parents=True)
    assert runner.is_mount_backed(str(tmp_path / "mnt" / "deliveries"))
    assert not runner.is_mount_backed(str(tmp_path / "mnt" / "missing"))   # 不在
    (tmp_path / "data" / "deliveries").mkdir(parents=True)
    assert not runner.is_mount_backed(str(tmp_path / "data" / "deliveries"))  # 不在挂载下


def test_home_output_url_goes_direct_when_not_mounted(tmp_path, monkeypatch):
    """没挂载 + 有 TOS_BUCKET → 默认输出是桶里的直连地址,绝不把本地盘伪装成桶。"""
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    local = str(tmp_path / "data" / "deliveries")
    assert runner.home_output_url(local) == "tos://herbucket/deliveries"
    spec = runner.resolve_output_input("tos://herbucket/deliveries", local)
    assert spec["kind"] == "tos", "没挂载时默认地址必须走直连 stage_out,不许按挂载直写本地盘"
    # 挂载承载的实例行为不变
    mounted = tmp_path / "mnt" / "deliveries"
    mounted.mkdir(parents=True)
    assert runner.home_output_url(str(mounted)) == "tos://herbucket/deliveries"
    assert runner.resolve_output_input("tos://herbucket/deliveries", str(mounted))["kind"] == "mount"
    # 用户明填本地交付根仍放行(运营配置的路径,不是自由路径)
    assert runner.resolve_output_input(local, local)["kind"] == "mount"


def test_home_output_url_without_bucket_falls_back_to_path(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    local = str(tmp_path / "data" / "deliveries")
    assert runner.home_output_url(local) == local


def test_bucket_url_goes_direct_when_synthesized_root_missing(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    synth = {"name": "默认", "bucket": None, "tos_prefix": None,
             "datasets_path": str(tmp_path / "nope")}
    assert runner.bucket_url(synth) == "tos://herbucket/datasets"
    (tmp_path / "yes").mkdir()
    synth["datasets_path"] = str(tmp_path / "yes")
    assert runner.bucket_url(synth) == str(tmp_path / "yes")     # 目录在:原样白名单
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    synth["datasets_path"] = str(tmp_path / "nope")
    assert runner.bucket_url(synth) == ""    # 没桶+目录不在:留空(rerun 侧裸进入,2026-08-26)
    synth["datasets_path"] = "tos://mybkt/ds"
    assert runner.bucket_url(synth) == "tos://mybkt/ds"          # 显式 tos:// 原样保留


def test_deployment_shape_note(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    for d in ("deliveries", "datasets"):
        (tmp_path / "mnt" / d).mkdir(parents=True)
    monkeypatch.setenv("TOS_BUCKET", "b")
    assert runner.deployment_shape_note(str(tmp_path / "mnt" / "deliveries"),
                                        str(tmp_path / "mnt" / "datasets")) == ""
    note = runner.deployment_shape_note(str(tmp_path / "data" / "deliveries"),
                                        str(tmp_path / "mnt" / "datasets"))
    assert "未挂载" in note and "tos://b/" in note
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    assert "TOS_BUCKET" in runner.deployment_shape_note(str(tmp_path / "x"), str(tmp_path / "y"))


def test_last_output_default_prefers_latest_run(tmp_path, monkeypatch):
    """跑质检页交付目录的记忆默认(2026-09-01 用户定):最近一次跑批写到哪,
    重开页面就默认哪;挂载输出规范化 tos://;没跑过 → None(站点默认兜底)。"""
    from curation.ui import runner
    monkeypatch.setattr(runner, "latest_run_delivery",
                        lambda rr: {"run_id": "r",
                                    "output": "tos://bkt/deliveries/x",
                                    "region": "cn-shanghai"})
    assert runner.last_output_default("rr") == ("tos://bkt/deliveries",
                                                "cn-shanghai")
    mnt = tmp_path / "mnt"
    (mnt / "deliveries" / "x").mkdir(parents=True)
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(mnt))
    monkeypatch.setenv("TOS_BUCKET", "homebkt")
    monkeypatch.setenv("TOS_REGION", "cn-beijing")
    monkeypatch.setattr(runner, "latest_run_delivery",
                        lambda rr: {"run_id": "r",
                                    "output": str(mnt / "deliveries" / "x"),
                                    "region": ""})
    assert runner.last_output_default("rr") == ("tos://homebkt/deliveries",
                                                "cn-beijing")
    monkeypatch.setattr(runner, "latest_run_delivery", lambda rr: None)
    assert runner.last_output_default("rr") is None


def test_tos_list_deliveries_hides_dot_dirs():
    from curation.ui import runner

    class _S:
        def iter_common_prefixes(self, b, p):
            yield from [".runs", ".probe_details", "aloha-10", "debug"]
    assert runner.tos_list_deliveries("tos://bkt/deliveries", store=_S()) == ["aloha-10", "debug"]


def test_data_sig_tracks_file_changes(tmp_path):
    """数据指纹:关键文件一动就变,不动就稳定;缺文件按 (0,0) 记,从无到有也算变化。"""
    from curation.ui.manifest import data_sig
    run = tmp_path / "d" / "20260825-000000"
    run.mkdir(parents=True)
    (run / "passed.json").write_text("{}")
    s1 = data_sig(str(run), str(tmp_path / "d"))
    assert s1 == data_sig(str(run), str(tmp_path / "d"))     # 稳定
    import os
    os.utime(run / "passed.json", ns=(1, 1))
    s2 = data_sig(str(run), str(tmp_path / "d"))
    assert s2 != s1                                          # mtime 变 → 指纹变
    hd = tmp_path / "d" / "human-decisions"
    hd.mkdir()
    (hd / "task_verdicts.csv").write_text("episode_id\n")
    assert data_sig(str(run), str(tmp_path / "d")) != s2     # 裁决 CSV 出现 → 变


def _mk_delivery(root, name, run, marker_files=("passed.json",)):
    d = root / name / run
    d.mkdir(parents=True)
    for f in marker_files:
        (d / f).write_text('{"episodes": {}}')
    return root / name


def test_most_recent_delivery_wins_by_latest_run(tmp_path):
    """复盘 ⑥:报告页默认选**最近跑过**的交付,不是字母序第一个。"""
    from curation.ui.manifest import most_recent_delivery
    root = tmp_path
    _mk_delivery(root, "aloha-10", "20260701-000000")
    newest = _mk_delivery(root, "zz-later-name-but-old", "20260702-000000")
    newest2 = _mk_delivery(root, "droid-50-guide", "20260825-015215")
    got = most_recent_delivery(str(root), [str(root / n) for n in
                                           ("aloha-10", "droid-50-guide",
                                            "zz-later-name-but-old")])
    assert got == str(newest2)
    del newest


def test_bucket_mapped_source_fallback(tmp_path, monkeypatch):
    """源数据集路径跨机回退(2026-08-27 rerun 侧实报):挂载实例产的交付记
    /mnt/tos/... 本地路径,直连实例打开时按 .tos-origin.json 映射回桶,
    验证 meta/info.json 存在才用;验不过 / 没 origin → 维持"找不到"。"""
    import json as _json

    from curation.ui import runner

    droot = tmp_path / "cache" / "droid-50"
    run = droot / "20260825-015215"
    run.mkdir(parents=True)
    (run / "passed.json").write_text(_json.dumps(
        {"episodes": {}, "源数据集路径": "/mnt/tos/datasets/no-such-ds-portability-test"}),
        encoding="utf-8")
    (droot / "latest").write_text("20260825-015215", encoding="utf-8")
    monkeypatch.setenv("CURATION_TOS_MOUNT", "/mnt/tos")

    # 没 origin:找不到(不猜)
    assert runner.source_dataset_of(str(droot)) is None

    (droot / runner.TOS_ORIGIN_NAME).write_text(_json.dumps(
        {"delivery_url": "tos://curation/deliveries/droid-50-guide",
         "run": "20260825-015215", "region": "cn-beijing"}), encoding="utf-8")
    from curation.ingest import dsfs
    monkeypatch.setattr(dsfs, "exists", lambda p: "no-such-ds-portability-test" in str(p))
    assert (runner.source_dataset_of(str(droot))
            == "tos://curation/datasets/no-such-ds-portability-test")
    # 桶里验不过 → 维持找不到
    monkeypatch.setattr(dsfs, "exists", lambda p: False)
    assert runner.source_dataset_of(str(droot)) is None


def test_source_video_lane_speaks_tos(tmp_path, monkeypatch):
    """轨迹页视频源档跨机可移植(2026-08-27 rerun 侧实报:所有条目都没视频):
    交付内视频被懒镜像跳过、本机没有源目录时,按交付记录的源路径 + origin
    桶映射走 tos://,文件经 dsfs 列举、播放走预签名 https。"""
    import json as _json

    from curation.ingest import dsfs
    from curation.ui import manifest as M

    droot = tmp_path / "cache" / "droid-50"
    run = droot / "20260825-015215"
    run.mkdir(parents=True)
    (run / "passed.json").write_text(_json.dumps(
        {"episodes": {}, "源数据集路径": "/mnt/tos/datasets/no-such-src"}),
        encoding="utf-8")
    (droot / "latest").write_text("20260825-015215", encoding="utf-8")
    (droot / ".tos-origin.json").write_text(_json.dumps(
        {"delivery_url": "tos://curation/deliveries/droid-50-guide"}),
        encoding="utf-8")
    monkeypatch.setenv("CURATION_TOS_MOUNT", "/mnt/tos")
    monkeypatch.setattr(dsfs, "exists", lambda p: True)
    monkeypatch.setattr(dsfs, "read_json",
                        lambda p: {"codebase_version": "v2.0"})
    monkeypatch.setattr(dsfs, "glob", lambda pat: [
        "tos://curation/datasets/no-such-src/videos/chunk-000/cam_a/episode_000003.mp4"])
    m = {"path": str(run)}
    paths = M.source_video_paths(m, "ep000003", data_root=None)
    assert paths == ["tos://curation/datasets/no-such-src/videos/chunk-000/"
                     "cam_a/episode_000003.mp4"]
    # _lane:远端免探测,按能播摆槽位
    lane = M._lane(paths[0], "ep000003", M.VIDEO_SOURCE_SOURCE)
    assert lane["playable"] is True and lane["camera"]
    # _file_url:tos:// → 预签名 https 直连桶
    monkeypatch.setattr(dsfs, "browser_media_source",
                        lambda p: "https://signed.example/" + p.split("/")[-1])
    assert M._file_url(paths[0]).startswith("https://signed.example/")


def test_delivery_records_portable_source_path(monkeypatch):
    """写端根治(2026-08-27):挂载实例产交付,「源数据集路径」记 tos:// 规范
    坐标而不是产地挂载路径;非挂载路径与 tos:// 输入原样。"""
    from curation.pipeline.run import _portable_source_path

    monkeypatch.setenv("CURATION_TOS_MOUNT", "/mnt/tos")
    monkeypatch.setenv("TOS_BUCKET", "curation")
    assert (_portable_source_path("/mnt/tos/datasets/droid_lerobot")
            == "tos://curation/datasets/droid_lerobot")
    assert _portable_source_path("tos://b/x") == "tos://b/x"
    assert _portable_source_path("/data/other") == "/data/other"
    monkeypatch.delenv("TOS_BUCKET")
    assert (_portable_source_path("/mnt/tos/datasets/x")
            == "/mnt/tos/datasets/x")   # 不知道桶名就不硬猜


def test_droid_100_semantics_profile_matches_by_name():
    """元数据残缺数据集按目录名认领语义档案(2026-08-27):官方 droid_100 的
    robot_type=unknown、names=motor_*,若不按名认领会被当关节角误杀运动学。"""
    from curation.ingest.dataset_semantics import resolve_semantics

    info = {"codebase_version": "v3.0", "robot_type": "unknown",
            "features": {"action": {"shape": [7], "names": {
                "motors": [f"motor_{i}" for i in range(7)]}}}}
    sem = resolve_semantics(info, None, "droid_100")
    assert sem.source == "profile" and sem.profile_name == "droid_100.yaml"
    assert sem.action_space == "ee" and sem.proprio_space == "ee"
    # 不带名字 / 别的名字:不误配
    assert resolve_semantics(info, None).source == "inferred"
    assert resolve_semantics(info, None, "droid_999").source == "inferred"


def test_v3_time_window_lane(tmp_path, monkeypatch):
    """v3 时间窗直播(2026-08-27 用户定'立即修'):合并 mp4 免审片站预切,
    源档/交付档给 `路径#t=from,to`,播放 URL 保片段,拖动钳制属性上标签。"""
    pd = pytest.importorskip("pandas")
    from curation.ingest import dsfs, lerobot_reader
    from curation.ui import manifest as M

    meta = pd.DataFrame([{
        "episode_index": 3,
        "videos/observation.images.cam_a/chunk_index": 0,
        "videos/observation.images.cam_a/file_index": 0,
        "videos/observation.images.cam_a/from_timestamp": 12.5,
        "videos/observation.images.cam_a/to_timestamp": 18.25,
    }])
    monkeypatch.setattr(lerobot_reader, "_load_episodes_meta", lambda d: meta)
    monkeypatch.setattr(dsfs, "read_json", lambda p: {
        "codebase_version": "v3.0",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {"observation.images.cam_a": {"dtype": "video"}}})
    M._V3_META_CACHE.clear()
    root = "tos://somebucket/datasets/x"
    wins = M._v3_episode_windows(root, 3)
    assert wins == [root + "/videos/observation.images.cam_a/chunk-000/"
                    "file-000.mp4#t=12.500,18.250"]
    assert M._v3_episode_windows(root, 99) == []      # 查无此条不猜

    # _lane:v3 路径的相机名取 videos/<cam>/ 段;远端免探;path 保片段
    lane = M._lane(wins[0], "ep000003", M.VIDEO_SOURCE_SOURCE)
    assert lane["playable"] is True and "cam a" in lane["camera"].lower().replace("_", " ")
    assert lane["path"].endswith("#t=12.500,18.250")

    # _file_url:远端 → 预签名 + 片段;本地 → gradio 文件 URL + 片段
    monkeypatch.setattr(dsfs, "browser_media_source", lambda p: "https://signed/" + p.split("/")[-1])
    u = M._file_url(wins[0])
    assert u.startswith("https://signed/") and u.endswith("#t=12.500,18.250")
    local = tmp_path / "file-000.mp4"
    local.write_bytes(b"x")
    u2 = M._file_url(str(local) + "#t=1.000,2.000")
    assert u2.startswith("/gradio_api/file=") and u2.endswith("#t=1.000,2.000")
    assert "%23" not in u2, "片段号被 URL 转义吞掉了"

    # 视频标签:钳制属性 + 片段说明,& 字符禁入属性
    attrs = M._window_attrs(wins[0])
    assert 'data-t0="12.500"' in attrs and "onseeking=" in attrs and "&" not in attrs
    assert "12.5–18.2" in M._window_caption(wins[0]).replace("&#183;", "·") or \
           "片段" in M._window_caption(wins[0])


def test_embodiment_suggestion_engine(monkeypatch):
    """机器人型号推荐两层(2026-08-27):档案登记直接置顶;试穿层维度+值域,
    谁都不像给空表绝不硬猜。"""
    np = pytest.importorskip("numpy")
    from curation.ingest import dsfs
    from curation.ui import runner

    # 档案层:droid_100 档案登记 franka
    monkeypatch.setattr(dsfs, "read_json", lambda p: {
        "codebase_version": "v3.0", "robot_type": "unknown",
        "features": {"action": {"shape": [7], "names": {
            "motors": [f"motor_{i}" for i in range(7)]}}}})
    sug = runner.suggest_embodiments("tos://b/dataset", "droid_100")
    assert sug and sug[0]["id"] == "franka" and "档案" in sug[0]["reason"]

    # 试穿层:EE 位姿样本(xyz ~0.5m)→ franka 臂展命中;7 维筛掉 6/14 dof
    class _T:
        columns = ["observation.state"]
        def __getitem__(self, k):
            class _S:
                def head(self, n): return self
                def to_list(self):
                    return [[0.5, 0.1, 0.4, 3.0, 0.2, 0.3, 0.5]] * 50
            return _S()
    monkeypatch.setattr(dsfs, "glob", lambda p: ["x.parquet"])
    monkeypatch.setattr(dsfs, "read_parquet", lambda p: _T())
    got = runner._tryon_embodiments("whatever")
    assert any(x["id"] == "franka" for x in got), got
    assert all(x["id"] not in ("so100", "aloha") for x in got)   # 维度不符不入围

    # 谁都不像:3 维怪数据 → 空表
    class _T3(_T):
        def __getitem__(self, k):
            class _S:
                def head(self, n): return self
                def to_list(self): return [[9.9, 9.9, 9.9]] * 50
            return _S()
    monkeypatch.setattr(dsfs, "read_parquet", lambda p: _T3())
    assert runner._tryon_embodiments("whatever") == []


def test_embodiment_ask_md_and_robot_type(monkeypatch):
    from curation.ingest import dsfs
    from curation.ui import runner

    md = runner.embodiment_ask_md("droid_100", [{"id": "franka", "reason": "试穿"}])
    assert "droid_100" in md and "franka" in md and "跳过运动学" in md
    assert "没有登记机器人型号" in runner.embodiment_ask_md("x", [])
    monkeypatch.setattr(dsfs, "read_json", lambda p: {"robot_type": "unknown"})
    assert runner.dataset_robot_type("tos://b/d", "x") == "unknown"
    monkeypatch.setattr(dsfs, "read_json",
                        lambda p: (_ for _ in ()).throw(OSError("x")))
    assert runner.dataset_robot_type("tos://b/d", "x") == ""


def test_cli_interactive_preflight(tmp_path, monkeypatch):
    """CLI 的两问(2026-08-27 用户定):TTY 下问型号与 v3 切分;非 TTY 绝不问
    (UI 任务台子进程停下等键盘=任务吊死)。"""
    import json as _json
    import sys as _sys
    import types

    from curation import cli

    ds = tmp_path / "mystery"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(_json.dumps(
        {"robot_type": "unknown", "codebase_version": "v3.0"}), encoding="utf-8")
    args = types.SimpleNamespace(input=str(ds), output=str(tmp_path / "out"),
                                 embodiment_id=None, only=None, skip=None,
                                 batch=False)
    # 非 TTY:一个问题都不问,args 原样
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    assert cli._interactive_run_preflight(args) is None
    assert args.embodiment_id is None and args.skip is None
    # TTY:回车吃疑似型号(monkeypatch 推荐器),y 切分
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: True)
    from curation.ui import runner as _r
    monkeypatch.setattr(_r, "suggest_embodiments",
                        lambda root, name: [{"id": "franka", "reason": "试穿"}])
    answers = iter(["", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    clip_out = cli._interactive_run_preflight(args)
    assert args.embodiment_id == "franka"
    assert clip_out and clip_out.endswith("/review/mystery")
    # TTY + skip:跳过运动学
    args2 = types.SimpleNamespace(input=str(ds), output="tos://b/deliveries/x",
                                  embodiment_id=None, only=None, skip=None,
                                  batch=False)
    answers2 = iter(["skip", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers2))
    assert cli._interactive_run_preflight(args2) is None
    assert args2.skip == "kinematic_limits" and args2.embodiment_id is None
    # TTY + 这次不跑运动学(--only 里没有它):型号一句都不问(2026-09-16 用户定)
    args3 = types.SimpleNamespace(input=str(ds), output="tos://b/deliveries/x",
                                  embodiment_id=None, only="dedup", skip=None,
                                  batch=False)
    asked = []
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": asked.append(prompt) or "n")
    assert cli._interactive_run_preflight(args3) is None
    assert not any("型号" in q for q in asked), "没跑运动学还问型号"
    assert args3.embodiment_id is None and args3.only == "dedup"


# ── 三条线统一红字(2026-08-28 用户定版)────────────────────────────────


