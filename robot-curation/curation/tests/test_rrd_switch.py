"""RRD 总开关(2026-08-21 用户定:release 只对外支持 LeRobot,RRD 默认关)。"""
from __future__ import annotations

import json

import pytest

from curation.ingest import rrd_reader


@pytest.fixture
def rrd_dir(tmp_path):
    d = tmp_path / "datasets" / "robo"
    d.mkdir(parents=True)
    (d / "ep0.rrd").write_bytes(b"RRD0")
    return d


def test_default_is_off_and_env_or_config_turns_on(monkeypatch):
    rrd_reader.set_enabled(None)
    monkeypatch.delenv("CURATION_RRD_ENABLED", raising=False)
    assert rrd_reader.rrd_enabled() is False
    monkeypatch.setenv("CURATION_RRD_ENABLED", "1")
    assert rrd_reader.rrd_enabled() is True
    rrd_reader.apply_config({"ingest": {"rrd_enabled": False}})
    assert rrd_reader.rrd_enabled() is True, "环境变量是运维显式覆盖,压过配置"
    monkeypatch.delenv("CURATION_RRD_ENABLED", raising=False)
    rrd_reader.apply_config({"ingest": {"rrd_enabled": True}})
    assert rrd_reader.rrd_enabled() is True
    rrd_reader.apply_config({"ingest": {}})          # 没写 = 不改
    assert rrd_reader.rrd_enabled() is True
    rrd_reader.apply_config({"ingest": {"rrd_enabled": False}})
    assert rrd_reader.rrd_enabled() is False


def test_default_yaml_ships_with_rrd_off():
    from curation.pipeline.config import load_config
    assert load_config(None)["ingest"]["rrd_enabled"] is False


def test_off_means_not_rrd_but_clear_message(rrd_dir):
    from curation.ingest.lerobot_reader import NotADatasetError, _load_info
    rrd_reader.set_enabled(False)
    assert rrd_reader.has_rrd_files(str(rrd_dir)) is True
    assert rrd_reader.is_rrd_dataset(str(rrd_dir)) is False
    with pytest.raises(NotADatasetError) as ei:
        _load_info(str(rrd_dir))
    assert "暂未开放" in str(ei.value) and "LeRobot v2/v3" in str(ei.value)
    rrd_reader.set_enabled(True)
    assert rrd_reader.is_rrd_dataset(str(rrd_dir)) is True


def test_off_hides_rrd_from_ui_lists_and_format(rrd_dir, tmp_path):
    from curation.ui import runner
    lerobot = tmp_path / "datasets" / "arm" / "meta"
    lerobot.mkdir(parents=True)
    (lerobot / "info.json").write_text(json.dumps({"codebase_version": "v2.1"}))
    rrd_reader.set_enabled(False)
    assert runner.list_datasets(str(tmp_path / "datasets")) == ["arm"]
    assert runner.dataset_format(str(rrd_dir))["kind"] == "unknown"
    assert runner.datasets_needing_clips(str(tmp_path / "datasets"), ["robo", "arm"]) == []
    rrd_reader.set_enabled(True)
    assert runner.list_datasets(str(tmp_path / "datasets")) == ["arm", "robo"]
    assert runner.dataset_format(str(rrd_dir))["kind"] == "rrd"


def test_off_batch_listing_and_run_dispatch(rrd_dir, tmp_path):
    from curation import cli
    rrd_reader.set_enabled(False)
    assert cli._list_datasets(str(tmp_path / "datasets")) == []
    rrd_reader.set_enabled(True)
    assert cli._list_datasets(str(tmp_path / "datasets")) == ["robo"]
