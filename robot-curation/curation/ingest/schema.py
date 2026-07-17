"""统一 DataFrame schema 定义 —— 单一事实源(文档中的 M1)。

列名/含义在此处以框架中立常量定义;daft 的 Schema 对象由 daft_schema() 惰性构建
(本模块顶层不 import daft,便于 core 层测试环境无 daft 也能引用列名)。
"""
from __future__ import annotations

# 列名常量(所有模块引用这里,不散落字符串)
EPISODE_ID = "episode_id"
EMBODIMENT_ID = "embodiment_id"
INSTRUCTION = "instruction"
ACTION = "action"            # 变长 [T, dim] tensor(小,存值)
PROPRIO = "proprio"          # struct{joint/gripper/ee_pose/velocity...},可空
VIDEO = "video"              # struct{cam_name: 路径字符串}(大,存指针懒加载)
TIMESTAMPS = "timestamps"    # 每通道时间戳
FPS = "fps"
RAW_EXTRAS = "raw_extras"    # 看不懂的列原样收留
PROVENANCE = "provenance"    # 字段来源溯源

ALL_COLUMNS = (
    EPISODE_ID, EMBODIMENT_ID, INSTRUCTION, ACTION, PROPRIO,
    VIDEO, TIMESTAMPS, FPS, RAW_EXTRAS, PROVENANCE,
)

# 必需列(缺失 → validate.py 拒收)
REQUIRED_COLUMNS = (EPISODE_ID, EMBODIMENT_ID, ACTION)


def daft_schema():
    """构建 daft 侧 schema(惰性 import;action = 变长 tensor,shape=None)。"""
    raise NotImplementedError("P2 M1 实现(PLAN.md §3.2)")
