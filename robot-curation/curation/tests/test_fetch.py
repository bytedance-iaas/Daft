"""`curation fetch` 的防线:P0 实跑扎出来的四个事实 + 六步纪律,逐条钉死。

背景(2026-08-18 P0 实测,都是真事故不是假想):
  ① 镜像站 inspect 报的 Used Storage 高报可达 1200 倍(aloha_sim_transfer_cube_human
     报 2.90 GB / 真实 2.4 MB)—— 体积只认逐文件 HEAD;
  ② 镜像里存在源头就残缺的数据集:那个 aloha 唯一的 mp4 在源站 content-length
     就是 0,下载器忠实下成 0 字节并报成功 —— 入库前必须结构化校验;
  ③ 跨文件系统 du 总量对不上(目录项差正好 6×4096)—— 逐文件比大小;
  ④ 下载器直指 /mnt/tos 会撞 FSX 静默残缺(2026-08-05 实锤)—— 必须本地暂存。
生产 pod 没有下载器,单测全部用假件覆盖接缝(list/HEAD/download),不真调。
"""
from __future__ import annotations

import datetime
import json
import os
import threading

import pytest

from curation import fetch


# ── 夹具 ─────────────────────────────────────────────────────────────────

#: 一个"健康"的小数据集:info.json 声明 dtype video,mp4 非空。
GOOD_FILES = {
    "meta/info.json": json.dumps({
        "codebase_version": "v3.0",
        "features": {
            "observation.images.top": {"dtype": "video"},
            "action": {"dtype": "float32"},
        },
    }).encode(),
    "meta/tasks.parquet": b"PARQ" * 10,
    "data/chunk-000/file-000.parquet": b"P" * 64,
    "videos/observation.images.top/chunk-000/file-000.mp4": b"\x00\x00\x00 ftypisom" + b"v" * 50,
}

#: libero 型:画面是 dtype image 内嵌 parquet,0 个 mp4 —— 我们的视觉类检查跑不了。
IMAGE_ONLY_FILES = {
    "meta/info.json": json.dumps({
        "features": {
            "observation.images.top": {"dtype": "image"},
            "action": {"dtype": "float32"},
        },
    }).encode(),
    "data/chunk-000/file-000.parquet": b"P" * 128,
}

#: 实拍的 inspect 输出形状(2026-08-18 bookworm pod 上抓的 libero 真输出裁短):
#: Used Storage 故意放一个**天文数字** —— 谁要是去读它,体积预检立刻会拒。
INSPECT_TEMPLATE = """ID: HuggingFaceVLA/{ref}
Created At: 2025-09-05T08:48:26.000Z
Last Modified: 2025-09-30T09:45:51.000Z
Used Storage(in Bytes): 69862654325
Files:
-----------------------------
{files}
"""


def _cfg(tmp_path):
    root = tmp_path / "datasets"
    root.mkdir(exist_ok=True)
    return {
        "fetch_sources": {"ai-infra": {
            "display_name": "内网 HF 镜像缓存桶",
            "bucket": "ai-infra",
            # RFC 2606 保留域,测试绝不发真请求(HEAD 接缝也被假件覆盖)
            "base_url": "https://mirror.example.com/dataset",
        }},
        "tos_buckets": [{"datasets_path": str(root)}],
    }


def _wire(monkeypatch, files: dict[str, bytes], *, head_sizes: dict | None = None,
          free: int = 10 ** 9, inspect_files: list[str] | None = None):
    """把三条接缝换成假件:清单来自实拍格式的 inspect 文本,HEAD 大小默认与
    文件内容一致(源站健康),download 把 files 写进 <staging>/<ref>/。
    返回 calls 记录(download 收到的参数),供断言透传。
    两个环境变量要摘干净:pod 部署里注入的存储限额/测试残留的批预算会让
    "按 df 假件算"的用例在真容器里跑出另一个结果。"""
    monkeypatch.delenv("CURATION_EPHEMERAL_LIMIT_BYTES", raising=False)
    monkeypatch.delenv("CURATION_FETCH_BATCH_BUDGET", raising=False)
    # 生产常态:落地在 /mnt/tos 挂载、暂存在容器本地盘 —— 不同文件系统。
    # 单测里 tmp_path 下两者天然同盘,不摁住的话"同盘严口径"会让所有既有
    # 用例都撞上"落地全量也算进预算"的拒绝;同盘场景的用例自己再打回真值。
    monkeypatch.setattr(fetch, "same_filesystem", lambda a, b: False)
    names = inspect_files if inspect_files is not None else sorted(files)
    text = INSPECT_TEMPLATE.format(ref="x", files="\n".join(names))
    monkeypatch.setattr(fetch, "_oniond_inspect_text", lambda s, r: text)

    sizes = {f: len(b) for f, b in files.items()}
    if head_sizes:
        sizes.update(head_sizes)

    def fake_remote_size(url):
        rel = url.split("/dataset/", 1)[1].split("/", 1)[1]
        return sizes[rel]
    monkeypatch.setattr(fetch, "remote_size", fake_remote_size)
    monkeypatch.setattr(fetch, "free_bytes", lambda p: free)

    calls = {"download": []}

    def fake_download(source, ref, staging, includes):
        calls["download"].append({"ref": ref, "staging": staging,
                                  "includes": includes})
        root = os.path.join(staging, ref)
        want = set(names)
        if includes:
            prefix = f"{source['base_url'].rstrip('/')}/{ref}/"
            want = {f for f in want
                    if fetch._include_match(f, includes, prefix)}
        for f in sorted(want):
            p = os.path.join(root, f)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fp:
                fp.write(files.get(f, b""))
    monkeypatch.setattr(fetch, "download_dataset", fake_download)
    return calls


def _run(cfg, monkeypatch=None, tmp_path=None, **kw):
    kw.setdefault("staging_root", str(tmp_path / "staging") if tmp_path else None)
    return fetch.run_fetch(cfg, kw.pop("source", "ai-infra"), kw.pop("ref", "x"), **kw)


# ── 体积预检 ─────────────────────────────────────────────────────────────

def test_precheck_refuses_and_names_three_numbers(tmp_path, monkeypatch, capsys):
    """暂存装不下**单个文件**:当场拒绝,报文必须把 需要/可用/差额 三个数都说清
    —— 含糊一句"空间不够"会让人不知道该清多少、还是该换暂存盘。
    (改成分批下载后,**总体积**大于本地盘不再是拒绝理由 —— libero 32.5 GiB
    就是靠分批+拷完即删下进 30Gi 配额的;批再小也得装下最大的那个文件,
    所以单文件超预算才是死路。)"""
    cfg = _cfg(tmp_path)
    calls = _wire(monkeypatch, GOOD_FILES, free=50)   # 最大单文件 >100 B > 可用 50 B
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    big = max(len(b) for b in GOOD_FILES.values())
    assert rc == 2
    assert f"需要 {big} B" in err and "可用 50 B" in err
    assert f"{big - 50} B" in err and "差" in err
    assert not calls["download"], "预检拒了就不许再去下载"


def test_total_size_beyond_local_disk_is_fine_with_batching(tmp_path, monkeypatch,
                                                            capsys):
    """★ 分批的存在意义:总体积 > 本地可用空间也能下(逐批过境,峰值 ≈ 2 批)。
    真事故:libero 真实 32.5 GiB,容器 ephemeral-storage 只有 30Gi,旧的
    "整集预检"直接拒 —— 分批后必须放行。"""
    cfg = _cfg(tmp_path)
    total = sum(len(b) for b in GOOD_FILES.values())
    big = max(len(b) for b in GOOD_FILES.values())
    free = big + 60          # 单文件装得下,但总体积装不下
    assert free < total
    calls = _wire(monkeypatch, GOOD_FILES, free=free)
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", str(big))
    rc = _run(cfg, tmp_path=tmp_path)
    assert rc == 0, capsys.readouterr().err
    assert len(calls["download"]) >= 2, "总量超盘就必须分成多批逐批过境"
    assert (tmp_path / "datasets" / "x" / "meta" / "info.json").exists()


def test_inflated_inspect_storage_is_ignored(tmp_path, monkeypatch, capsys):
    """★ 钉死事实①:inspect 文本里 Used Storage 写着 65 GiB(实拍值 69862654325),
    HEAD 实测只有几百 B —— 预检必须按 HEAD 走。谁要是回退成读 inspect 的数,
    free=10^6 B 会被 65 GiB 吓得当场拒绝,本用例立刻红。
    (真事故:aloha 报 2.90 GB / 真实 2.4 MB,高报 1200 倍。)"""
    cfg = _cfg(tmp_path)
    calls = _wire(monkeypatch, GOOD_FILES, free=10 ** 6)
    rc = _run(cfg, tmp_path=tmp_path)
    assert rc == 0, capsys.readouterr().err
    assert calls["download"], "几百 B 的真实体积在 1 MB 可用空间下必须放行"


# ── 结构化校验(事实②:退出码 0 不等于下载完好)──────────────────────────

def _expect_reject(tmp_path, monkeypatch, capsys, files, *, head_sizes=None,
                   must_mention: str = ""):
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, files, head_sizes=head_sizes)
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 1, err
    assert "拒绝入库" in err
    if must_mention:
        assert must_mention in err, f"要点名问题文件,实际报文:\n{err}"
    # 目标目录不许出现哨兵(拒绝入库 = 一个字节都不进数据集根)
    assert not os.path.exists(tmp_path / "datasets" / "x" / "meta" / "info.json")
    # 暂存必须保留供排查,且报文说了在哪
    assert "暂存保留" in err
    staging = tmp_path / "staging"
    assert any(staging.iterdir()), "校验失败时暂存目录必须保留"


def test_zero_byte_video_rejected(tmp_path, monkeypatch, capsys):
    """★ 钉死事实②:源站 mp4 本身就是 0 字节(HEAD 也报 0,大小逐个相等这条
    过得去)—— 但 0 字节视频必须被点名拒绝。真事故:aloha_sim_transfer_cube_human
    唯一的 mp4 源头即空,下载器下成 0 字节并报成功。"""
    files = dict(GOOD_FILES)
    files["videos/observation.images.top/chunk-000/file-000.mp4"] = b""
    _expect_reject(tmp_path, monkeypatch, capsys, files,
                   must_mention="0 字节视频")


def test_aria2_residue_rejected(tmp_path, monkeypatch, capsys):
    """下载目录里残留 *.aria2 = 断点没续完,哪怕清单文件都在也不许入库。"""
    files = dict(GOOD_FILES)
    files["data/chunk-000/file-000.parquet.aria2"] = b"resume-state"
    # 残留文件不在 inspect 清单里(它是下载器的私货),单独摆进暂存
    _expect_reject(tmp_path, monkeypatch, capsys, files,
                   must_mention="续传残留")


def test_size_mismatch_rejected(tmp_path, monkeypatch, capsys):
    """本地大小 ≠ 源站 HEAD:半截文件绝不入库(事实③:总量对账会被目录项
    4096 糊弄过去,必须逐文件比)。"""
    _expect_reject(tmp_path, monkeypatch, capsys, GOOD_FILES,
                   head_sizes={"data/chunk-000/file-000.parquet": 9999},
                   must_mention="大小不符")


def test_broken_info_json_rejected(tmp_path, monkeypatch, capsys):
    """meta/info.json 解析不了(FSX 家族最爱的坏法是零填充)→ 拒绝入库。"""
    files = dict(GOOD_FILES)
    files["meta/info.json"] = b"\x00" * 40
    _expect_reject(tmp_path, monkeypatch, capsys, files,
                   must_mention="info.json")


# ── 哨兵最后写 ───────────────────────────────────────────────────────────

def test_sentinel_written_last(tmp_path, monkeypatch):
    """★ meta/info.json 必须是最后一个落地的文件:list_datasets 判"有效数据集"
    只看它 —— 先落哨兵,边拷边打开界面的人就会点进一个视频还在路上的数据集,
    跑质检读到一半没了。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    order = []
    real = fetch.publish_file

    def spy(src, dst):
        order.append(dst)
        return real(src, dst)
    monkeypatch.setattr(fetch, "publish_file", spy)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert len(order) == len(GOOD_FILES)
    assert order[-1].endswith(os.path.join("meta", "info.json"))


# ── 幂等 / 覆盖 ──────────────────────────────────────────────────────────

def test_existing_dataset_skipped(tmp_path, monkeypatch, capsys):
    """哨兵已在 → 跳过并说明,不去碰源站(幂等重跑不烧流量不动数据)。"""
    cfg = _cfg(tmp_path)
    calls = _wire(monkeypatch, GOOD_FILES)
    target = tmp_path / "datasets" / "x" / "meta"
    target.mkdir(parents=True)
    (target / "info.json").write_text("{}")
    rc = _run(cfg, tmp_path=tmp_path)
    assert rc == 0
    assert "跳过" in capsys.readouterr().out
    assert not calls["download"]


def test_overwrite_replaces_stale_files(tmp_path, monkeypatch):
    """--overwrite:旧目录整个换新 —— 上一版残留的文件不许混进新数据集
    (半新半旧比纯旧更毒,报表会把两版的条目算成一版)。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    target = tmp_path / "datasets" / "x"
    (target / "meta").mkdir(parents=True)
    (target / "meta" / "info.json").write_text("{}")
    (target / "stale-leftover.bin").write_text("old")
    assert _run(cfg, tmp_path=tmp_path, overwrite=True) == 0
    assert not (target / "stale-leftover.bin").exists()
    got = json.loads((target / "meta" / "info.json").read_text())
    assert "features" in got, "覆盖后应是新下的 info.json,不是旧壳"


# ── 跨 pod 进行中标记 ────────────────────────────────────────────────────

def _marker(tmp_path, hours_ago: float, host="peer-pod-abc"):
    into = tmp_path / "datasets"
    at = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(hours=hours_ago))
    p = into / ".fetching-x.json"
    p.write_text(json.dumps({"host": host, "ref": "x",
                             "started_at": at.isoformat(timespec="seconds")}))
    return p


def test_fresh_marker_from_peer_refuses_and_names_them(tmp_path, monkeypatch,
                                                       capsys):
    """别的实例(robot-curator 与 rerun 的 daft-curation 挂同一个桶,之间没有锁)
    正在下同一个数据集 → 拒绝,并说清是谁、什么时候开始的 —— 不拒就是两边互相
    覆盖写了一半的文件,静默残缺。"""
    cfg = _cfg(tmp_path)
    calls = _wire(monkeypatch, GOOD_FILES)
    marker = _marker(tmp_path, hours_ago=1)
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "peer-pod-abc" in err and "开始" in err
    assert not calls["download"]
    assert marker.exists(), "别人的标记不许被我们顺手删掉"


def test_stale_marker_is_taken_over(tmp_path, monkeypatch, capsys):
    """超过陈旧线的标记 = 崩溃残留,允许接管 —— 否则一次 pod 崩溃就把这个
    数据集永久堵死(标记不是锁,没有人会来释放它)。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    _marker(tmp_path, hours_ago=fetch.STALE_MARKER_HOURS + 1)
    rc = _run(cfg, tmp_path=tmp_path)
    assert rc == 0
    assert "陈旧" in capsys.readouterr().out


def test_marker_cleared_on_success_kept_as_failure_record_on_failure(
        tmp_path, monkeypatch, capsys):
    """成功要清自己的标记(留着就是给下一次跑批造假占用);失败**不清**,改写成
    失败记录(断在哪个阶段/第几批)—— 但失败记录是墓碑不是占用:marker_verdict
    判 "failed" 而不是 "fresh",重跑立即接管,不吃 12 小时陈旧线。
    (分批改造前的语义是"失败也清":那时留标记确实等于假占用;现在 failed
    状态不挡道,现场信息留给人排查,两头都占住。)"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    marker = tmp_path / "datasets" / ".fetching-x.json"
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert not marker.exists()

    bad = dict(GOOD_FILES)
    bad["meta/info.json"] = b"not json"
    _wire(monkeypatch, bad)
    assert _run(cfg, tmp_path=tmp_path, name="y", overwrite=True) == 1
    marker_y = tmp_path / "datasets" / ".fetching-y.json"
    assert marker_y.exists(), "失败要留失败记录(断在哪儿),不许一擦了之"
    state, desc = fetch.marker_verdict(str(marker_y))
    assert state == "failed", "失败记录不算占用,不许把重跑挡 12 小时"
    assert "整体校验" in desc


# ── 格式吃不进去的数据集(用户定:照下 + 明确标注)────────────────────────

def test_image_only_dataset_ingested_with_explicit_note(tmp_path, monkeypatch,
                                                        capsys):
    """libero 型(dtype: image 内嵌 parquet、0 个 mp4)→ 照样入库成功,但输出
    末尾必须有具体判据的标注(没有视频文件/画面内嵌 parquet/哪些检查跑不了),
    不许含糊说"可能不支持"。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, IMAGE_ONLY_FILES)
    rc = _run(cfg, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert (tmp_path / "datasets" / "x" / "meta" / "info.json").exists()
    assert "无法" in out and "视觉" in out
    assert "没有视频文件" in out and "parquet" in out
    assert "可能" not in out, "判据要说死,不许含糊"


def test_include_does_not_suppress_the_unsupported_note(tmp_path, monkeypatch,
                                                        capsys):
    """★用了 --include 也要判"这个数据集能不能做视觉检查",判据是**证据够不够**,
    不是"用没用 --include"。

    2026-08-18 实机咬到:`--include "meta/*"` 拉了含 info.json 的 libero,落地就是
    个 image-only 数据集,却因为带了 --include 而一声不吭 —— 原实现写的是
    `note = None if includes else ...`,一见 --include 就整个跳过判断。
    判"有没有视频"必须看**源站完整清单**;只看下载集合会把"我这次没拉视频"
    误判成"这数据集没有视频"(反过来也一样)。
    """
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, IMAGE_ONLY_FILES)
    rc = _run(cfg, tmp_path=tmp_path, includes=["meta/*"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "无法" in out and "没有视频文件" in out, "带 --include 时标注不许消失"


def test_include_without_video_files_says_so_instead_of_claiming_unsupported(
        tmp_path, monkeypatch, capsys):
    """反面:数据集**本来有** mp4,只是这次 --include 没拉 —— 不许说成"本系统
    无法做视觉检查"(那是污蔑数据),要说"本次没拉视觉文件"。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    rc = _run(cfg, tmp_path=tmp_path, includes=["meta/*"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "本次没有拉取视觉文件" in out
    assert "无法对它做视觉类检查" not in out, "数据集有 mp4,不许说成系统不支持"


def test_video_dataset_gets_no_scary_note(tmp_path, monkeypatch, capsys):
    """正常带 mp4 的数据集不许误挂"无法做视觉检查"的标注(狼来了)。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert "无法" not in capsys.readouterr().out


# ── --include / --name / --into / --source ──────────────────────────────

def test_include_scopes_manifest_and_download_gets_exact_paths(tmp_path,
                                                               monkeypatch):
    """--include 用来筛**清单**(匹配语义照抄下载器:未锚定正则、对完整 URL),
    体积预检只量匹配的文件 —— 预检量全量、实下一半,预检就是在拿别人的体积
    吓唬人。真正传给下载器的不再是用户模式,而是**批内文件的精确路径**(每个
    文件一个模式,实测约束见 download_dataset)—— 预检、下载、校验三方集合
    从"逐字一致"升级成"同一份清单"。"""
    cfg = _cfg(tmp_path)
    head_urls = []
    calls = _wire(monkeypatch, GOOD_FILES)
    fake = fetch.remote_size          # _wire 装上的假件;spy 包在它外面记录 URL

    def spy(url):
        head_urls.append(url)
        return fake(url)
    monkeypatch.setattr(fetch, "remote_size", spy)
    rc = _run(cfg, tmp_path=tmp_path, includes=["meta/*"])
    assert rc == 0
    downloaded = [f for c in calls["download"] for f in c["includes"]]
    assert sorted(downloaded) == ["meta/info.json", "meta/tasks.parquet"], \
        "下载器收到的必须是 --include 筛过的清单的精确路径"
    assert head_urls and all("/meta/" in u for u in head_urls), \
        "--include 之外的文件不该被 HEAD"
    # 只拷了 meta 下的文件;info.json 虽然被 --include 命中也**不发布**
    # (部分拉取不完整,不许自称可用数据集,见 fetch.py 第⑥步注释)
    target = tmp_path / "datasets" / "x"
    assert (target / "meta" / "tasks.parquet").exists()
    assert not (target / "meta" / "info.json").exists()
    assert not (target / "data").exists()


def test_partial_fetch_without_meta_says_so(tmp_path, monkeypatch, capsys):
    """--include 只拉 data/*:不报"校验失败"(拉哪些是用户自己选的),但要把
    部分拉取的后果说人话讲清 —— 不会被当作可用数据集列出、为什么(数据不全,
    列出去质检会读到不存在的数据)、怎么得到可用数据集(完整拉取)。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    rc = _run(cfg, tmp_path=tmp_path, includes=["data/*"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "部分拉取" in out and "不会被当作可用数据集列出" in out
    assert "不带 --include" in out, "要告诉用户怎么得到可用数据集"
    assert not (tmp_path / "datasets" / "x" / "meta" / "info.json").exists()


def test_name_and_into_take_effect(tmp_path, monkeypatch):
    """--name 定落地名、--into 定落地根:两个都给就落在 <into>/<name>。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    other = tmp_path / "elsewhere"
    other.mkdir()
    rc = _run(cfg, tmp_path=tmp_path, into=str(other), name="landed")
    assert rc == 0
    assert (other / "landed" / "meta" / "info.json").exists()


def test_default_into_is_first_tos_bucket(tmp_path, monkeypatch, capsys):
    """--into 缺省 = 配置里**第一个** tos_buckets 的 datasets_path(与 UI 的
    默认桶同一条约定);连 tos_buckets 都没有就明说要 --into,不猜路径。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert (tmp_path / "datasets" / "x" / "meta" / "info.json").exists()

    rc = fetch.run_fetch({"fetch_sources": cfg["fetch_sources"]}, "ai-infra", "x")
    assert rc == 2
    assert "--into" in capsys.readouterr().err


def test_unknown_source_rejected_with_choices(tmp_path, capsys):
    """--source 给了没声明过的来源 → 明确报错并列出可选,不静默回落默认。"""
    rc = fetch.run_fetch(_cfg(tmp_path), "hf", "x")
    err = capsys.readouterr().err
    assert rc == 2
    assert "ai-infra" in err


def test_missing_into_dir_rejected(tmp_path, capsys):
    """落地根目录不存在 = 多半是挂载没就位:静默 makedirs 会把数据下到容器
    本地盘,看着成功、桶里什么都没有 —— 必须当场拒绝。"""
    cfg = _cfg(tmp_path)
    rc = fetch.run_fetch(cfg, "ai-infra", "x", into=str(tmp_path / "not-mounted"))
    assert rc == 2
    assert "不存在" in capsys.readouterr().err


def test_staging_on_tos_mount_rejected(tmp_path, capsys):
    """★ 钉死事实④:暂存(=下载器的写入目标)绝不允许指到 /mnt/tos ——
    FSX 撞 aria2 多连接随机写 → 静默残缺(2026-08-05 实锤)。"""
    cfg = _cfg(tmp_path)
    rc = fetch.run_fetch(cfg, "ai-infra", "x", staging_root="/mnt/tos/anywhere")
    err = capsys.readouterr().err
    assert rc == 2
    assert "/mnt/tos" in err and "暂存" in err


# ── inspect 输出解析(格式按 2026-08-18 实拍钉死)────────────────────────

def test_parse_inspect_files_real_shape():
    """实拍格式:Files: 行后一行纯虚线,再往下每行一个裸路径;Used Storage
    那行绝不进清单。格式变了(没有 Files: 段)要报错而不是静默空清单。"""
    text = INSPECT_TEMPLATE.format(
        ref="libero", files="meta/info.json\ndata/chunk-000/file-000.parquet")
    assert fetch.parse_inspect_files(text) == [
        "meta/info.json", "data/chunk-000/file-000.parquet"]
    with pytest.raises(fetch.FetchError):
        fetch.parse_inspect_files("ID: x\nUsed Storage(in Bytes): 1\n")


# ── CLI 接线与帮助措辞 ───────────────────────────────────────────────────

def test_cli_wiring_passes_all_flags(monkeypatch, tmp_path):
    """cli.main 的 fetch 分支把全部参数原样交给 run_fetch(接线断了会静默用
    默认值,--include/--overwrite 悄悄失效比报错糟得多)。"""
    from curation import cli, fetch as fetch_mod
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    got = {}

    def fake_run(cfg, source, ref, **kw):
        got.update(kw, source=source, ref=ref, cfg=cfg)
        return 0
    monkeypatch.setattr(fetch_mod, "run_fetch", fake_run)
    rc = cli.main(["fetch", "--source", "ai-infra", "--ref", "libero",
                   "--include", "meta/*", "--include", "data/*",
                   "--into", str(tmp_path), "--name", "lb", "--overwrite"])
    assert rc == 0
    assert got["source"] == "ai-infra" and got["ref"] == "libero"
    assert got["includes"] == ["meta/*", "data/*"]
    assert got["into"] == str(tmp_path) and got["name"] == "lb"
    assert got["overwrite"] is True
    assert "checks" in got["cfg"], "配置要走 load_config(含出厂默认)"


def _fetch_help() -> str:
    from curation.cli import build_parser
    # argparse 子命令的 help 要从 subparser 上拿;从 _subparsers action 里翻出来
    p = build_parser()
    for action in p._actions:
        if hasattr(action, "choices") and action.choices and "fetch" in action.choices:
            return action.choices["fetch"].format_help()
    raise AssertionError("没找到 fetch 子命令")


def test_help_names_oniond_in_source_and_no_other_jargon():
    """帮助措辞两条纪律一起钉:① --source 的说明里**要**点名 oniond(界面语义化、
    实现照实交代,用户得知道 ai-infra 走的是哪条通道);② 除此之外不许出现内部
    黑话 —— M 编号(m4a 之类,同「编号不进 CLI」纪律)、aria2、BUCKET 这类
    实现细节都不进帮助。"""
    text = _fetch_help()
    assert "oniond" in text
    assert "ai-infra" in text
    import re
    assert not re.search(r"\b[Mm]\d+[a-c]?\b", text), "M 编号不进 CLI"
    assert "aria2" not in text
    assert "BUCKET" not in text


# ── 分批下载 + 下载/入库重叠(2026-08-18 改造)──────────────────────────

def _six_files(n_bytes: int = 100) -> dict[str, bytes]:
    """六个等大(100 B)的文件,配 CURATION_FETCH_BATCH_BUDGET=200 正好切成
    3 批 × 2 个:批数、每批成员全部可预测,断言不用猜。"""
    info = json.dumps({"features": {"action": {"dtype": "float32"}}})
    files = {"meta/info.json": (info + " " * (n_bytes - len(info))).encode()}
    for c in "abcde":
        files[f"data/{c}.parquet"] = b"x" * n_bytes
    return files


def _corrupt_download_of(monkeypatch, relpath: str):
    """让假下载器把某个文件写残(截短)—— 模拟传输半途断掉的批。"""
    inner = fetch.download_dataset

    def bad(source, ref, staging, includes):
        inner(source, ref, staging, includes)
        p = os.path.join(staging, ref, relpath)
        if os.path.exists(p):
            with open(p, "wb") as fp:
                fp.write(b"shrt")
    monkeypatch.setattr(fetch, "download_dataset", bad)


def test_batches_deleted_after_copy_keeps_local_peak_at_two(tmp_path,
                                                            monkeypatch, capsys):
    """★ 双缓冲的全部意义:本地盘峰值只有约 2 批,不是整个数据集 —— 容器
    ephemeral-storage 只有 30Gi,libero(32.5 GiB)一次性暂存必被拒,分批+
    拷完即删才装得下。每批开下载时清点暂存里还活着的批目录:除自己外最多 1 个
    (第 N+2 批开下前第 N 批必须已删);跑完后暂存整个清空。
    进度输出要有「第 N/M 批」(任务台 UI 靠它显示进度)。"""
    cfg = _cfg(tmp_path)
    files = _six_files()
    calls = _wire(monkeypatch, files)
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    peers_alive = []
    inner = fetch.download_dataset

    def spy(source, ref, staging, includes):
        parent = os.path.dirname(staging)
        peers_alive.append(len([d for d in os.listdir(parent)
                                if d.startswith("batch-")
                                and os.path.join(parent, d) != staging]))
        inner(source, ref, staging, includes)
    monkeypatch.setattr(fetch, "download_dataset", spy)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert len(calls["download"]) == 3
    assert max(peers_alive) <= 1, \
        f"暂存里同时存在的批目录超过 2 个(含在下的),拷完即删被拆了:{peers_alive}"
    out = capsys.readouterr().out
    assert "第 1/3 批" in out and "第 3/3 批" in out
    assert not any((tmp_path / "staging").iterdir()), "成功后暂存必须清空"


def test_download_overlaps_with_copy(tmp_path, monkeypatch):
    """★ 重叠确实发生:批 2 的下载要在批 1 的拷贝**完成之前**就开始(实测重叠
    省约 40%,数据在 fetch.py 模块头注释;这条防的是有人觉得"重叠没必要"改回
    串行)。

    验证手法:批 1 的本地清理(=拷贝完成的最后一步)卡在一个事件上,事件由
    "批 2 下载已开始"触发(10 秒超时)。并行实现毫秒级放行,事件必然先于清理
    被记录;改回串行后同一个线程只能先拷完清理、再开批 2 下载 —— 等满超时后
    顺序颠倒,本用例变红(把 run_fetch 里的拷贝线程改成逐批同步调用实跑验证过
    一次,确实红在最后这条断言上)。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, _six_files())
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    seq = []
    second_dl_started = threading.Event()
    n_dl = {"n": 0}
    inner_dl = fetch.download_dataset

    def dl_spy(source, ref, staging, includes):
        n_dl["n"] += 1
        seq.append(("dl", n_dl["n"]))
        if n_dl["n"] == 2:
            second_dl_started.set()
        inner_dl(source, ref, staging, includes)
    monkeypatch.setattr(fetch, "download_dataset", dl_spy)
    real_rm = fetch.remove_local_batch

    def rm_spy(path):
        if os.path.basename(path) == "batch-0001":
            second_dl_started.wait(timeout=10)
        seq.append(("rm", os.path.basename(path)))
        real_rm(path)
    monkeypatch.setattr(fetch, "remove_local_batch", rm_spy)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert ("dl", 2) in seq and ("rm", "batch-0001") in seq
    assert seq.index(("dl", 2)) < seq.index(("rm", "batch-0001")), \
        "批 2 的下载必须在批 1 拷贝完成之前开始 —— 重叠被拆回串行了"


def test_second_batch_failure_keeps_first_and_marker_names_breakpoint(
        tmp_path, monkeypatch, capsys):
    """第 2 批坏 → 当场停,失败现场按规矩保全:第 1 批已入库的文件**留在目标**
    (下次文件级续传直接跳过,比"全删"省一遍下载);哨兵没写(数据集清单不会把
    半份数据列成完整数据集,不存在误用);进行中标记改写成失败记录,写明断在
    第 2/3 批;失败那批的本地暂存保留供排查。"""
    cfg = _cfg(tmp_path)
    files = _six_files()
    _wire(monkeypatch, files)
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    _corrupt_download_of(monkeypatch, "data/c.parquet")
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "第 2/3 批" in err and "大小不符" in err
    target = tmp_path / "datasets" / "x"
    assert (target / "data" / "a.parquet").exists(), "第 1 批的成果不许陪葬"
    assert (target / "data" / "b.parquet").exists()
    assert not (target / "meta" / "info.json").exists(), "哨兵不许写"
    marker = json.loads(
        (tmp_path / "datasets" / ".fetching-x.json").read_text())
    assert marker["failed_at_batch"] == 2 and marker["batches_total"] == 3
    kept = list((tmp_path / "staging").glob("*/batch-0002"))
    assert kept, "失败那批的暂存要保留供排查"


def test_rerun_after_failure_skips_files_already_in_target(tmp_path,
                                                           monkeypatch, capsys):
    """重跑按**文件**续传,不按批:上一跑已入库的文件(目标目录有、大小与源站
    HEAD 一致)直接跳过,只下还缺的 —— 批次边界是执行细节,批预算一调"第几批"
    就全变,所以进度的唯一可信来源是目标目录本身,失败标记里的批号只是给人看。
    上一跑留下的失败标记不算占用,不许把重跑挡 12 小时。"""
    cfg = _cfg(tmp_path)
    files = _six_files()
    _wire(monkeypatch, files)
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    _corrupt_download_of(monkeypatch, "data/c.parquet")
    assert _run(cfg, tmp_path=tmp_path) == 1        # 复刻断在第 2 批的现场

    calls = _wire(monkeypatch, files)               # 重新接线:下载不再写残
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    rc = _run(cfg, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    downloaded = [f for c in calls["download"] for f in c["includes"]]
    assert set(downloaded) == {"data/c.parquet", "data/d.parquet",
                               "data/e.parquet", "meta/info.json"}, \
        "第 1 批的文件必须被文件级续传跳过,缺的全要补上"
    assert "跳过" in out
    assert (tmp_path / "datasets" / "x" / "meta" / "info.json").exists()
    assert not (tmp_path / "datasets" / ".fetching-x.json").exists(), \
        "补完收官,失败记录要清掉"


def test_copier_exception_stops_main_and_reports(tmp_path, monkeypatch, capsys):
    """拷贝线程抛异常必须传回主线程、停止后续批并如实报错 —— 吞掉就是
    "下载全绿、一半没入库"的静默事故;失败记录也要写明断在第几批。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, _six_files())
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    real_pub = fetch.publish_file

    def boom(src, dst):
        if dst.endswith("a.parquet"):
            raise RuntimeError("FSX 写炸了(测试注入)")
        return real_pub(src, dst)
    monkeypatch.setattr(fetch, "publish_file", boom)
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "FSX 写炸了" in err and "第 1/3 批" in err
    assert not (tmp_path / "datasets" / "x" / "meta" / "info.json").exists()
    marker = json.loads(
        (tmp_path / "datasets" / ".fetching-x.json").read_text())
    assert marker["failed_at_batch"] == 1


def test_batch_budget_formula():
    """② 批预算 = min(8 GiB, 可写/3):除以 3 = 在途两批(一批在下、一批在拷)
    + 一份安全余量;8 GiB 上限让 libero(32.5 GiB)分 5 批、双缓冲峰值 16 GiB,
    30Gi 配额装得下。"""
    G = 1024 ** 3
    assert fetch.batch_budget(100 * G) == 8 * G
    assert fetch.batch_budget(9 * G) == 3 * G


def test_oversized_file_gets_own_batch_and_file_count_caps_a_batch():
    """④ 单个文件比批预算大:自己独占一批(不拒、不死循环),顺序不乱;
    文件数到 50 也切一刀 —— oniond 一次 50 个精确路径 --include 实测可用
    (2026-08-18),更大的量级没验过,不无脑放大。"""
    sizes = {"a": 50, "big": 500, "b": 50}
    assert fetch.plan_batches(["a", "big", "b"], sizes, 100) == \
        [["a"], ["big"], ["b"]]
    many = [f"f{i:03d}" for i in range(120)]
    batches = fetch.plan_batches(many, {f: 1 for f in many}, 10 ** 9)
    assert [len(b) for b in batches] == [50, 50, 20]


def test_capacity_uses_ephemeral_limit_not_df(tmp_path, monkeypatch, capsys):
    """★ 防 pod 被 kubelet 驱逐(2026-08-18 生产 pod 实测):df/disk_usage 报的
    是**节点整块盘**(2.2 TiB),而容器 ephemeral-storage limit(30Gi)是 kubelet
    按用量强制的,不是文件系统配额 —— 只读 df 当预算,大数据集会把 pod 写到被
    驱逐(pod 直接消失,日志无痕),比拒绝下载严重得多。
    注入限额 20 GiB、df 假件报 2 TiB:预算必须按限额侧算(20 − 2 安全余量 =
    18 GiB),19 GiB 的单文件要被拒,报文说清三个数和依据。只读 df 的写法下
    19 GiB ≪ 2 TiB 会放行 → 本用例变红(把 staging_capacity 改成只回 df
    实跑验证过一次,确实红)。"""
    cfg = _cfg(tmp_path)
    G = 1024 ** 3
    calls = _wire(monkeypatch, {"data/huge.parquet": b""},
                  head_sizes={"data/huge.parquet": 19 * G}, free=2048 * G)
    monkeypatch.setenv("CURATION_EPHEMERAL_LIMIT_BYTES", str(20 * G))
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "19.00 GiB" in err and "18.00 GiB" in err and "1.00 GiB" in err
    assert "限额" in err, "报文要说清依据,不然用户看 df 有 2 TiB 会以为我们算错"
    assert not calls["download"]


def test_capacity_without_limit_env_is_conservative_and_says_so(tmp_path,
                                                                monkeypatch):
    """限额环境变量缺失时不许静默退回"df 说多少是多少"(那正是驱逐坑本身):
    按保守缺省 10 GiB 与 df 取小,并在依据里明说没拿到限额、该去部署里注入。"""
    monkeypatch.delenv("CURATION_EPHEMERAL_LIMIT_BYTES", raising=False)
    monkeypatch.setattr(fetch, "free_bytes", lambda p: 2048 * 1024 ** 3)
    avail, basis = fetch.staging_capacity(str(tmp_path))
    assert avail == fetch.DEFAULT_EPHEMERAL_LIMIT
    assert "未注入" in basis and "CURATION_EPHEMERAL_LIMIT_BYTES" in basis


# ── 哨兵不参与续传 + 摘除开工后冒出的哨兵(2026-08-18 libero 实机事故)────

def _video_declared_but_missing() -> dict[str, bytes]:
    """info.json 声明 dtype video 但清单里一个 mp4 都没有 —— 整体校验必失败,
    专门用来构造"全部批都入库了、最后一关不过"的现场。"""
    info = json.dumps({"features": {
        "observation.images.top": {"dtype": "video"},
        "action": {"dtype": "float32"},
    }})
    files = {"meta/info.json": (info + " " * (100 - len(info))).encode()}
    for c in "abcde":
        files[f"data/{c}.parquet"] = b"x" * 100
    return files


#: 让 meta/info.json 落在第 2/3 批(中间批):批预算 200、两个 data 文件一批,
#: 清单顺序把 info.json 夹在中间。
_MIDDLE_BATCH_ORDER = ["data/a.parquet", "data/b.parquet", "meta/info.json",
                       "data/c.parquet", "data/d.parquet", "data/e.parquet"]


def test_stray_sentinel_is_removed_not_resumed_and_report_matches_facts(
        tmp_path, monkeypatch, capsys):
    """★ 回归 2026-08-18 oniond-p0 libero 实机事故(32.5 GiB 分 10 批,真跑复现):
    ① 判存在之后、续传扫描之前,目标目录里冒出了 meta/info.json(现场是并发的
    `--include "meta/*"` 部分拉取在开工后 18 秒按"哨兵最后写"发布了它;FSX
    可见性延迟也能造出同一窗口)。旧实现把它当"已下好"跳过 —— 它不进任何批、
    不被押后,整体校验报"缺失"而它本尊躺在目标目录里(报文与事实矛盾);更糟:
    校验失败、拒绝入库,**哨兵却在** —— 半成品挂着"可入住"牌子被 list_datasets
    列成有效数据集,用户点进去跑质检读到的是没过校验的数据。

    修后四条一起钉:①哨兵不在目标目录;②报文不许再喊"缺失"(真正的失败原因
    要点名);③已入库的批留着;④标记留着并写清断点。"""
    cfg = _cfg(tmp_path)
    files = _video_declared_but_missing()
    _wire(monkeypatch, files, inspect_files=_MIDDLE_BATCH_ORDER)
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    target = tmp_path / "datasets" / "x"
    real_measure = fetch.measure_sizes

    def measure_and_drop_stray(source, ref, names):
        # 复刻现场:哨兵在 ① 之后、④ 之前落进目标目录(HEAD 核对体积正好在
        # 这个窗口里),大小与源站一致 —— 旧的续传逻辑会把它当"已下好"
        p = target / "meta" / "info.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(files["meta/info.json"])
        return real_measure(source, ref, names)
    monkeypatch.setattr(fetch, "measure_sizes", measure_and_drop_stray)

    rc = _run(cfg, tmp_path=tmp_path)
    captured = capsys.readouterr()
    assert rc == 1
    # ① 硬性不变量:整体校验没过,哨兵绝不许出现在目标目录里
    assert not (target / "meta" / "info.json").exists(), \
        "校验失败还留着哨兵 = 半成品被数据集清单列成有效数据集"
    # ② 报文与事实一致:info.json 拉到了(被押后了),不许再喊"缺失";
    #    真正的失败原因(声明了 video 却没有视频)要点名
    assert "缺失" not in captured.err
    assert "视频" in captured.err
    # 摘哨兵这件事要向用户交代(静默动目标目录里的文件=下一个谜团)
    assert "哨兵" in captured.out and "摘" in captured.out
    # ③ 已入库的批留着(下次文件级续传接着用)
    assert (target / "data" / "a.parquet").exists()
    # ④ 标记留着并写清断点
    marker = json.loads((tmp_path / "datasets" / ".fetching-x.json").read_text())
    assert marker["failed_stage"] == "整体校验"


def test_sentinel_lands_last_even_from_a_middle_batch(tmp_path, monkeypatch):
    """成功路径自查:分批之后哨兵仍然是**最后一个**落地的文件 —— 哪怕它被
    下载在中间那批里(押后暂存机制被拆掉的话,它会在第 2/3 批就落地,边拷边
    打开界面的人就会点进一个后半截还在路上的数据集)。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, _six_files(), inspect_files=_MIDDLE_BATCH_ORDER)
    monkeypatch.setenv("CURATION_FETCH_BATCH_BUDGET", "200")
    order = []
    real = fetch.publish_file

    def spy(src, dst):
        order.append(dst)
        return real(src, dst)
    monkeypatch.setattr(fetch, "publish_file", spy)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert len(order) == 6
    assert order[-1].endswith(os.path.join("meta", "info.json"))


# ── --into 指本地路径:落地全量也算进预算(2026-08-18 pod 被驱逐事故)────

def test_same_filesystem_detects_same_device(tmp_path):
    """接缝本体的直测:同一 tmp 盘下两个目录必须判同盘(生产上 /mnt/tos 与
    本地盘 st_dev 不同,单测没法造,只测同盘方向)。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert fetch.same_filesystem(str(a), str(b))


def test_local_into_counts_landing_bytes_against_budget(tmp_path, monkeypatch,
                                                        capsys):
    """★ 防 pod 被写死(2026-08-18 真事故):--into 指到 /tmp(与暂存同盘)下
    libero,落地的 32.5 GiB 也算进容器 ephemeral-storage 配额,"暂存 + 落地"
    两份一起把 pod 写到被 kubelet 驱逐 —— 而旧预检只算暂存、不算落地。
    同盘时按严口径:落地全量 + 暂存至少装下最大单文件 > 预算 → 当场拒绝,
    报文说清"同一个文件系统,总量一起算"和差多少;落地在挂载上(不同盘,
    生产常态)同一组数照旧放行。"""
    cfg = _cfg(tmp_path)
    files = _six_files()                  # 6 × 100 B,落地全量 600 B
    calls = _wire(monkeypatch, files, free=250)   # 单文件 100 ≤ 250,旧预检放行
    monkeypatch.setattr(fetch, "same_filesystem", lambda a, b: True)
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "同一个文件系统" in err and "还差" in err
    assert not calls["download"], "预检拒了就不许再去下载"

    # 现状对照:不同文件系统(生产常态)同一组数必须照旧放行
    calls = _wire(monkeypatch, files, free=250)
    assert _run(cfg, tmp_path=tmp_path) == 0
    assert calls["download"]


# ── 部分拉取不发布哨兵(钉死 2026-08-18 事故的因果链源头)────────────────

def test_partial_fetch_never_publishes_sentinel_then_full_fetch_proceeds(
        tmp_path, monkeypatch, capsys):
    """★ 2026-08-18 libero 事故的整条因果链,根子在"部分拉取发布了哨兵":
    `--include "meta/*"` 落地了 info.json → 后续全量拉取的续传把它当"已下好"
    跳过(旧设计里 ① 判存在还会直接说"已入库,跳过") → 半成品自称可用数据集。
    哨兵的含义是"完整可用",部分拉取按定义不完整 —— 发哨兵就是撒谎:meta 声明
    1693 条 episode、盘上只有几个文件,质检读到不存在的 episode 就会炸。

    钉两段:①部分拉取后目标目录**没有**哨兵、其它命中文件照常落地、有说人话的
    说明;②紧接着的全量拉取**不被跳过**、正常完成并补齐哨兵 —— 部分拉取不再
    给全量拉取埋雷。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    rc = _run(cfg, tmp_path=tmp_path, includes=["meta/*"])
    out = capsys.readouterr().out
    target = tmp_path / "datasets" / "x"
    assert rc == 0
    assert not (target / "meta" / "info.json").exists(), \
        "部分拉取发布哨兵 = 半成品自称可用数据集(事故因果链的源头)"
    assert (target / "meta" / "tasks.parquet").exists(), "命中的其它文件照常落地"
    assert "部分拉取" in out and "不会被当作可用数据集列出" in out

    # 紧接全量拉取:不许被"已有 meta/info.json,跳过"挡住,要正常补齐收官
    calls = _wire(monkeypatch, GOOD_FILES)
    rc = _run(cfg, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "跳过(要重下加 --overwrite)" not in out, \
        "部分拉取的残留把全量拉取挡住了 —— 用户没法把数据集补齐"
    assert calls["download"], "缺的文件要真的去下"
    sentinel = target / "meta" / "info.json"
    assert sentinel.exists()
    assert "features" in json.loads(sentinel.read_text()), "补齐后哨兵是真 info.json"


# ── 进行中标记带 run_id:收尾只动自己的标记 ─────────────────────────────

def test_finishing_run_does_not_delete_someone_elses_marker(tmp_path,
                                                            monkeypatch, capsys):
    """★ 2026-08-18 事故帮凶:两个运行写同名标记互相覆盖,先完成的把并发运行的
    标记也顺手删了 —— 第三个运行会误判"没人在下"闯进来一起写,标记在并发时
    反而帮了倒忙。改法:标记带 run_id,收尾删/改之前先读回来认领,不是自己写的
    就不动、只警告(它仍然不是锁 —— 对象存储没有原子创建,这里只是把"静默互相
    覆盖"降级成"大概率能发现并说明")。
    场景:A 在跑,B 中途覆盖了标记(模拟并发接管);A 完成时必须留下 B 的标记
    并警告,而不是把 B 的占用一擦了之。"""
    cfg = _cfg(tmp_path)
    _wire(monkeypatch, GOOD_FILES)
    marker = tmp_path / "datasets" / ".fetching-x.json"
    inner = fetch.download_dataset

    def hijack(source, ref, staging, includes):
        inner(source, ref, staging, includes)
        marker.write_text(json.dumps({
            "host": "peer-pod-abc", "ref": "x", "run_id": "peer-run-id",
            "started_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds")}))
    monkeypatch.setattr(fetch, "download_dataset", hijack)
    rc = _run(cfg, tmp_path=tmp_path)
    err = capsys.readouterr().err
    assert rc == 0, "本次拉取本身是成功的,标记冲突只警告不翻车"
    assert marker.exists(), "别人的标记不许被顺手删掉"
    assert json.loads(marker.read_text())["run_id"] == "peer-run-id", \
        "留下的必须是别人的标记原样,不许改写"
    assert "另一个运行" in err and "并发" in err, "要明确警告可能有并发写入"
