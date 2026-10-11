"""A.2 验收:VLM 客户端解析/请求构造(本地 stub 服务器,不需要真模型)。"""
from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import numpy as np
import pytest


def _mark_frame(mark: int) -> np.ndarray:
    """造一帧"带标记"的图:整幅填同一灰度值 mark → 服务端解码后能认出是第几帧。
    用于校验并发下的**保序性**(见 _Stub.do_POST 注释)。"""
    return np.full((32, 32, 3), mark, dtype=np.uint8)


def _decode_mark(data_uri: str) -> int:
    """从 data URI 解出 _mark_frame 编进去的灰度值(取中心像素,避开 JPEG 边缘振铃)。"""
    from PIL import Image

    raw = base64.b64decode(data_uri.split(",", 1)[1])
    arr = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))
    return int(round(float(arr[arr.shape[0] // 2, arr.shape[1] // 2].mean())))


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """并发桩:默认 HTTPServer 单线程串行处理,并发请求会排队,
    既测不出真并发行为、也掩盖竞态。"""

    daemon_threads = True

from curation.adapters.vlm_client import vlm_completion_from_config


# ---------- 解析 ----------


# ---------- 请求构造(stub openai 兼容服务器) ----------

class _Stub(BaseHTTPRequestHandler):
    seen: list[dict] = []
    _lock = threading.Lock()
    delay_jitter: float = 0.0          # >0 时每请求随机延迟,用于放大乱序风险

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if _Stub.delay_jitter:
            import random
            import time as _t
            _t.sleep(random.random() * _Stub.delay_jitter)
        with _Stub._lock:                      # 并发请求下 seen 是共享状态,必须加锁
            _Stub.seen.append({"path": self.path, "body": body})
        # v3:每请求 2 图 → 回 1 个数;v4:按 ask 文本里的 k 回 k 个数
        import re as _re
        texts = " ".join(c.get("text", "") for c in body["messages"][0]["content"]
                         if c["type"] == "text")
        m = _re.search(r"exactly (\d+) comma", texts)
        if any(c["type"] == "video_url" for c in body["messages"][0]["content"]):
            answer = {"verdict": "success", "task_type": "persistent", "completion": 0.9,
                      "reason": "visible goal", "evidence": [{"camera": "cam", "start_s": 0.0,
                      "end_s": 0.5, "observation": "object at destination"}]}
            if 'more field "cameras"' in texts:        # the judgement answers per camera too (D71)
                answer["cameras"] = {"cam": {"verdict": "success", "reason": "visible goal",
                                             "camera_check": {}}}
            resp = {"choices": [{"message": {"content": json.dumps(answer)}}]}
        elif m:
            k = int(m.group(1))
            resp = {"choices": [{"message": {"content": ", ".join(str((i + 1) * 10) for i in range(k))}}]}
        else:
            # ⚠️ 回值必须由**请求内容**决定,不能用到达序号:客户端并发发出后到达顺序不确定,
            # 用序号会让"第 i 帧 ↔ 第 i 个回值"的对应关系断裂,从而测不出真正的保序性。
            # 查询帧的像素值(_QUERY_MARK)编在图里 → 回该值,断言即可校验保序。
            imgs = [c for c in body["messages"][0]["content"] if c["type"] == "image_url"]
            mark = _decode_mark(imgs[-1]["image_url"]["url"]) if imgs else 0
            resp = {"choices": [{"message": {"content": str(mark)}}]}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture()
def stub_server():
    _Stub.seen = []
    srv = _ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_from_config_single_source_of_truth(stub_server):
    """模型只在 YAML 一处:从配置构造,端点/模型都来自 vlm: 段。

    v7.2 起 from_config 返回**多视角联合**打分器:帧 = [(相机名, 图), ...];
    单相机数据集即单元素列表(自动退化,零分支)。"""
    cfg = {"checks": {"task_success": {"vlm": {"endpoint": stub_server, "model": "m-x"}}}}
    vlm = vlm_completion_from_config(cfg)
    from curation.adapters.video_input import VideoClip
    out = vlm([VideoClip("cam", "data:video/mp4;base64,YWJj", "abc", 0, 1, 10, 3)], "t")
    assert out["verdict"] == "success" and out["completion"] == 0.9
    assert _Stub.seen[-1]["body"]["model"] == "m-x"
    # 联合 prompt 必须带相机标签(模型据此知道哪张图是哪路)
    content = _Stub.seen[-1]["body"]["messages"][0]["content"]
    assert any(c["type"] == "video_url" for c in content)
    assert any("Camera: cam" in c.get("text", "") for c in content)


def test_from_config_reads_max_concurrency(stub_server, monkeypatch):
    """并发度可从 YAML 配置(上 Ray 后要按 worker 数调小,不能写死)。"""
    cfg = {"checks": {"task_success": {"vlm": {
        "endpoint": stub_server, "model": "m-x", "max_concurrency": 1}}}}   # 1=串行
    from curation.adapters import vlm_client
    capacities = []
    gate = vlm_client.SharedGate
    monkeypatch.setattr(vlm_client, "SharedGate", lambda n: capacities.append(n) or gate(n))
    assert vlm_completion_from_config(cfg).media_input == "video"
    assert capacities == [1]


def test_missing_endpoint_is_explicit():
    with pytest.raises(ValueError, match="endpoint"):
        vlm_completion_from_config({"checks": {"task_success": {"vlm": {"model": "m"}}}})

# ---------- v4 few-shot 协议 + CoT 解析 ----------

def test_strip_reasoning():
    from curation.adapters.vlm_client import strip_reasoning

    assert strip_reasoning("<think>我算算 1 2 3</think>\n40, 50") == "\n40, 50"
    assert strip_reasoning("no tags 70") == "no tags 70"


