"""VLM 工厂闭包必须可 cloudpickle —— 防 2026-08-18 生产完整质检崩溃复发。

事故:a5c3d0e(对冲补发)在各 VLM 工厂与 build_arbitration_deps 里建了**局部**
threading.Semaphore 当闸门,这些闭包一路进了 task_check 这个 daft async UDF;
daft 的 check_serializable 对 UDF 做 cloudpickle,裸 Semaphore 内含 _thread.lock
序列化不了 → 生产上 `curation run`(完整质检)直接崩在 with_column,只有 --lite
能跑。之所以三天没人发现:上线后只跑过 --lite 和 rejudge,而 test_e2e_pipeline
一直在 --ignore 列表里,单测也咬不到。

本文件不发任何网络请求(工厂构造期只拼 URL/闭包),专测"能不能序列化",
序列化器用 daft.pickle(vendored cloudpickle)—— 与生产 check_serializable 同一把尺子:
任何人再往这些闭包里塞不可 pickle 的共享状态(锁/信号量/线程池/事件),这里变红。
"""
from __future__ import annotations

import pickle

import pytest
from daft import pickle as daft_pickle

from curation.adapters.vlm_client import (SharedGate, make_endstate_voter,
                                          make_llm_ask,
                                          make_multiview_completion,
                                          make_vlm_completion)
from curation.dataset_level.caption import make_vlm_captioner
from curation.pipeline.funnel import build_arbitration_deps

# RFC 5737 文档段地址:测试永不真连
_EP = "http://192.0.2.10:8000/v1"
_MODEL = "test-model"


def _arb_cfg() -> dict:
    """build_arbitration_deps 需要的最小配置(api_key_env 指向不存在的变量 →
    auth_headers 返回空头,构造照走,零网络)。"""
    return {
        "checks": {"task_success": {
            "arbitration": {"enable": True},
            "vlm": {"endpoint": _EP, "model": _MODEL,
                    "api_key_env": "CURATION_TEST_NO_SUCH_KEY"},
        }},
        "pipeline": {"vlm_episode_concurrency": 4},
    }


def test_arbitration_deps_cloudpicklable():
    """事故的直接复刻:仲裁链依赖包(四工厂共享 arb_gate + captioner)整体过
    cloudpickle。修复前(funnel 里 arb_gate = threading.Semaphore)本用例必红。"""
    deps = build_arbitration_deps(_arb_cfg())
    assert deps is not None
    daft_pickle.dumps(deps)  # 炸 = 有闭包捕获了不可序列化的共享状态


def test_each_vlm_factory_product_cloudpicklable():
    """五个工厂的产物逐个过 cloudpickle:vlm_completion / cam_voter / captioner
    都会进 task_check 闭包,llm_ask 进 M7 归纳,谁塞了裸锁谁红。"""
    products = {
        "vlm_completion": make_vlm_completion(_EP, _MODEL),
        "multiview_completion": make_multiview_completion(_EP, _MODEL),
        "endstate_voter": make_endstate_voter(_EP, _MODEL),
        "captioner": make_vlm_captioner(_EP, _MODEL),
        "llm_ask": make_llm_ask(_EP, _MODEL),
    }
    for name, fn in products.items():
        try:
            daft_pickle.dumps(fn)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"{name} 闭包不可序列化(进 daft UDF 会当场崩):{e}")


def test_composed_task_check_closure_cloudpicklable():
    """按 funnel 里 task_check 的真实闭包结构拼一个同型闭包整体序列化:
    单测各工厂都绿、组合后仍可能因新增捕获物变红,这层兜住组合面。"""
    deps = build_arbitration_deps(_arb_cfg())
    vlm_completion = make_multiview_completion(_EP, _MODEL)
    cam_voter = make_endstate_voter(_EP, _MODEL)

    def _task_check_sync_like(x):
        return (vlm_completion, cam_voter, deps["question_writer"],
                deps["grounder"], deps["judge"], deps["same_task"],
                deps["captioner"], x)

    async def task_check_like(x):
        return _task_check_sync_like(x)

    daft_pickle.dumps(task_check_like)


def test_shared_gate_semantics():
    """SharedGate 的三条契约:①容量真的限并发(acquire 到顶就拿不到);
    ②普通 pickle 往返后同一 dumps 里的共享关系不裂开(四工厂共用一闸的语义
    靠它);③往返后闸门功能完好。"""
    g = SharedGate(2)
    assert g.acquire(timeout=0.1) and g.acquire(timeout=0.1)
    assert not g.acquire(timeout=0.05)      # 第 3 个许可必须拿不到
    g.release()
    assert g.acquire(timeout=0.1)

    pair = pickle.loads(pickle.dumps((g, g)))
    assert pair[0] is pair[1]               # memo 保共享:反序列化后仍是同一实例
    g2 = pair[0]
    assert g2.capacity == 2
    assert g2.acquire(timeout=0.1) and g2.acquire(timeout=0.1)
    assert not g2.acquire(timeout=0.05)     # 重建的信号量容量与构造期一致
