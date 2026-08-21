"""全局夹具。"""
import pytest


@pytest.fixture(autouse=True)
def _rrd_capability_on():
    """RRD 总开关默认关(2026-08-21 release 决定),但能力本身要一直保绿:测试里
    统一打开;"默认关时怎么表现"由 test_rrd_switch.py 自己设 False 覆盖。"""
    from curation.ingest import rrd_reader
    rrd_reader.set_enabled(True)
    yield
    rrd_reader.set_enabled(None)


@pytest.fixture(autouse=True)
def _clear_discover_cache():
    """discover_deliveries 带 5 秒 TTL 缓存(2026-08-20):同一进程里连跑的测试
    各自造交付目录,不清缓存会让后一条看到前一条的扫描结果。"""
    from curation.ui.manifest import clear_discover_cache
    clear_discover_cache()
    yield
    clear_discover_cache()
