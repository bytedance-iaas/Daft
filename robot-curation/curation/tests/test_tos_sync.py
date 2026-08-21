"""桶里的交付:解析批次 / 全量镜像 / 改动写回(2026-08-21,rejudge·reprofile 接 tos://)。
字典当桶,离线。"""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from curation import tos_store


class _DictClient:
    def __init__(self, objs: dict[str, bytes]):
        self.objs = dict(objs)
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def list_objects_type2(self, bucket, prefix="", delimiter=None,
                           continuation_token=None, max_keys=1000):
        keys = sorted(k for k in self.objs if k.startswith(prefix))
        contents = [SimpleNamespace(key=k, size=len(self.objs[k]),
                                    etag='"' + hashlib.md5(self.objs[k]).hexdigest() + '"')
                    for k in keys]
        return SimpleNamespace(contents=contents, common_prefixes=[], is_truncated=False)

    def get_object(self, bucket, key):
        data = self.objs[key]
        return SimpleNamespace(read=lambda: data)

    def get_object_to_file(self, bucket, key, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(self.objs[key])

    def put_object_from_file(self, bucket, key, local_path):
        self.objs[key] = open(local_path, "rb").read()
        self.puts.append(key)

    def delete_object(self, bucket, key):
        self.objs.pop(key, None)
        self.deletes.append(key)


def _store(objs):
    return tos_store.TosStore("ep", "cn-beijing", client=_DictClient(objs))


RUN = "deliveries/d1/20260821-000000"
OBJS = {
    "deliveries/d1/latest": b"20260821-000000\n",
    "deliveries/d1/human-decisions/label_decisions.csv": b"a,b\n",
    f"{RUN}/passed.json": b'{"x":1}',
    f"{RUN}/report.md": b"# r",
    f"{RUN}/details/k.csv": b"1,2",
    f"{RUN}/lerobot_curated/meta/info.json": b"{}",
    f"{RUN}/lerobot_curated/data/e0.parquet": b"PQ0",
    f"{RUN}/lerobot_curated/data/e1.parquet": b"PQ1",
}


def test_resolve_run_url_delivery_vs_run_vs_garbage():
    st = _store(OBJS)
    assert tos_store.resolve_run_url("tos://bkt/deliveries/d1", store=st) == f"tos://bkt/{RUN}"
    assert tos_store.resolve_run_url(f"tos://bkt/{RUN}/", store=st) == f"tos://bkt/{RUN}"
    with pytest.raises(tos_store.TosStageError):
        tos_store.resolve_run_url("tos://bkt/deliveries/nothing", store=st)


def test_mirror_full_vs_light_share_one_cache_tree(tmp_path, monkeypatch):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    st = _store(OBJS)
    light = tos_store.mirror_run(f"tos://bkt/{RUN}", store=st,
                                 skip_dirs=tos_store.MIRROR_SKIP_DIRS_LIGHT)
    assert os.path.isfile(os.path.join(light, "report.md"))
    assert not os.path.exists(os.path.join(light, "lerobot_curated")), "轻镜像不下大件"
    deliv = os.path.dirname(light)
    assert os.path.isfile(os.path.join(deliv, "human-decisions", "label_decisions.csv"))
    assert os.path.isfile(os.path.join(deliv, "latest"))
    assert os.path.isfile(os.path.join(deliv, tos_store.ORIGIN_NAME))
    full = tos_store.mirror_run(f"tos://bkt/{RUN}", store=st)
    assert full == light, "同一棵缓存树"
    assert os.path.isfile(os.path.join(full, "lerobot_curated", "data", "e1.parquet"))


def test_sync_back_uploads_changed_deletes_orphans_skips_same(tmp_path, monkeypatch):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    st = _store(OBJS)
    local = tos_store.mirror_run(f"tos://bkt/{RUN}", store=st)
    # 本地改:report 改了、e1 剔了、新加 e2;human-decisions 追加一行;passed.json 没动
    open(os.path.join(local, "report.md"), "wb").write(b"# r2")
    os.remove(os.path.join(local, "lerobot_curated", "data", "e1.parquet"))
    open(os.path.join(local, "lerobot_curated", "data", "e2.parquet"), "wb").write(b"PQ2")
    hd = os.path.join(os.path.dirname(local), "human-decisions", "label_decisions.csv")
    open(hd, "ab").write(b"c,d\n")
    open(os.path.join(local, ".curation-tmp"), "wb").write(b"junk")   # 发布残留不上传
    r = tos_store.sync_back(local, f"tos://bkt/{RUN}", store=st)
    c = st._c
    assert f"{RUN}/report.md" in c.puts and f"{RUN}/lerobot_curated/data/e2.parquet" in c.puts
    assert "deliveries/d1/human-decisions/label_decisions.csv" in c.puts
    assert f"{RUN}/passed.json" in c.puts, "完整性标志永远重传"
    assert c.puts.index(f"{RUN}/passed.json") > c.puts.index(f"{RUN}/report.md"), "marker 最后传"
    assert f"{RUN}/details/k.csv" not in c.puts, "没变的不传"
    assert c.deletes == [f"{RUN}/lerobot_curated/data/e1.parquet"], "剔掉的删,且只删批次下的"
    assert not any(k.endswith(".curation-tmp") for k in c.puts)
    assert r["deleted"] == 1 and r["uploaded"] >= 4 and r["skipped"] >= 2
    # 第二次写回:除 marker 外全部跳过,零删除
    r2 = tos_store.sync_back(local, f"tos://bkt/{RUN}", store=st)
    assert r2["deleted"] == 0 and r2["uploaded"] == 2       # passed.json + meta/info.json


def test_cli_rejudge_on_tos_delivery_mirrors_runs_and_syncs(tmp_path, monkeypatch, capsys):
    """CLI:--delivery tos:// → resolve_run_url → 全量 mirror → 本地执行 → sync_back。"""
    from curation import cli
    calls = {}
    monkeypatch.setattr(tos_store, "resolve_run_url",
                        lambda url, region=None, **kw: calls.setdefault("resolve", (url, region)) and f"tos://bkt/{RUN}")
    local = tmp_path / "cache" / "reports" / "bkt" / "deliveries" / "d1" / "20260821-000000"
    local.mkdir(parents=True)
    (local / "passed.json").write_text("{}")
    monkeypatch.setattr(tos_store, "mirror_run",
                        lambda url, region=None, **kw: calls.setdefault("mirror", url) and str(local))
    def fake_sync(loc, url, region=None, **kw):
        calls["sync"] = (loc, url, region)
        return {"uploaded": 1, "deleted": 0, "skipped": 3}

    def fake_run(delivery, inp, cfg, **kw):
        calls["run"] = (delivery, inp)
        return {"ok": 1}
    monkeypatch.setattr(tos_store, "sync_back", fake_sync)
    import curation.pipeline.rejudge as rj
    monkeypatch.setattr(rj, "run_rejudge", fake_run)
    from curation.ingest import dsfs
    monkeypatch.setattr(dsfs, "configure", lambda region=None: calls.setdefault("rg", region))
    rc = cli.main(["rejudge", "--delivery", "tos://bkt/deliveries/d1",
                   "--delivery-region", "cn-beijing",
                   "--input", "tos://src/datasets/x", "--input-region", "ap-southeast-1"])
    assert rc == 0
    assert calls["resolve"] == ("tos://bkt/deliveries/d1", "cn-beijing")
    assert calls["run"] == (str(local), "tos://src/datasets/x")
    assert calls["rg"] == "ap-southeast-1"
    assert calls["sync"] == (str(local), f"tos://bkt/{RUN}", "cn-beijing")
    assert "已同步回" in capsys.readouterr().out


def test_source_dataset_of_accepts_tos_and_region(tmp_path):
    from curation.ui import runner
    run = tmp_path / "d" / "20260821-000000"
    run.mkdir(parents=True)
    (run / "passed.json").write_text(
        '{"源数据集路径": "tos://src/datasets/x", "源数据集地区": "cn-beijing", "episodes": []}',
        encoding="utf-8")
    (tmp_path / "d" / "latest").write_text("20260821-000000")
    assert runner.source_dataset_of(str(tmp_path / "d")) == "tos://src/datasets/x"
    assert runner.source_region_of(str(tmp_path / "d")) == "cn-beijing"
