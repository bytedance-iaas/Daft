"""相机朝向与左右镜像(2026-09-02 用户定,libero ep000004 教训)。

任务文本里的"左/右"是机器人视角;正对机器人的前置相机画面左右相反,判官按画面看会答反
(布丁其实在机器人右边,画面上在盘子左边,复核两路全投 no)。解法不是让模型猜朝向,而是
数据集 profile 声明每路相机的朝向,任务含左右词时把一句提示挂在该相机的标签/问题上;
其它朝向/未声明的相机一个字不加(2026-09-02 用户定:只纠正 front)。不含左右词的任务一个字不加。
"""
from __future__ import annotations

import re

#: profile 里 cameras.<name>.view 的取值
VIEWS = ("front", "rear", "wrist", "side", "unknown")

_LATERAL = re.compile(r"\b(left|right|leftmost|rightmost)\b|[左右]", re.IGNORECASE)

_HINTS = {
    # 只有正对机器人的前置机位需要纠正(2026-09-02 用户定):其它朝向不加任何提示——
    # "左右一致"是模型默认假设,加了是废话;"朝向未知就答 UNCLEAR"在 droid 上没验证过,
    # 宁可回到"没声明就什么都不改"的保守默认,把未知机位的朝向核实后写进 profile。
    "front": ("This camera faces the robot, so the robot's RIGHT appears on the LEFT of the "
              "image (and vice versa); interpret left/right in the task from the robot's "
              "point of view."),
}


def lateral(text: str) -> bool:
    """任务文本是否含左右方位词(中英文)。"""
    return bool(_LATERAL.search(str(text or "")))


def view_hint(view: str | None, instruction: str) -> str:
    """某路相机的朝向提示:任务不含左右词 → "";只有 front 有文案,其余(含未声明)→ ""。"""
    if not lateral(instruction):
        return ""
    return _HINTS.get(str(view or "").lower(), "")


def camera_hints(views: dict | None, instruction: str, cameras) -> dict[str, str]:
    """{相机短名: 提示句}(只含非空,即只有 front 机位)。views = profile 的 cameras 段
    ({名: view} 或 {名: {view: ...}}),没声明的相机不加。"""
    if not lateral(instruction):
        return {}
    views = views or {}
    out = {}
    for cam in cameras:
        v = views.get(cam)
        if isinstance(v, dict):
            v = v.get("view")
        h = view_hint(v, instruction)
        if h:
            out[str(cam)] = h
    return out


#: 全腕部数据集的提示(2026-09-18 umi 抽屉条目教训):打分/复核题写着"只看物体和场景,
#: 不看机械臂",这是给外部固定机位定的;相机全装在夹爪上时,东西一到手里镜头就离开原位,
#: 画面里唯一的"做成"证据就是夹爪里攥着的那个物体——不点明,模型会把它当机械臂动作扔掉。
#: 只在**每一路**都是 wrist 时才加(有外部机位的数据集一个字不变,droid/libero 零 diff)。
WRIST_ONLY_HINT = ("this camera is mounted on the gripper; the gripper and whatever it holds are "
                   "part of the scene, so an object held in the gripper away from where it "
                   "started counts as task progress")


def wrist_only_hints(views: dict | None, cameras) -> dict[str, str]:
    """{相机短名: 全腕部提示}——仅当 cameras 非空且每一路在 profile 里都声明为 wrist;否则 {}。"""
    cams = [str(c) for c in cameras]
    if not cams or not views:
        return {}
    for cam in cams:
        v = views.get(cam)
        if isinstance(v, dict):
            v = v.get("view")
        if str(v or "").lower() != "wrist":
            return {}
    return {cam: WRIST_ONLY_HINT for cam in cams}


def label_hints(views: dict | None, instruction: str, cameras) -> dict[str, str]:
    """打分/复核层挂在相机标签上的提示 = 左右镜像提示 + 全腕部提示(用 "; " 连接)。
    仲裁层**只**用 camera_hints(左右):腕部提示若挂到核验题上会把"物体在夹爪里"暗示成
    "已在目标位置",那是另一道题。"""
    out = dict(camera_hints(views, instruction, cameras))
    for cam, h in wrist_only_hints(views, cameras).items():
        out[cam] = f"{out[cam]}; {h}" if cam in out else h
    return out
