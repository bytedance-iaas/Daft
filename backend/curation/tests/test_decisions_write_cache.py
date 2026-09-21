"""裁决台账写缓存的取舍判据(2026-09-04 改:内容/时间比对取代"行数多者胜")。

防的事:①FSX 可见延迟——刚写完读回是空的/旧的,连裁两条不能把第一条冲掉;
②界面外的合法改动(删错裁/恢复旧台账/清空)不能被本进程的缓存永远盖住
(2026-09-04 倒回交付状态时实撞:磁盘行数少于缓存即被无视,直到 pod 重启)。
"""
from __future__ import annotations

import os
import time

from curation.dataset_level import decisions as dec

FIELDS = ["episode_id", "verdict", "note", "at"]


def _path(tmp_path):
    return str(tmp_path / "human-decisions" / "task_verdicts.csv")


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(dec._csv_text(FIELDS, rows))


def test_append_then_read_uses_disk_when_content_matches(tmp_path):
    p = _path(tmp_path)
    dec._append_row(p, FIELDS, {"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"})
    dec._append_row(p, FIELDS, {"episode_id": "ep2", "verdict": "判失败", "note": "", "at": "t"})
    assert [r["episode_id"] for r in dec._read_rows(p, FIELDS)] == ["ep1", "ep2"]


def test_missing_file_falls_back_to_cache(tmp_path):
    """FSX 还没让文件可见:读不到 → 用缓存(原行为保留)。"""
    p = _path(tmp_path)
    dec._append_row(p, FIELDS, {"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"})
    os.remove(p)
    assert [r["episode_id"] for r in dec._read_rows(p, FIELDS)] == ["ep1"]


def test_older_stale_content_falls_back_to_cache(tmp_path):
    """文件读回是旧内容(mtime 早于我们写盘)= 可见性延迟 → 缓存为准。"""
    p = _path(tmp_path)
    dec._append_row(p, FIELDS, {"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"})
    dec._append_row(p, FIELDS, {"episode_id": "ep2", "verdict": "判成功", "note": "", "at": "t"})
    _write(p, [{"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"}])   # 回退成旧的一行
    old = time.time() - 600
    os.utime(p, (old, old))
    assert [r["episode_id"] for r in dec._read_rows(p, FIELDS)] == ["ep1", "ep2"]


def test_newer_external_shrink_wins_over_cache(tmp_path):
    """界面外把台账改短(删掉一行)且文件比我们写盘更新 → 磁盘为准,缓存作废。
    这就是"行数多者胜"永远做不到的那一格。"""
    p = _path(tmp_path)
    dec._append_row(p, FIELDS, {"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"})
    dec._append_row(p, FIELDS, {"episode_id": "ep2", "verdict": "判成功", "note": "", "at": "t"})
    _write(p, [{"episode_id": "ep2", "verdict": "拿不准", "note": "外部改", "at": "t2"}])
    future = time.time() + 5
    os.utime(p, (future, future))
    rows = dec._read_rows(p, FIELDS)
    assert [(r["episode_id"], r["verdict"]) for r in rows] == [("ep2", "拿不准")]
    assert os.path.abspath(p) not in dec._WRITE_CACHE          # 作废,后续读直接看磁盘


def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    p = _path(tmp_path)
    dec._append_row(p, FIELDS, {"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"})
    _write(p, [])                                              # 外部清空,但 mtime 更早(模拟延迟)
    old = time.time() - 600
    os.utime(p, (old, old))
    assert len(dec._read_rows(p, FIELDS)) == 1                # 未过期:缓存兜底
    monkeypatch.setattr(dec, "_WRITE_CACHE_TTL_S", 0.0)
    assert dec._read_rows(p, FIELDS) == []                    # 过期:磁盘为准


def test_records_cache_signature_survives_new_cache_shape(tmp_path):
    """_RECORDS_CACHE 的签名读的是缓存行数,新结构下不能炸。"""
    p = _path(tmp_path)
    dec._append_row(p, FIELDS, {"episode_id": "ep1", "verdict": "判成功", "note": "", "at": "t"})
    ent = dec._WRITE_CACHE[os.path.abspath(p)]
    assert set(ent) >= {"rows", "at", "digest", "mtime_ns"} and len(ent["rows"]) == 1
