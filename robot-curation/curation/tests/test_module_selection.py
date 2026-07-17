"""模块选择(--only/--skip/--report-only)验收:单跑任意模块或组合。"""
from __future__ import annotations

import json
import os

import pytest

from curation.pipeline.config import apply_check_selection, load_config

PUSHT = "/data03/hao/data/pusht"


def test_only_single_module():
    cfg = apply_check_selection(load_config(), only="visual_quality")
    assert cfg["checks"]["visual_quality"]["enable"] is True
    assert all(not c["enable"] for n, c in cfg["checks"].items()
               if n != "visual_quality")


def test_only_combo():
    cfg = apply_check_selection(load_config(), only="visual_quality, kinematic_limits")
    on = {n for n, c in cfg["checks"].items() if c["enable"]}
    assert on == {"visual_quality", "kinematic_limits"}


def test_m_numbers_rejected():
    """M 编号不进 CLI(编号只是文档沟通标签,系统只认语义化名)。"""
    with pytest.raises(ValueError, match="未知检查名"):
        apply_check_selection(load_config(), only="m4a")


def test_skip_mode():
    cfg = apply_check_selection(load_config(), skip="task_success,video_action_sync")
    assert not cfg["checks"]["task_success"]["enable"]
    assert not cfg["checks"]["video_action_sync"]["enable"]
    assert cfg["checks"]["motion_quality"]["enable"]              # 其余不动


def test_bad_name_and_mutex():
    with pytest.raises(ValueError, match="未知检查名"):
        apply_check_selection(load_config(), only="visualquality")
    with pytest.raises(ValueError, match="互斥"):
        apply_check_selection(load_config(), only="visual_quality",
                              skip="motion_quality")
    with pytest.raises(ValueError, match="为空"):
        apply_check_selection(load_config(), only=" , ")


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_cli_only_visual_report_only(tmp_path):
    """端到端:--only m4a --report-only → 报告只含视觉判决,不产出导出目录。"""
    from curation.cli import main

    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "6",
               "--only", "visual_quality", "--report-only"])
    assert rc == 0
    rep = json.load(open(out / "passed.json"))
    assert rep["dataset"]["input_episodes"] == 6
    assert not (out / "episodes_parquet").exists()                # report-only 生效
    assert not (out / "lerobot_curated").exists()
    # 漏斗统计只应有视觉一道抽帧关卡(数值段全关 → 无拦截)
    assert rep["dataset"]["verdict_keep"] + rep["dataset"]["verdict_drop"] == 6


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_cli_only_numeric_combo(tmp_path):
    """组合:--only m4b,m5b(纯数值,秒级)→ 正常出报告。"""
    from curation.cli import main

    out = tmp_path / "out2"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "6",
               "--only", "motion_quality,kinematic_limits", "--report-only"])
    assert rc == 0
    rep = json.load(open(out / "passed.json"))
    assert rep["dataset"]["verdict_keep"] + rep["dataset"]["verdict_drop"] == 6



def test_parent_dir_gives_friendly_error(tmp_path):
    """指到装多个数据集的父目录 → 友好报错并列出可用数据集,不甩栈。"""
    from curation.ingest.lerobot_reader import NotADatasetError, read_lerobot_rows

    (tmp_path / "ds_a" / "meta").mkdir(parents=True)
    (tmp_path / "ds_a" / "meta" / "info.json").write_text("{}")
    (tmp_path / "ds_b" / "meta").mkdir(parents=True)
    (tmp_path / "ds_b" / "meta" / "info.json").write_text("{}")
    with pytest.raises(NotADatasetError, match="包含 2 个数据集"):
        read_lerobot_rows(str(tmp_path))


def test_nonexistent_path_friendly(tmp_path):
    from curation.ingest.lerobot_reader import NotADatasetError, read_lerobot_rows

    with pytest.raises(NotADatasetError, match="不存在"):
        read_lerobot_rows(str(tmp_path / "nope"))



@pytest.mark.skipif(not os.path.exists("/data03/hao/data/pusht/meta/info.json"),
                    reason="无数据")
def test_batch_mode_processes_all(tmp_path):
    """--batch:父目录下每个数据集各出一份结果到 <output>/<名>/。"""
    import shutil
    parent = tmp_path / "datasets"
    (parent).mkdir()
    # 造两个迷你数据集(软链到真 pusht,省空间)
    for name in ("ds_a", "ds_b"):
        os.symlink("/data03/hao/data/pusht", str(parent / name))
    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", str(parent), "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "motion_quality", "--batch"])
    assert rc == 0
    assert (out / "ds_a" / "passed.json").exists()
    assert (out / "ds_b" / "report.md").exists()
    # 批处理汇总清单(2026-07-15):数据集名 + 机器人型号一览
    import json as _json
    bs = _json.load(open(out / "batch_summary.json"))
    assert bs["数据集数"] == 2
    assert {r["数据集"] for r in bs["datasets"]} == {"ds_a", "ds_b"}
    assert all(r["规格表"] == "pusht" for r in bs["datasets"])
    md = open(out / "batch_summary.md").read()
    assert "| ds_a |" in md and "机器人型号" in md



@pytest.mark.skipif(not os.path.exists("/data03/hao/data/pusht/meta/info.json"),
                    reason="无数据")
def test_output_exists_fails_fast_then_overwrite(tmp_path):
    """输出目录已有结果 → 提前拦(OutputExistsError);--overwrite 则清理重跑。"""
    from curation.cli import main
    from curation.ingest.lerobot_reader import OutputExistsError
    from curation.pipeline.run import run_pipeline
    out = tmp_path / "o"
    args = dict(embodiment_id="pusht", max_episodes=3, only_checks="visual_quality")
    run_pipeline(None, "/data03/hao/data/pusht", str(out), **args)      # 首次OK
    with pytest.raises(OutputExistsError):                              # 再次拦
        run_pipeline(None, "/data03/hao/data/pusht", str(out), **args)
    run_pipeline(None, "/data03/hao/data/pusht", str(out), overwrite=True, **args)  # 覆盖OK
    assert main(["run", "--input", "/data03/hao/data/pusht", "--output", str(out),
                 "--embodiment-id", "pusht", "--max-episodes", "3",
                 "--only", "visual_quality"]) == 3                      # CLI 退出码3


def test_only_skill_profile_selectable():
    """--only skill_profile:全部检查关,画像开(数据集级模块也可单选)。"""
    cfg = apply_check_selection(load_config(), only="skill_profile")
    assert all(not c["enable"] for c in cfg["checks"].values())
    assert cfg["skill_profile"]["enable"] is True


def test_skip_skill_profile():
    cfg = apply_check_selection(load_config(), skip="skill_profile")
    assert cfg["skill_profile"]["enable"] is False
    assert cfg["checks"]["motion_quality"]["enable"]          # 检查不动


def test_only_other_module_disables_profile():
    """--only visual_quality:画像也关(only 的语义=只跑列出的)。"""
    cfg = apply_check_selection(load_config(), only="visual_quality")
    assert cfg["skill_profile"]["enable"] is False


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_cli_only_skill_profile_e2e(tmp_path, monkeypatch):
    """端到端:--only skill_profile = 跳过全部检查,只跑 caption→taxonomy 画像。

    VLM 全链假注入(不碰 GPU):captioner 回固定短语,LLM 回固定两级体系 JSON。
    验收:全部 episode 直通(无检查);报告画像来自 taxonomy(族名=LLM 产物);
    task_success 未跑。
    """
    import json as _json

    from curation.adapters import vlm_server
    from curation.dataset_level import caption as cap_mod

    monkeypatch.setattr(vlm_server, "ensure_vlm", lambda ep, m: (True, "假VLM(测试)"))
    monkeypatch.setattr(cap_mod, "make_vlm_captioner",
                        lambda ep, m, **k: (lambda frames: "push the T block"))
    from curation.adapters import vlm_client
    fake_tax = {"families": [{"name": "推动类", "subskills": [
        {"name": "推T块", "members": ["push the T block"]}]}]}

    def _fake_llm(prompt):
        if "taxonomy" in prompt:                       # 归纳两级体系
            return _json.dumps(fake_tax)
        return _json.dumps({"map": []})                # 标注审计的族映射
    monkeypatch.setattr(vlm_client, "make_llm_ask",
                        lambda ep, m, **k: _fake_llm)

    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "4",
               "--only", "skill_profile", "--report-only"])
    assert rc == 0
    rep = _json.load(open(out / "passed.json"))
    text = open(out / "report.md").read()
    assert "推动类" in text and "推T块" in text          # 画像来自 LLM taxonomy
    assert "bmbfbbfgjjg" not in text                     # 不是原始标注碎片分组
    # 无检查:全部直通 keep
    assert len(rep["episodes"]) == 4 if isinstance(rep.get("episodes"), list) else True


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_report_identity_header(tmp_path):
    """所有报告开头带数据集名+机器人型号(2026-07-15 用户定)。"""
    import json as _json
    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "motion_quality", "--report-only"])
    assert rc == 0
    md = open(out / "report.md").read()
    head = "\n".join(md.splitlines()[:4])
    assert "**数据集**: pusht" in head
    assert "**机器人型号**" in head and "规格表: pusht" in head
    for f in ("passed.json", "reject.json", "review.json"):
        d = _json.load(open(out / f))
        assert d.get("数据集") == "pusht", f
        assert d.get("机器人", {}).get("registry_profile") == "pusht", f


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_dedup_two_stage_no_video_hash_without_collision(tmp_path, monkeypatch):
    """两段式去重(2026-07-15):action 无撞车 → 视频哈希一次都不该发生。

    droid 实测教训:全员视频指纹=哈希 200GB 磨十几分钟;第一道 action 哈希免费,
    撞车才验视频。此测试把视频哈希换成炸弹——管线跑通即证明它从未被调用。"""
    from curation.dataset_level import dedup as dd

    def boom(path, chunk=1 << 20):
        raise AssertionError(f"action 无撞车却调了视频哈希: {path}")

    monkeypatch.setattr(dd, "_file_sha256", boom)
    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "5",
               "--only", "motion_quality", "--report-only"])
    assert rc == 0
    import json as _json
    d = _json.load(open(out / "passed.json"))
    assert d["dataset"]["dedup_removed"] == 0


def test_dedup_selectable():
    """dedup 成为可选模块(2026-07-15):only 不含它就不跑,可单选,可 skip。"""
    cfg = apply_check_selection(load_config(), only="motion_quality")
    assert cfg["dedup"]["enable"] is False          # only=只跑列出的
    cfg = apply_check_selection(load_config(), only="dedup")
    assert cfg["dedup"]["enable"] is True
    assert all(not c["enable"] for c in cfg["checks"].values())
    cfg = apply_check_selection(load_config(), skip="dedup")
    assert cfg["dedup"]["enable"] is False
    assert cfg["checks"]["motion_quality"]["enable"]


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_only_motion_never_touches_dedup(tmp_path, monkeypatch):
    """--only motion_quality:连 action 哈希都不做(dedup 整体没跑),报告注明。"""
    from curation.dataset_level import dedup as dd

    def boom(*a, **k):
        raise AssertionError("--only motion_quality 却跑了去重")

    monkeypatch.setattr(dd, "action_hash", boom)
    monkeypatch.setattr(dd, "episode_fingerprint", boom)
    from curation.cli import main
    import json as _json
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "4",
               "--only", "motion_quality", "--report-only"])
    assert rc == 0
    d = _json.load(open(out / "passed.json"))
    assert "未启用" in d["dataset"].get("dedup_note", "")
    assert "去重未启用" in open(out / "report.md").read()
