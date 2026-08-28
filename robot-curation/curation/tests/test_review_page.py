"""静态审片站(curation review-page + UI /review 路由)。

钉住三件事:①生成器产出 index + 逐 episode 页 + 相对路径互链;②重跑幂等
(片段已在 = 不再编码);③/review 静态路由注册即服务、且被 Basic 锁盖住。
"""
from __future__ import annotations

import os

import pytest

from curation.export.review_page import build_review_page


def _rows(n=3):
    return [{"episode_id": f"ep{i:06d}", "instruction": f"task {i}",
             "video": {"observation.images.cam_a": {"path": "/no/a.mp4",
                                                    "from_ts": 0, "to_ts": 1},
                       "observation.images.cam_b": {"path": "/no/b.mp4",
                                                    "from_ts": 0, "to_ts": 1}}}
            for i in range(n)]


def _fake_clips(monkeypatch, calls):
    import curation.export.evidence as ev

    def fake(flagged_ids, videos, out_dir, **kw):
        calls.append(list(flagged_ids))
        d = os.path.join(out_dir, "details", "audit_clips")
        os.makedirs(d, exist_ok=True)
        for eid in flagged_ids:
            for cam in sorted(videos[eid])[:kw.get("max_cams", 3)]:
                with open(os.path.join(d, f"{eid}__{cam.split('.')[-1]}.mp4"), "wb") as f:
                    f.write(b"x")
        return sum(len(videos[e]) for e in flagged_ids)

    monkeypatch.setattr(ev, "write_audit_clips", fake)


def test_build_pages_and_links(tmp_path, monkeypatch):
    calls = []
    _fake_clips(monkeypatch, calls)
    n = build_review_page(_rows(3), str(tmp_path), title="站点T")
    assert n == 6 and len(calls) == 3
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "站点T" in index and "3 条" in index
    for i in range(3):
        eid = f"ep{i:06d}"
        assert f'href="ep/{eid}.html"' in index          # 索引 → 单页(相对路径)
        page = (tmp_path / "ep" / f"{eid}.html").read_text(encoding="utf-8")
        assert f"../details/audit_clips/{eid}__cam_a.mp4" in page
        assert f"../details/audit_clips/{eid}__cam_b.mp4" in page
        assert f"task {i}" in page
    # prev/next 串场:中间条两头都有,两端只有一头
    mid = (tmp_path / "ep" / "ep000001.html").read_text(encoding="utf-8")
    assert "ep000000.html" in mid and "ep000002.html" in mid
    first = (tmp_path / "ep" / "ep000000.html").read_text(encoding="utf-8")
    assert "上一条" not in first and "ep000001.html" in first


def test_rerun_is_idempotent_on_clips(tmp_path, monkeypatch):
    """重跑只补缺:片段齐的 episode 一次编码调用都不发(TOS 上重刷站点要快)。"""
    calls = []
    _fake_clips(monkeypatch, calls)
    build_review_page(_rows(2), str(tmp_path))
    calls.clear()
    n = build_review_page(_rows(2), str(tmp_path))
    assert n == 0 and calls == []
    # 页面仍会重写(标注可能更新)
    assert (tmp_path / "index.html").exists()


def test_review_route_serves_and_auth_covers(tmp_path, monkeypatch):
    """/review 挂上就能出 index.html;配 Basic 后同样 401→200。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    for k in ("CURATION_TERMINAL", "CURATION_UI_USER", "CURATION_UI_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    delivery = tmp_path / "dlv"
    delivery.mkdir()
    (delivery / "passed.json").write_text('{"episodes": {}}', encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>review-site</h1>", encoding="utf-8")

    app = create_asgi_app(str(delivery), terminal=False, review_dir=str(site))
    with TestClient(app) as c:
        r = c.get("/review/")
        assert r.status_code == 200 and "review-site" in r.text
        assert c.get("/review/nope.html").status_code == 404

    monkeypatch.setenv("CURATION_UI_USER", "demo")
    monkeypatch.setenv("CURATION_UI_PASSWORD", "s3cret")
    app = create_asgi_app(str(delivery), terminal=False, review_dir=str(site))
    with TestClient(app) as c:
        assert c.get("/review/").status_code == 401
        assert c.get("/review/", auth=("demo", "s3cret")).status_code == 200


def test_no_review_dir_no_route(tmp_path, monkeypatch):
    """不传 review_dir:/review 不存在(与终端「不传就没有」同一约定)。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    from curation.ui.app import create_asgi_app
    for k in ("CURATION_TERMINAL", "CURATION_UI_USER", "CURATION_UI_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    delivery = tmp_path / "dlv"
    delivery.mkdir()
    (delivery / "passed.json").write_text('{"episodes": {}}', encoding="utf-8")
    app = create_asgi_app(str(delivery), terminal=False)
    with TestClient(app) as c:
        assert c.get("/review/").status_code == 404


def test_site_json_records_source_dataset(tmp_path, monkeypatch):
    """站点身份文件(2026-08-11):UI 靠它把交付对上审片站,内容必须是**源数据集**。

    防的事故:审片站原先只有一个自由文本标题,UI 认不出它是谁家的数据,只能
    "谁有这个 episode 号就用谁"——droid-ep13-20-demo 因此借用了 droid200 的片段
    (那次同源巧对,换个数据集就是给客户放错视频)。
    """
    import json
    calls = []
    _fake_clips(monkeypatch, calls)
    src = tmp_path / "datasets" / "droid_lerobot"
    src.mkdir(parents=True)
    site = tmp_path / "review" / "droid200"
    build_review_page(_rows(1), str(site), title="DROID 200 审片",
                      source_dataset=str(src))
    doc = json.loads((site / "site.json").read_text(encoding="utf-8"))
    assert doc["source_dataset"] == str(src)          # 绝对路径,便于人工对账
    assert doc["dataset_name"] == "droid_lerobot"     # 交付里记的就是这个名
    assert doc["title"] == "DROID 200 审片" and doc["generated_at"]
    # 重跑更新(幂等):时间戳字段在,内容随新参数走
    build_review_page(_rows(1), str(site), title="改了名", source_dataset=str(src))
    assert json.loads((site / "site.json").read_text(encoding="utf-8"))["title"] == "改了名"
    # 不给源数据集就**不写**这个文件:宁可让 UI 走降级,也不写一张存疑的身份证
    plain = tmp_path / "review" / "plain"
    build_review_page(_rows(1), str(plain), title="无源")
    assert not (plain / "site.json").exists()


# ── 切片入交付(build_delivery_clips,2026-08-28 去挂载依赖)──────────────


def _batch(tmp_path):
    """最小批次目录:passed.json 在场 = resolve_run 认它自己。"""
    import json
    b = tmp_path / "20260828-000000"
    (b / "details").mkdir(parents=True)
    (b / "passed.json").write_text(json.dumps({"episodes": []}), encoding="utf-8")
    return b


def test_delivery_clips_local_batch_and_idempotent(tmp_path, monkeypatch):
    """本地交付:片段落 <批次>/review_clips + 索引进 details/;重跑 0 新编码。"""
    import json

    from curation.export.review_page import build_delivery_clips
    calls = []
    _fake_clips(monkeypatch, calls)
    b = _batch(tmp_path)
    n, where = build_delivery_clips(_rows(2), str(b))
    assert n == 4 and where == str(b)
    for i in range(2):
        for cam in ("cam_a", "cam_b"):
            assert (b / "review_clips" / f"ep{i:06d}__{cam}.mp4").exists()
    idx = json.loads((b / "details" / "review_clips_index.json")
                     .read_text(encoding="utf-8"))
    assert idx == {"ep000000": ["cam_a", "cam_b"], "ep000001": ["cam_a", "cam_b"]}
    # 幂等:索引齐了就一条不重切
    calls.clear()
    n2, _ = build_delivery_clips(_rows(2), str(b))
    assert n2 == 0 and calls == []


def test_delivery_clips_remote_uploads_and_cleans_local(tmp_path, monkeypatch):
    """桶交付:逐条上传到 <批次>/review_clips/、传完即删本地;索引也上传。"""
    import json

    from curation import tos_store
    from curation.export.review_page import build_delivery_clips
    calls = []
    _fake_clips(monkeypatch, calls)

    uploads = []

    class _St:
        def get_bytes(self, bucket, key):
            raise FileNotFoundError(key)      # 还没有索引

        def upload(self, local, bucket, key):
            uploads.append((bucket, key, os.path.exists(local)))

    monkeypatch.setattr(tos_store, "resolve_run_url",
                        lambda url, region=None, **kw: "tos://bkt/d/x/20260828-000000")
    monkeypatch.setattr(tos_store, "make_store_for",
                        lambda bucket, region=None, **kw: _St())
    n, where = build_delivery_clips(_rows(1), "tos://bkt/d/x",
                                    region="cn-beijing")
    assert n == 2 and where == "tos://bkt/d/x/20260828-000000"
    keys = [k for _b, k, _e in uploads]
    assert "d/x/20260828-000000/review_clips/ep000000__cam_a.mp4" in keys
    assert "d/x/20260828-000000/review_clips/ep000000__cam_b.mp4" in keys
    assert keys[-1] == "d/x/20260828-000000/details/review_clips_index.json"
    assert all(b == "bkt" for b, _k, _e in uploads)
    assert all(e for _b, _k, e in uploads), "上传时本地文件必须还在"


def test_cli_review_page_requires_exactly_one_target(tmp_path, capsys):
    """--output 与 --into-delivery 二选一:都给/都不给 = 输入错误。"""
    from curation.cli import main
    assert main(["review-page", "--input", str(tmp_path)]) == 2
    assert "二选一" in capsys.readouterr().err


def test_prune_delivery_clips_local_and_remote(tmp_path, monkeypatch):
    """rejudge 剔条目后清片段:索引减行、本地文件删、远端(给了 URL)点名删;
    没剔任何在册条目 = 0 且索引不动。"""
    import json

    from curation.export.review_page import prune_delivery_clips
    b = _batch(tmp_path)
    (b / "review_clips").mkdir()
    for eid, cams in (("ep000000", ["cam_a"]), ("ep000001", ["cam_a", "cam_b"])):
        for c in cams:
            (b / "review_clips" / f"{eid}__{c}.mp4").write_bytes(b"x")
    (b / "details" / "review_clips_index.json").write_text(json.dumps(
        {"ep000000": ["cam_a"], "ep000001": ["cam_a", "cam_b"]}), encoding="utf-8")

    deletes = []

    class _St:
        def delete(self, bucket, key):
            deletes.append((bucket, key))
    from curation import tos_store
    monkeypatch.setattr(tos_store, "make_store_for",
                        lambda bucket, region=None, **kw: _St())
    n = prune_delivery_clips(str(b), ["ep000000"],
                             remote_url="tos://bkt/d/RUN", region=None)
    assert n == 1
    assert (b / "review_clips" / "ep000000__cam_a.mp4").exists()
    assert not (b / "review_clips" / "ep000001__cam_a.mp4").exists()
    assert ("bkt", "d/RUN/review_clips/ep000001__cam_a.mp4") in deletes
    assert ("bkt", "d/RUN/review_clips/ep000001__cam_b.mp4") in deletes
    idx = json.loads((b / "details" / "review_clips_index.json")
                     .read_text(encoding="utf-8"))
    assert idx == {"ep000000": ["cam_a"]}
    assert prune_delivery_clips(str(b), ["ep000000"]) == 0
