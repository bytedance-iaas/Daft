"""交付件落盘的写法防线:挂载直写产出的**坏文件必须被发现并重写**。

防的是 2026-08-14 e2e 抓到的那次事故:一次跑批结束、落盘回验已经报"交付就绪",
交付目录里的 passed.json 却是 **10853 字节全 `\\0`** —— save_report 当时是
`open(...,"w") + json.dump` 直写 TOS 的 FSX 挂载,而且一次跑批对同一路径写三遍,
最后那遍(落盘回验之后)把验过的好文件写坏了,全程零报错。同一堵墙此前已经咬过
savefig(零填充 PNG)、pyarrow(EPERM)、PyAV(无 moov 废 mp4)、任务台归档
status.json(164 字节全 `\\0`)。

用例用"写完立刻读是坏的"假文件系统层复现那种坏法:copyfile 被换成会把目标写成
零填充的桩,断言 ① 交付件走的是临时文件+copyfile(不是对着目标直写)②开了
verify 的那几遍会重写,最终落地的是能解析的好文件。
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from curation.export import safe_write
from curation.export.report import save_report
from curation.tests.test_report_wording import _report


class _FlakyMount:
    """假挂载:头 n_bad 次拷贝把目标写成零填充坏文件,之后正常。"""

    def __init__(self, n_bad: int = 1, only_suffix: str = ""):
        self.n_bad, self.only_suffix = n_bad, only_suffix
        self.calls: list[str] = []
        self._real = shutil.copyfile

    def __call__(self, src, dst, **kw):
        self.calls.append(str(dst))
        hit = not self.only_suffix or str(dst).endswith(self.only_suffix)
        if hit and self.n_bad > 0:
            self.n_bad -= 1
            with open(dst, "wb") as f:
                f.write(b"\0" * 128)          # 字节数看着正常,内容全零
            return dst
        return self._real(src, dst, **kw)


class _SlowMount:
    """假挂载:文件写进去了,但**读回来是 0 字节**(FSX 的可见性延迟,pod 实测
    要 30 秒上下才吐出真内容)。这是常态,不是故障。"""

    def __call__(self, src, dst, **kw):
        with open(dst, "wb") as f:
            f.write(b"")


def test_verified_write_rewrites_broken_file(tmp_path, monkeypatch, capsys):
    """开了 verify 的交付件:第一次落成零填充坏文件 → 回读发现 → 重写 → 最终可解析。"""
    flaky = _FlakyMount(n_bad=1)
    monkeypatch.setattr(safe_write.shutil, "copyfile", flaky)
    dst = str(tmp_path / "passed.json")
    safe_write.write_json(dst, {"数据集": "ds", "episodes": {}}, verify="json")

    assert len(flaky.calls) == 2, "坏文件必须触发一次重写"
    assert json.load(open(dst, encoding="utf-8"))["数据集"] == "ds"
    assert "读不回" not in capsys.readouterr().out, "重写成功就不该报警"


def test_permanently_broken_mount_warns_instead_of_silence(tmp_path, monkeypatch,
                                                           capsys):
    """挂载一直写坏:重写一次仍坏 → 留一行日志说清哪个文件,绝不静默、也不抛。"""
    monkeypatch.setattr(safe_write.shutil, "copyfile", _FlakyMount(n_bad=99))
    dst = str(tmp_path / "report.md")
    safe_write.write_text(dst, "# 报告", verify="text")
    out = capsys.readouterr().out
    assert "坏的" in out and "report.md" in out


def test_invisible_new_file_is_not_reported_as_an_accident(tmp_path, monkeypatch,
                                                           capsys):
    """★ 不许狼来了:新文件"暂时读不到"是这张挂载的常态(pod 实测 ~30s 才可见),
    不是坏文件 —— 不重写、不报警,交给轮询式落盘回验去确认。

    2026-08-14 e2e 里,把"读不到"也当故障的版本一次跑批刷了 8 行假警告。
    """
    slow = _SlowMount()
    monkeypatch.setattr(safe_write.shutil, "copyfile", slow)
    safe_write.write_json(str(tmp_path / "passed.json"), {"a": 1}, verify="json")
    assert capsys.readouterr().out == ""
    assert safe_write.content_state(str(tmp_path / "passed.json"),
                                    "json") == safe_write.STATE_UNSEEN


def test_rendering_goes_to_a_local_temp_file_not_the_target(tmp_path, monkeypatch):
    """渲染(json.dump/csv.writer 那些多次小写)必须落在本地临时文件上,
    交付路径只接一次整份 copyfile —— 挂载受不了的正是"库多次直写"。"""
    dst = str(tmp_path / "details" / "x.csv")
    copies: list = []
    real = shutil.copyfile
    monkeypatch.setattr(safe_write.shutil, "copyfile",
                        lambda s, d, **k: (copies.append((s, d)), real(s, d))[1])
    with safe_write.delivery_file(dst, newline="") as f:
        assert f.name != dst, "拿到的文件对象直指交付路径 = 又在直写挂载"
        f.write("a,b\n1,2\n")
    # 目标只被整份写一次 —— 但落点是**同目录下的临时名**,再改名到位(见下一条用例)
    assert len(copies) == 1
    staged = copies[0][1]
    assert staged != dst and os.path.dirname(staged) == os.path.dirname(dst)
    assert (tmp_path / "details" / "x.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"


def test_publish_is_an_atomic_rename_never_truncate_in_place(tmp_path, monkeypatch):
    """★ 发布必须是**原子改名**,不许对着目标就地截断再写。

    2026-08-14 第七次事故的根因:发布那步是 `copyfile(tmp, dst)`,也就是把目标
    截断再从头写 —— 存在一段"文件已经在那儿、内容还不是它"的中间态。同一个
    passed.json 一次跑批要写四遍(save_report 在 run.py 里被调 4 次)、重判再写
    两遍,中间态被下一遍撞上,文件就永久停在 **138719 字节全 `\\0`**。

    判据两条:拷贝的落点是同目录下的临时名(不是目标本身);目标是被 `os.replace`
    改名换上去的。改名在同一挂载内是原子的:读者要么看到旧的整份,要么看到新的整份。
    """
    renames: list = []
    real_replace = os.replace
    monkeypatch.setattr(safe_write.os, "replace",
                        lambda s, d: (renames.append((s, d)), real_replace(s, d))[1])
    dst = str(tmp_path / "passed.json")
    safe_write.write_json(dst, {"n": 1})
    assert len(renames) == 1, "发布没走改名"
    staged, final = renames[0]
    assert final == dst
    assert os.path.dirname(staged) == os.path.dirname(dst), "临时名必须与目标同目录"
    assert not os.path.exists(staged), "改名之后临时名不该还在"


def test_same_path_written_many_times_stays_intact(tmp_path):
    """★ 同一路径连写多遍后仍完整,且**每一遍中途都读得到完整的上一版**。

    这正是事故的形状:passed.json 在一次跑批里被写四遍。原来的发布方式下,
    第二遍开始写的瞬间目标就被截断了 —— 此刻的读者看到的是零填充。
    现在每遍都是"写好再改名",所以任何时刻读到的都是某一版的完整内容。
    """
    dst = str(tmp_path / "passed.json")
    seen: list = []

    class _Watcher:
        """每次改名之前偷看一眼目标现在是什么(模拟并发读者)。"""

        def __init__(self, real):
            self.real = real

        def __call__(self, src, d):
            if os.path.exists(d):
                seen.append(safe_write.content_state(d, "json"))
            return self.real(src, d)

    real_replace = os.replace
    safe_write.os.replace = _Watcher(real_replace)
    try:
        for i in range(4):
            safe_write.write_json(dst, {"轮次": i, "pad": "x" * 5000})
    finally:
        safe_write.os.replace = real_replace

    assert json.load(open(dst, encoding="utf-8"))["轮次"] == 3
    assert seen == [safe_write.STATE_OK] * 3, \
        f"中途读到过不完整的目标:{seen} —— 发布不是原子的"


def test_zero_filled_file_counts_as_corrupt_not_merely_absent(tmp_path):
    """★ 判据本身:零填充坏文件不许被"非空"放行 —— 事故当天它就是这么混过回验的。

    而且要判成 corrupt(等下去不会变好、该点名)而不是 unseen(还没可见、该继续等)。
    """
    p = tmp_path / "passed.json"
    p.write_bytes(b"\0" * 10853)
    assert safe_write.content_state(str(p), "json") == safe_write.STATE_CORRUPT
    assert safe_write.content_state(str(p), "text") == safe_write.STATE_CORRUPT
    assert safe_write.readable(str(p), "json") is False
    p.write_bytes(b"")                                   # 还没可见
    assert safe_write.content_state(str(p), "json") == safe_write.STATE_UNSEEN
    p.write_text("{截断的 json", encoding="utf-8")        # 半截 = 坏
    assert safe_write.content_state(str(p), "json") == safe_write.STATE_CORRUPT
    p.write_text('{"a": 1}', encoding="utf-8")
    assert safe_write.readable(str(p), "json") is True


def test_no_half_written_file_when_rendering_fails(tmp_path):
    """渲染中途抛异常:目标一个字节都不落 —— 半截交付件比没有更坏(读端当它是真的)。"""
    dst = str(tmp_path / "half.json")
    with pytest.raises(ValueError):
        with safe_write.delivery_file(dst) as f:
            f.write('{"a":')
            raise ValueError("渲染炸了")
    assert not (tmp_path / "half.json").exists()


# ───────── 目录级交付(episodes_parquet 这类"一个目录=一份数据集")─────────


def _dataset(d, **files):
    """最小"数据集目录":几个文件当 parquet 分片使。"""
    os.makedirs(d, exist_ok=True)
    for name, blob in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(blob)


def _listing(d):
    return {n: open(os.path.join(d, n), encoding="utf-8").read()
            for n in sorted(os.listdir(d))}


def test_dir_publish_never_touches_old_dataset_while_writing(tmp_path):
    """★ 目录级发布不许"先删后写"。

    2026-08-14 全零 passed.json 复盘时捎出来的同族隐患:rejudge 同步交付数据集
    是 `rmtree(pq_dir)` 再 `write_parquet(pq_dir)` —— 写失败 / 进程被杀 / pod
    被驱逐,客户真正拿走的那份数据集就**彻底没了**(连个坏文件都不剩)。
    判据:往 staging 写到一半的任意时刻,原目录读到的都还是旧内容整份。
    """
    from curation.export.safe_write import delivery_dir

    dst = str(tmp_path / "episodes_parquet")
    _dataset(dst, **{"part-0.parquet": "旧分片0", "part-1.parquet": "旧分片1"})
    with delivery_dir(dst) as staging:
        # staging 必须与目标同级 —— 同一挂载,rename 才是原子的
        assert os.path.dirname(staging) == str(tmp_path)
        _dataset(staging, **{"part-0.parquet": "新分片,只写了一半"})
        assert _listing(dst) == {"part-0.parquet": "旧分片0",
                                 "part-1.parquet": "旧分片1"}, \
            "新数据还没写完,原目录已经被动了 —— 这就是先删后写"
    assert _listing(dst) == {"part-0.parquet": "新分片,只写了一半"}


def test_dir_publish_failure_leaves_old_dataset_intact_and_no_litter(tmp_path):
    """写者抛异常 ⇒ 旧数据集一字未变,交付目录里也不留 staging 残骸。"""
    from curation.export.safe_write import delivery_dir

    dst = str(tmp_path / "episodes_parquet")
    _dataset(dst, **{"part-0.parquet": "旧分片"})
    with pytest.raises(RuntimeError):
        with delivery_dir(dst) as staging:
            _dataset(staging, **{"part-0.parquet": "写到一半"})
            raise RuntimeError("写炸了")
    assert _listing(dst) == {"part-0.parquet": "旧分片"}
    assert sorted(os.listdir(tmp_path)) == ["episodes_parquet"], \
        "staging 残骸留在交付目录里,客户 ls 会当成交付物"


def test_dir_publish_success_swaps_content_and_cleans_up(tmp_path):
    """成功发布:内容整体换新,交付目录里没有残留的 staging / 旧目录。"""
    from curation.export.safe_write import delivery_dir

    dst = str(tmp_path / "episodes_parquet")
    _dataset(dst, **{"part-0.parquet": "旧0", "part-1.parquet": "旧1"})
    with delivery_dir(dst) as staging:
        _dataset(staging, **{"part-0.parquet": "新0"})
    assert _listing(dst) == {"part-0.parquet": "新0"}, "旧分片不许混进新数据集"
    assert sorted(os.listdir(tmp_path)) == ["episodes_parquet"]


def test_dir_publish_works_for_first_time_export(tmp_path):
    """目标原本不存在(首次导出)也要能发布 —— 别把原子换位做成只会替换。"""
    from curation.export.safe_write import delivery_dir

    dst = str(tmp_path / "episodes_parquet")
    with delivery_dir(dst) as staging:
        _dataset(staging, **{"part-0.parquet": "首份"})
    assert _listing(dst) == {"part-0.parquet": "首份"}
    assert sorted(os.listdir(tmp_path)) == ["episodes_parquet"]


def test_save_report_survives_a_corrupting_mount(tmp_path, monkeypatch):
    """★ 事故复现:passed.json 第一次落成零填充 → verify 那遍必须把它救回来。"""
    monkeypatch.setattr(safe_write.shutil, "copyfile",
                        _FlakyMount(n_bad=1, only_suffix="passed.json"))
    save_report(_report(), str(tmp_path), verify=True)
    passed = json.loads((tmp_path / "passed.json").read_text(encoding="utf-8"))
    assert passed["数据集"] == "droid_batch"
    assert "ep000000" in passed["episodes"]          # 键名结构原样
    for name in ("reject.json", "review.json"):
        json.loads((tmp_path / name).read_text(encoding="utf-8"))
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("#")
