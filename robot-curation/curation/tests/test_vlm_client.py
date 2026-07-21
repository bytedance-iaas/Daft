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

from curation.adapters.vlm_client import (
    make_vlm_completion,
    parse_completion,
    parse_completion_list,
    vlm_completion_from_config,
)


# ---------- 解析 ----------

@pytest.mark.parametrize("text,expected", [
    ("85", 0.85),
    ("85.5", 0.855),
    ("0.3", 0.3),
    ("The completion is 70 percent", 0.70),
    ("100", 1.0),
    ("0", 0.0),
    ("120", 1.0),          # 越界裁剪
    ("-5", 0.0),
])
def test_parse_completion(text, expected):
    assert parse_completion(text) == pytest.approx(expected)


def test_parse_no_number_raises():
    with pytest.raises(ValueError, match="没有数字"):
        parse_completion("I cannot tell.")


def test_parse_completion_list():
    assert parse_completion_list("10, 20, 90", 3) == pytest.approx([0.1, 0.2, 0.9])
    assert parse_completion_list("0.1 0.5 0.9", 3) == pytest.approx([0.1, 0.5, 0.9])
    with pytest.raises(ValueError, match="期望 3 个数"):
        parse_completion_list("10, 20", 3)


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
        if m:
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


def test_end_to_end_against_stub(stub_server):
    vlm = make_vlm_completion(stub_server, model="test-model")
    ref = _mark_frame(0)
    marks = [10, 20, 30, 40]
    shuffled = [_mark_frame(m) for m in marks]         # 每帧编入自己的标记值
    out = vlm(ref, shuffled, "push the block")
    # 桩按"请求里查询帧的标记"回值 → 第 i 帧必须拿到第 i 个值,并发下也不能错位
    assert out == pytest.approx([m / 100 for m in marks], abs=0.02)

    assert len(_Stub.seen) == 4                        # 锚定协议:4 帧 = 4 次请求
    req = _Stub.seen[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["body"]["model"] == "test-model"
    content = req["body"]["messages"][0]["content"]
    imgs = [c for c in content if c["type"] == "image_url"]
    assert len(imgs) == 2                              # 参考帧 + 查询帧
    assert all(c["image_url"]["url"].startswith("data:image/jpeg;base64,") for c in imgs)
    txt = next(c for c in content if c["type"] == "text")["text"]
    assert "push the block" in txt and "START" in txt


def test_from_config_single_source_of_truth(stub_server):
    """模型只在 YAML 一处:从配置构造,端点/模型都来自 vlm: 段。"""
    cfg = {"checks": {"task_success": {"vlm": {"endpoint": stub_server, "model": "m-x"}}}}
    vlm = vlm_completion_from_config(cfg)
    marks = [30, 60]
    out = vlm(_mark_frame(0), [_mark_frame(m) for m in marks], "t")
    assert out == pytest.approx([m / 100 for m in marks], abs=0.02)
    assert _Stub.seen[-1]["body"]["model"] == "m-x"


def test_from_config_reads_max_concurrency(stub_server):
    """并发度可从 YAML 配置(上 Ray 后要按 worker 数调小,不能写死)。"""
    cfg = {"checks": {"task_success": {"vlm": {
        "endpoint": stub_server, "model": "m-x", "max_concurrency": 1}}}}   # 1=串行
    vlm = vlm_completion_from_config(cfg)
    marks = [10, 20, 30]
    out = vlm(_mark_frame(0), [_mark_frame(m) for m in marks], "t")
    assert out == pytest.approx([m / 100 for m in marks], abs=0.02)   # 串行也必须对


@pytest.mark.parametrize("concurrency", [1, 4, 8])
def test_order_preserved_under_concurrency(stub_server, concurrency):
    """★ 保序是正确性要求:VOC 抗幻觉靠"预测完成度 vs 真实时序"的相关性,
    结果错位整个判定就废。服务端加随机延迟放大乱序风险,断言 i 帧拿到 i 值。"""
    _Stub.delay_jitter = 0.05          # 让响应先后顺序与请求顺序脱钩
    try:
        vlm = make_vlm_completion(stub_server, model="m", max_concurrency=concurrency)
        marks = [70, 10, 90, 40, 20, 60, 30, 80]        # 刻意乱序,非单调
        out = vlm(_mark_frame(0), [_mark_frame(m) for m in marks], "t")
        assert out == pytest.approx([m / 100 for m in marks], abs=0.02)
        assert len(_Stub.seen) == len(marks)            # 不多发也不漏发
    finally:
        _Stub.delay_jitter = 0.0


def test_missing_endpoint_is_explicit():
    with pytest.raises(ValueError, match="endpoint"):
        vlm_completion_from_config({"checks": {"task_success": {"vlm": {"model": "m"}}}})

# ---------- v4 few-shot 协议 + CoT 解析 ----------

def test_strip_reasoning():
    from curation.adapters.vlm_client import strip_reasoning

    assert strip_reasoning("<think>我算算 1 2 3</think>\n40, 50") == "\n40, 50"
    assert strip_reasoning("no tags 70") == "no tags 70"


def test_parse_list_ignores_cot_numbers():
    from curation.adapters.vlm_client import parse_completion_list

    text = "<think>frame 1 maybe 90? frame 2... 8 frames total</think>10, 20, 30"
    assert parse_completion_list(text, 3) == pytest.approx([0.1, 0.2, 0.3])


def test_parse_list_takes_last_k():
    from curation.adapters.vlm_client import parse_completion_list

    # 模型复述题干"the 4 frames"后再给答案 → 取最后 K 个
    assert parse_completion_list("For the 4 frames: 10, 20, 30, 40", 4) == \
        pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_v5_fewshot_anchored_request_structure(stub_server):
    from curation.adapters.vlm_client import build_linear_context, make_vlm_completion

    ctx_src = [np.full((16, 16, 3), i * 25, dtype=np.uint8) for i in range(9)]
    ctx = build_linear_context(ctx_src, n=4)
    vlm = make_vlm_completion(stub_server, model="m-v5", context=ctx)
    ref = np.zeros((16, 16, 3), dtype=np.uint8)
    out = vlm(ref, [ref] * 3, "stack cubes")
    assert len(out) == 3

    assert len(_Stub.seen) == 3                        # v5 = 每评测帧一次请求
    content = _Stub.seen[0]["body"]["messages"][0]["content"]
    imgs = [c for c in content if c["type"] == "image_url"]
    assert len(imgs) == 4 + 1 + 1                      # 4 上下文 + 起始帧 + 1 查询帧
    texts = [c["text"] for c in content if c["type"] == "text"]
    assert sum("Task completion:" in t for t in texts) == 4   # 每个上下文帧带标注
    assert any("THIS frame" in t for t in texts)
    assert any("stack cubes" in t for t in texts)


def test_build_linear_context_labels_match_frames():
    from curation.adapters.vlm_client import build_linear_context

    frames = [np.full((8, 8, 3), i * 30, dtype=np.uint8) for i in range(9)]
    ctx_frames, ctx_rates = build_linear_context(frames, n=5)
    # 打乱后标注仍与帧一一对应:帧亮度单调编码时序 → 亮度排序应还原标注排序
    lum = [f.mean() for f in ctx_frames]
    assert sorted(range(5), key=lambda i: lum[i]) == sorted(range(5), key=lambda i: ctx_rates[i])
    assert min(ctx_rates) == 0.0 and max(ctx_rates) == 1.0
