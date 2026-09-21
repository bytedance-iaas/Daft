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


def write_audit_clips(flagged_ids: list[str], videos: dict, out_dir: str,
                      sample_interval_s: float = 0.5, max_side: int = 448,
                      play_fps: int = 4, max_cams: int = 3,
                      on_progress=None, concurrency: int = 4) -> int:
    """标注分歧条目的**视频片段**导出(2026-08-05 用户定:裁决要能直接看视频)。

    每条 flagged episode × 每路相机(≤max_cams)写一个 mp4 到
    details/audit_clips/<ep>__<cam>.mp4。素材 = 漏斗同参解码帧(0.5s 采样),
    按 play_fps 回放 ≈ 2 倍速——裁决看的是"干了什么",倍速反而高效。
    ⚠️ 编码用 PyAV(pod 无 ffmpeg CLI)且 **movflags=faststart**:moov 不在头部
    的话浏览器 <video> 会无报错地永久转圈(2026-08-03 人工审片工具的血泪)。
    失败条目静默跳过(少一路视频,不断链);返回成功写出的片段数。

    on_progress: 每条 episode 完成后调用一次(无参)。**并发下会被多线程调用**,
      回调方须自己线程安全(_progress_tick 内部有锁,满足)。
    concurrency: 同时切几条。解码+编码都在 PyAV 里放开 GIL,条数多时是纯等待
      CPU 的活;1 = 串行(排障)。片段之间互不影响,并发不动任何判定。
    """
    from ..adapters.decode import decode_window

    clip_dir = os.path.join(out_dir, "details", "audit_clips")

    def _one(eid: str) -> int:
        n = 0
        video = videos.get(eid) or {}
        try:
            for cam in sorted(video)[:max_cams]:
                v = video[cam]
                try:
                    frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                              sample_interval_s=sample_interval_s,
                                              max_side=max_side)
                    if not frames:
                        continue
                    os.makedirs(clip_dir, exist_ok=True)
                    dst = os.path.join(clip_dir, f"{eid}__{cam.split('.')[-1]}.mp4")
                    # ⚠️ 先编码到本地盘再整体拷贝:faststart 收尾要 seek 把 moov 挪到
                    # 文件头,而 TOS-FSX 挂载不支持 seek 写回(与 aria2 直写同一堵墙,
                    # 2026-08-05 实测:直写产出无 moov 的废 mp4,浏览器永久转圈)。
                    import shutil
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                        tmp = tf.name
                    try:
                        _encode_mp4(frames, tmp, play_fps)
                        shutil.copyfile(tmp, dst)          # 顺序写,FSX 安全
                        n += 1
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                except Exception:  # noqa: BLE001
                    continue
        finally:
            if on_progress is not None:      # 失败条也要计数,否则进度停在半截像卡死
                on_progress()
        return n

    ids = list(flagged_ids)
    if concurrency <= 1 or len(ids) <= 1:
        return sum(_one(e) for e in ids)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(concurrency, len(ids))) as ex:
        return sum(ex.map(_one, ids))


def _encode_mp4(frames: list, path: str, fps: int) -> None:
    """RGB ndarray 序列 → h264 mp4(faststart;宽高裁到偶数,yuv420p 硬要求)。"""
    import av
    import numpy as np

    h, w = frames[0].shape[:2]
    w2, h2 = w - w % 2, h - h % 2
    with av.open(path, "w", options={"movflags": "faststart"}) as container:
        stream = container.add_stream("h264", rate=fps)
        stream.width, stream.height = w2, h2
        stream.pix_fmt = "yuv420p"
        for f in frames:
            frame = av.VideoFrame.from_ndarray(
                np.ascontiguousarray(f[:h2, :w2]), format="rgb24")
            for pkt in stream.encode(frame):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
