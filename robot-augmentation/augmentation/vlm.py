"""方舟 VLM 调用与几个通用问答:物体清单、编辑目标解析、目标材质短语。模型名只在 AUG_VLM_MODEL 一处(默认 doubao-seed-2-0-pro)。"""
from __future__ import annotations

import base64
import json
import os

import cv2
import numpy as np
import requests

DEFAULT_MODEL = os.environ.get("AUG_VLM_MODEL", "doubao-seed-2-0-pro-260215")


def ask_vlm(content: list, *, model: str | None = None, max_tokens: int = 600) -> str:
    """一次对话调用,短超时 + 重试 3 次(方舟偶发挂死的连接)。content 是 OpenAI 风格的多模态 parts。"""
    body = {"model": model or DEFAULT_MODEL, "temperature": 0, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
    base = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    last = None
    for _ in range(3):
        try:
            r = requests.post(base + "/chat/completions", headers={"Authorization": f"Bearer {os.environ['ARK_API_KEY']}", "Connection": "close"},
                              json=body, timeout=(10, 60))
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"VLM call failed after 3 attempts: {last!r}")


def img_part(rgb: np.ndarray) -> dict:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()}}


_ask_vlm, _img_part = ask_vlm, img_part


def inventory(frame_rgb: np.ndarray) -> dict:
    """让 VLM 列出首帧里的物体(英文短名,GroundingDINO 用)与哪些属于机器人。返回 {"objects": [...], "robot": [...]}。"""
    txt = _ask_vlm([_img_part(frame_rgb), {"type": "text", "text":
        "List every distinct physical object visible in this robot workspace image. Use short English noun phrases with a color "
        "or material adjective (e.g. 'red cube', 'white robot arm', 'black cardboard box'). Include the robot arm and its gripper as "
        "separate entries if both are visible, and include table mats / notebooks / containers. Do NOT include the table surface itself, "
        "the floor, walls or cables. Then say which entries are parts of the robot. Output JSON only: "
        "{\"objects\": [...], \"robot\": [...]} where robot is a subset of objects."}])
    txt = txt[txt.find("{"): txt.rfind("}") + 1]
    d = json.loads(txt)
    objs = [o.strip().lower() for o in d.get("objects", []) if isinstance(o, str) and o.strip()]
    robot = [o.strip().lower() for o in d.get("robot", []) if isinstance(o, str)]
    return {"objects": objs, "robot": [o for o in robot if o in objs]}



def edit_targets(edit: str, objects: list[str], robot: list[str], geom_dir: str | None = None) -> list[str]:
    """把编辑指令映射到几何档案里的物体名:哪些是这次要改的(不进保留掩码)。机器人部件除非指令点名否则永远保留。
    带首帧标注图一起问,让模型按画面对应(指令里的"牛皮纸本子"= 清单里的"brown work mat"这种靠文字对不上)。"""
    content = []
    png = os.path.join(geom_dir, "boxes_first_frame.png") if geom_dir else None
    if png and os.path.exists(png):
        content.append(_img_part(cv2.cvtColor(cv2.imread(png), cv2.COLOR_BGR2RGB)))
    content.append({"type": "text", "text":
        ("The image shows the first frame with each detected object outlined and labeled. " if content else "") +
        "Scene objects: " + json.dumps(objects, ensure_ascii=False) + "\n"
        "Robot parts among them: " + json.dumps(robot, ensure_ascii=False) + "\n"
        "Edit instruction (Chinese or English): " + edit + "\n"
        "Which of the scene objects does the instruction change (material, color, shape or identity)? Match by what the object IS in the "
        "picture, not by the words (e.g. the instruction may call a mat a notebook). A plain table surface / background change does NOT "
        "count as an object unless the list contains it. Output JSON only: {\"targets\": [names copied verbatim from the list]}"})
    txt = _ask_vlm(content, max_tokens=200)
    try:
        j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return [t for t in j.get("targets", []) if t in objects]
    except Exception:
        return []



def target_materials(edit: str, targets: list[str]) -> dict:
    """从编辑指令里抽每个目标物体要变成的材质/颜色(英文短语),供 intent_check 用。"""
    txt = _ask_vlm([{"type": "text", "text":
        "Edit instruction: " + edit + "\nTarget objects: " + json.dumps(targets, ensure_ascii=False) +
        "\nFor each target, give the material/color it should become after the edit, as a short English phrase (e.g. 'gold metal', 'silver metal', 'dark wood'). "
        "Output JSON only: {\"<target>\": \"<phrase>\"}"}], max_tokens=200)
    try:
        j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return {k: str(v) for k, v in j.items() if k in targets}
    except Exception:
        return {}


