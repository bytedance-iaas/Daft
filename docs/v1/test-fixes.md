# 搬运后修正的 v1 测试

v1 的单测随代码一起搬进了 `backend/curation/tests/`。下面几条在冻结点 `release_v1@45bdf9292` 上就不过
（2026-09-21 在冻结点源码上复现，Linux CI 与 macOS 结果相同），原因都在测试本身，被测代码一行没改。

| 测试 | 冻结点上为什么不过 | 改法 |
|---|---|---|
| `test_ui_manifest.py::test_task_verdict_survives_fsx_visibility_gap`、`::test_decision_survives_fsx_visibility_gap` | 两条都用「写一个空文件」模拟 FSX 的可见延迟。`dataset_level/decisions.py` 在 2026-09-04 把判据从「行数多者胜」改成「内容不同且文件比我们写盘更新 = 界面外改过，以磁盘为准」，空文件是后写的、mtime 更新，于是走进了「界面外改过」那条分支。能不能过全看两次写是否落在同一个时间戳刻度里，本来就不稳定 | 写空文件后把 mtime 回填成我们写盘时的值，模拟的才是「延迟窗口里读到旧版本」 |
| `test_vlm_watchdog.py::test_watchdog_real_process_integration` | 给子进程算 `PYTHONPATH` 时从 `curation/__init__.py` 多退了一级，子进程 import 不到 `curation`，看门狗根本没起来。v1 的开发机上 curation 用 pip 装进了环境，所以一直没暴露 | 改成 curation 包所在的目录 |

另有 `test_environment.py` 检查的是 GPU 主机（torch、nvidia-smi），本机和 CI 都没有，CI 里用 `--ignore` 跳过。

这三处修正要不要同步回 `release_v1`，由维护 v1 的同学决定；它们不影响任何质检结果。
