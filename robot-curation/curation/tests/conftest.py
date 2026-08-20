"""全局夹具。"""
import pytest


@pytest.fixture(autouse=True)
def _clear_discover_cache():
    """discover_deliveries 带 5 秒 TTL 缓存(2026-08-20):同一进程里连跑的测试
    各自造交付目录,不清缓存会让后一条看到前一条的扫描结果。"""
    from curation.ui.manifest import clear_discover_cache
    clear_discover_cache()
    yield
    clear_discover_cache()
