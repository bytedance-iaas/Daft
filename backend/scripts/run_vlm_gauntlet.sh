#!/usr/bin/env bash
# 四模型顺序评测驱动:serve → 等就绪 → 评测 → 停 → 下一个(M4c 模型选型用)
# 用法: bash scripts/run_vlm_gauntlet.sh
set -uo pipefail
cd /data03/hao/curation-project
export HF_HOME=/data03/hao/.hf_home

MODELS=(
  "Qwen/Qwen2.5-VL-7B-Instruct:ab_qwen25_v3"
  "Qwen/Qwen3-VL-8B-Instruct:ab_qwen3_v3"
  "nvidia/Cosmos-Reason1-7B:ab_cosmos1_v3"
  "nvidia/Cosmos-Reason2-8B:ab_cosmos2_v3"
)
VLLM=/data03/hao/venv/vllm/bin/vllm
PY=/data03/hao/venv/curation/bin/python

stop_vllm() {
  # 用 vllm 可执行路径匹配,避免 pkill 误杀本脚本(命令文本自匹配的坑,2026-07-02 踩过)
  local pids
  pids=$(pgrep -f "venv/vllm/bin" | grep -v "^$$\$" || true)
  [ -n "$pids" ] && kill $pids 2>/dev/null
  sleep 10
}

for entry in "${MODELS[@]}"; do
  model="${entry%%:*}"; tag="${entry##*:}"
  echo "===== $model ====="
  stop_vllm
  GPU=$(python3 scripts/free_gpus.py -n 1)
  echo "GPU=$GPU"
  PATH=/data03/hao/venv/vllm/bin:$PATH CUDA_VISIBLE_DEVICES=$GPU \
    $VLLM serve "$model" --port 8000 --max-model-len 16384 \
    --gpu-memory-utilization 0.85 > "/data03/hao/.vllm_${tag}.log" 2>&1 &
  SERVE_PID=$!

  # 等就绪(最多 12 分钟),失败则记录并跳过
  ok=0
  for i in $(seq 1 72); do
    if curl -s -m 3 http://localhost:8000/v1/models 2>/dev/null | grep -q "$model"; then ok=1; break; fi
    if grep -q "EngineCore failed" "/data03/hao/.vllm_${tag}.log" 2>/dev/null; then break; fi
    sleep 10
  done
  if [ "$ok" != 1 ]; then
    echo "SERVE_FAILED $model" | tee -a spikes/gauntlet_failures.log
    continue
  fi

  $PY spikes/spike_vlm_ab.py --endpoint http://localhost:8000/v1 \
    --model "$model" --n 10 --out "spikes/${tag}.json" \
    && echo "EVAL_OK $model" || echo "EVAL_FAILED $model" | tee -a spikes/gauntlet_failures.log
done
stop_vllm
echo "GAUNTLET_DONE"
