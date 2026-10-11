"""task_success 证据帧落盘(UI 数据面,2026-07-27 U0 盘点补缺)。

背景:VLM 判定的 detail 里只有 probe_frames **帧号**,帧本身在漏斗 UDF 里
用完即弃——被拒/待裁决条目"看着证据说话"没有图可看(demo/人工裁决都要)。
方案:导出期对 flagged 条目按漏斗同参(sample_interval_s/max_side)重解码,
把 probe 帧存 JPEG 到 details/evidence/<ep>/。选择导出期而非 UDF 内落盘,
是为了不碰漏斗并发路径(写文件进 UDF = 并发写盘 + 交付目录耦合进检查层)。

模式沿用 sync_plots 的约定:flagged=只存人工会看的那批(拒绝/待裁决,默认);
all=全存(小数据集/演示);off=不存。
"""
from __future__ import annotations

import json
import os


#: 证据帧最多给几条 episode 存(再多也没人逐条看,徒增交付体积与导出时间)。
EVIDENCE_CAP = 200


def flagged_for_evidence(per_episode: dict, mode: str = "flagged") -> list[str]:
    """选出该存证据帧的 episode:task_success 拒绝 or 待裁决(mode=all 时全员)。

    纯函数,单独可测。硬门中途杀的(时间戳/运动学)不在此列——它们死于
    VLM 之前,本就没有 probe 帧。
    """
    if mode == "off":
        return []
    out = []
    for eid, pe in sorted(per_episode.items()):
        ts = pe.get("checks", {}).get("task_success")
        if ts is None:
            continue
        if mode == "all":
            out.append(eid)
            continue
        rejected = ts.get("passed") is False
        undecided = "task_success" in (pe.get("undecidable") or [])
        if rejected or undecided:
            out.append(eid)
    return out


def probe_indices(check_entry: dict) -> list[int]:
    """从 task_success 的 detail(双重编码 JSON)里取 probe_frames 帧号。"""
    try:
        det = check_entry.get("detail") or "{}"
        if isinstance(det, str):
            det = json.loads(det)
        idx = det.get("probe_frames") or []
        return [int(i) for i in idx]
    except Exception:  # noqa: BLE001
        return []


def render_task_evidence(per_episode: dict, videos: dict, out_dir: str,
                         interval: float, max_side: int,
                         mode: str = "flagged", cap: int = EVIDENCE_CAP,
                         decode_fn=None, on_progress=None) -> dict:
    """flagged 条目的 probe 帧 → details/evidence/<ep>/probe<i>_f<帧号>.jpg。

    videos: {episode_id: 多相机指针 struct};解码只取首相机(与漏斗
    `sorted(video.keys())[0]` 同一路,证据与判定看的是同一双眼睛)。
    decode_fn 可注入(测试桩);返回 {episode_id: [相对路径,...]},空条目不建目录。
    单条解码失败跳过不中断——证据是附件,不是判决的一部分。
    on_progress: 每条(含跳过的)处理完调用一次,无参 —— 本步骤要逐条解码,
      条数多时是十几秒到几分钟的活,调用方拿它报进度(慢步骤不许静默)。
    """
    eids = flagged_for_evidence(per_episode, mode)[:cap]
    if not eids:
        return {}
    if decode_fn is None:
        from ..adapters.decode import decode_window as decode_fn  # noqa: N813
    written: dict = {}

    def _one(eid: str) -> list:
        """一条 episode 的证据帧;跳过/失败都返回空,进度由外层 finally 记。"""
        video = videos.get(eid)
        if not video:
            return []
        idx = probe_indices(per_episode[eid]["checks"]["task_success"])
        if not idx:
            return []
        cam = sorted(video.keys())[0]
        v = video[cam]
        try:
            frames, _ = decode_fn(v["path"], v["from_ts"], v["to_ts"],
                                  sample_interval_s=interval, max_side=max_side)
        except Exception:  # noqa: BLE001
            return []
        if not frames:
            return []
        ep_dir = os.path.join(out_dir, eid)
        rels = []
        for i, fi in enumerate(idx):
            if not 0 <= fi < len(frames):
                continue
            os.makedirs(ep_dir, exist_ok=True)
            name = f"probe{i}_f{fi}.jpg"
            if _write_jpeg(os.path.join(ep_dir, name), frames[fi]):
                rels.append(os.path.join(eid, name))
        return rels

    for eid in eids:
        try:
            rels = _one(eid)
        finally:
            # 跳过的、失败的都要计数,否则进度永远到不了 100%,看着像卡死
            if on_progress is not None:
                on_progress()
        if rels:
            written[eid] = rels
    return written


def _write_jpeg(path: str, frame_rgb) -> bool:
    """RGB ndarray → JPEG。cv2 吃 BGR,写前翻通道。

    ⚠️ 先写本地临时文件、验完 JPEG 魔数(FF D8 FF)再整份拷到交付目录:图像库
    直写 TOS 的 FSX 挂载会**静默产出零填充坏文件**(2026-08-07 savefig 那次 5 张
    坏 3 张,字节数看着正常、签名全零)。与 sync_plots._savefig_fsx_safe 同一招;
    这里判坏就返回 False(证据帧是附件,少一张不该中断交付)。
    """
    import os
    import shutil
    import tempfile
    try:
        import cv2
        import numpy as np
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            if not cv2.imwrite(tmp, cv2.cvtColor(
                    np.asarray(frame_rgb), cv2.COLOR_RGB2BGR)):
                return False
            with open(tmp, "rb") as f:
                if f.read(3) != b"\xff\xd8\xff":
                    return False
            shutil.copyfile(tmp, path)
            return True
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return False


