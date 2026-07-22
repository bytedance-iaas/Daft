"""P4.4 验收:一条命令出三件套;报告与统计对账;导出可回读。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import yaml

PUSHT = "/data03/hao/data/pusht"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pusht_needed = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载")


@pytest.fixture(scope="module")
def cli_output(tmp_path_factory):
    """跑一次真 CLI(子进程,pusht 前 10 条,--lite 精简版:测无VLM降级语义,不碰GPU)。"""
    from curation.pipeline.config import load_config

    cfg = load_config()
    cfg["checks"]["video_action_sync"]["params"]["corr_min"] = 0.2   # pusht 相关性天花板低
    cfg_path = str(tmp_path_factory.mktemp("cfg") / "pusht.yaml")
    yaml.dump(cfg, open(cfg_path, "w"), allow_unicode=True)

    out_dir = str(tmp_path_factory.mktemp("out") / "delivery")
    r = subprocess.run(
        [sys.executable, "-m", "curation.cli", "run",
         "--config", cfg_path, "--input", PUSHT, "--output", out_dir,
         "--embodiment-id", "pusht", "--max-episodes", "10", "--lite"],  # 降级语义=精简版
        capture_output=True, text=True, cwd=PROJECT_ROOT,
        env={**os.environ, "HF_HOME": "/data03/hao/.hf_home"})
    assert r.returncode == 0, f"CLI 失败:\n{r.stderr[-2000:]}"
    return out_dir, r.stdout


@pusht_needed
def test_three_deliverables_exist(cli_output):
    out_dir, stdout = cli_output
    assert os.path.exists(os.path.join(out_dir, "passed.json"))
    assert os.path.exists(os.path.join(out_dir, "report.md"))
    assert os.path.exists(os.path.join(out_dir, "episodes_parquet"))
    assert os.path.exists(os.path.join(out_dir, "lerobot_curated", "meta", "info.json"))
    assert "三件套" in stdout


@pusht_needed
def test_report_reconciles_with_deliverables(cli_output):
    out_dir, _ = cli_output
    rep = json.load(open(os.path.join(out_dir, "passed.json")))
    d = rep["dataset"]
    assert d["input_episodes"] == 10
    # 交付数 = lerobot_curated 里的 episode 数(对账)
    info = json.load(open(os.path.join(out_dir, "lerobot_curated", "meta", "info.json")))
    assert info["total_episodes"] == d["delivered"]
    # keep + drop = 漏斗幸存者数(中途硬门淘汰的不在 verdict 里)
    assert d["verdict_keep"] + d["verdict_drop"] == d["funnel_stats"]["output"]
    # M4c 无端点应留降级说明
    assert "task_success_note" in d


@pusht_needed
def test_exported_dataset_readable(cli_output):
    out_dir, _ = cli_output
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rep = json.load(open(os.path.join(out_dir, "passed.json")))
    back = read_lerobot_rows(os.path.join(out_dir, "lerobot_curated"))
    assert len(back) == rep["dataset"]["delivered"]


@pusht_needed
def test_skill_profile_present(cli_output):
    out_dir, _ = cli_output
    rep = json.load(open(os.path.join(out_dir, "passed.json")))
    assert rep["skills"]["n_episodes"] > 0
    assert rep["skills"]["n_skills"] >= 1              # pusht 单任务 → 1 类
    md = open(os.path.join(out_dir, "report.md")).read()
    assert "技能分布画像" in md

# ---------- --episodes(只跑指定 episode;2026-07-21) ----------
def test_parse_episodes_forms():
    """单条/多条/区间/混用 四种写法。"""
    from curation.cli import _parse_episodes

    assert _parse_episodes(None) is None
    assert _parse_episodes("") is None
    assert _parse_episodes("34") == {34}
    assert _parse_episodes("34,56,78") == {34, 56, 78}
    assert _parse_episodes("10-13") == {10, 11, 12, 13}
    assert _parse_episodes("3,10-12") == {3, 10, 11, 12}
    assert _parse_episodes(" 5 , 7 ") == {5, 7}          # 容忍空格


def test_parse_episodes_rejects_bad_input():
    from curation.cli import _parse_episodes

    for bad in ("abc", "1-", "-", "5-3"):                # 非数字/残缺/起止颠倒
        with pytest.raises(ValueError):
            _parse_episodes(bad)
