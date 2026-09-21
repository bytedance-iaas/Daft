# 密钥与资源管理（W8 / F2.6）

TOS 访问密钥、VLM 后端与模型、交付目录写探针、媒体预签名，以及给任务编排（W5）用的开始前三项检查和 CLI 子进程环境。
设计依据：`docs/design/08-secrets-and-auth.md`（全篇）、`01` §2.1–§2.2、`03` §2 / §3 第 4 步 / §7，决策 D8、D16、D30。

**一条纪律**：密钥本体只会离开密封存储去两个地方 —— CLI 子进程的环境变量、发往 TOS / 模型服务的请求。
它不进任何响应、日志、审计事件、异常信息，也不以明文落库；服务方回来的报错文字先抹掉密钥再保存或展示。

## 文件

| 文件 | 内容 |
|---|---|
| `sealing.py` | AES-256-GCM：`payload_enc = nonce(12) ‖ 密文 ‖ tag(16)`，行 id 作为附加数据绑定；每行记 `key_version`；可续做的主密钥轮换 `rotate()` |
| `rotate.py` | `python -m daemon.secrets.rotate`：在 Pod 里执行轮换（设计里的 `curator-admin rotate-master-key`） |
| `scrub.py` | 把已知密钥（含 URL 编码形式）从服务方返回的文字里换成 `***`，再套一遍日志脱敏规则 |
| `tos.py` | 用解密后的访问密钥发起的调用：身份校验、读、写探针、预签名；端点规则；错误分类 |
| `vlm.py` | OpenAI 兼容接口：`GET /models`、最小调用（思考强度为空时请求体里没有 `reasoning_effort`） |
| `effort.py` | 思考强度映射表（08 §4.1），站点可覆盖 |
| `service.py` | `SecretsService`（每个 Daemon 一个，`service_of(runtime)`）：解密、查找、给 W5 的 `VlmTarget` |
| `prechecks.py` | 开始前的三项检查（D30） |
| `cli_env.py` | CLI 子进程的环境变量 |
| `views.py`、`http.py` | 响应组装；路由共用的校验（不回显密钥）、幂等、审计 |
| `../routes/access_keys.py`、`vlm.py`、`media.py` | `/credentials`、`/vlm-backends`、`/deliveries/probe`、`/media/sign` |

## 行为要点

- **保存访问密钥只验身份**：一次签名的 `ListBuckets`；没有列桶权限的，对「测试用存储桶」做 `HeadBucket`。
  不读也不写任何对象。结果只是标记：`ok` / `failed`（附原因）/ `unverified`（没有列桶权限又没填测试用存储桶），
  验证不过照样保存（D30）。对具体存储桶的读写在任务开始前验。
- **编辑时密钥留空 = 不修改**；换了 Access Key ID 必须同时填新的 Secret。改名不重新验证，改密钥、地域、endpoint、测试用存储桶会。
- **删除**：被未结束的任务引用 → 409 `credential_in_use` / `backend_in_use`；只被已结束的任务引用 → 同样 409，
  `details.confirm_required: true`，带 `?confirm=true` 才删（任务上的引用置空，之后用 `rebind-credentials` 重新绑定）。
- **VLM 后端**：API Key 封存在一条独立的密钥行里（`kind` 为 `ark` / `custom_vlm`，名字 `vlm-backend/<后端 id>`，
  用户建的访问密钥不能用这个前缀），随后端一起建、一起删；这行的 `payload_meta` 记 `has_api_key`、`models_listed`，列表不解密。
  保存后试一次 `GET {endpoint}/models`：列得出来就存为 `source: listed`；列不出来不算错，后端标「未验证」并提示手填。
- **手填模型**先发一次最小请求，不通返回 422 `model_check_failed`，不保存。返回里的 `model` 字段能把 `ep-…` 解析成具体模型，
  用来确定它的思考强度档位。
- **思考强度**只能取该模型的有效档位（映射表匹配不上的给全 7 档），或 `null` = 请求里不带这个字段。
- **媒体签名**：`delivery` 以任务的批次目录 `<交付目录>/<run_id>/` 为前缀（输出密钥），`input` 以输入数据集为前缀（输入密钥；
  HuggingFace 缓存桶不签名，直接给公网地址）。`path` 先归一化，含 `..`、反斜杠、控制字符、编码过的 `.` / `/` 一律拒绝。
  一律用公网端点签名，TTL 60–3600 秒，默认 30 分钟，响应带 `Cache-Control: no-store`。

## 给 W5 的接口

```python
from daemon.secrets import service_of, prechecks_for_task, run_prechecks, cli_environment, Unavailable

svc = service_of(runtime)

# 开始 / 重试 / 继续运行 / 重新导出 / 执行裁决之前（checks 取与之相关的几项）
report = prechecks_for_task(svc, task, checks=("input", "output", "vlm"), need_vlm=None)
report.raise_for_failure()          # 422 precheck_failed，details.checks 逐项列出

# 还没写库的配置（POST /tasks 在建行之前想先查）
run_prechecks(svc, input=InputTarget(source, uri, region, cred_id),
              output=OutputTarget(uri, region, cred_id),
              vlm=VlmSelection(model_id=..., reasoning_effort=...))

# 开始时固化 VLM 配置（P17，不含 API Key）
task_vlm_snapshot = svc.vlm_target_for_task(task).snapshot()

# 起 CLI 子进程
cli = cli_environment(svc, task, need_input=True, need_output=True, need_vlm=None)
subprocess.Popen(argv + ["--input-region", cli.input_region, "--output-region", cli.output_region,
                         "--vlm-api-key-env", cli.vlm_api_key_env], env=cli.env)  # 值为 None 的参数不传
```

- `need_vlm=None` 表示按任务勾选的模块推断（C1 注册表里 `needs` 含 `vlm` 的模块）。
- 密钥或后端被删、密钥解不开时，`cli_environment` 与 `svc.tos_key(...)` 抛 `Unavailable`（`code`、`message_zh`）。
- 任务级思考强度覆盖在建任务 / PATCH 时用 `svc.check_task_effort(model_id, effort)` 校验。
- 其它 TOS 操作（清理交付产物、读报告）：`key = svc.tos_key(cred_id, role="output")`，
  `with svc.tos(key, region) as (client, ends): ...`（TOS SDK 客户端，用完关闭；Daemon 建的客户端关掉了 SDK 的 DNS 缓存，
  否则它会替换整个进程的 urllib3 建连函数）。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CURATOR_MASTER_KEY`、`_NEXT`、`_VERSION` | 见 Daemon README | 主密钥；轮换期间新写入的密钥用 `_NEXT` 加密 |
| `TOS_ENDPOINT` | 空 | 部署所在地域的 TOS 端点（v1 同名变量）。是内网端点（`*.ivolces.com`）时，同地域的调用走内网；也会原样交给 CLI |
| `CURATOR_REASONING_EFFORT_TABLE` | 空 | 覆盖思考强度映射表：JSON 文件路径或 JSON 本身，`[{"prefix": "glm-4.5", "levels": ["low","medium","high"], "default": "medium"}]` |

## 主密钥轮换（08 §2.1）

1. 把新主密钥写进 Secret 的 `masterKeyNext`（环境变量 `CURATOR_MASTER_KEY_NEXT`）并重启：此后两版都能解，新写入的用新版加密。
2. `kubectl exec <pod> -- python -m daemon.secrets.rotate`：逐行重新加密；中途断了再执行一次即可，退出码 0 表示完成。
   输出里的 `target_version` 是新版号；解不开的行列在 `failed`（退出码 1），需要重新填写。
3. 把新主密钥挪到 `masterKey`，`CURATOR_MASTER_KEY_VERSION` 改成 `target_version`，删掉 `masterKeyNext`，重启。

## 手动验证步骤

在 `backend/` 下执行，依赖见 Daemon README（另需 `cryptography`）。开三个终端，下面的 `$D` 指同一个空目录。

1. **模型服务替身**（OpenAI 兼容，只认 `demo-api-key-123456`，没有 `/models`，像方舟）

   ```bash
   ../.venv/bin/python -m tests.secrets.fakes --port 18181 --key demo-api-key-123456
   ```

2. **启动 Daemon**

   ```bash
   export D=$(mktemp -d)
   CURATOR_MASTER_KEY=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8= CURATOR_DATA_DIR=$D \
   CURATOR_BASE_PATH=/curation CURATOR_AUTH_USER=demo CURATOR_AUTH_PASSWORD=demo-pass CURATOR_LOG_FORMAT=text \
   ../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080 2>&1 | tee $D/daemon.log
   ```

3. **验证失败的访问密钥可以保存，并且有标记**（需要能访问公网 TOS）

   ```bash
   curl -s -u demo:demo-pass -X POST -H 'Content-Type: application/json' localhost:18080/curation/api/v1/credentials \
     -d '{"name":"demo-tos","access_key_id":"AKLTmanualDemoKeyId0000","secret_access_key":"manual-demo-secret-0000","region":"cn-beijing"}'
   ```

   预期：201，`verify_state` 为 `failed`，`last_verify_error` 是「访问密钥「demo-tos」的 AK/SK 不对，或者已经失效（InvalidAccessKeyId）」，
   `meta` 里只有地域和 `access_key_id_hint: "0000"`。

4. **密钥不在任何响应、日志和库文件里**

   ```bash
   curl -s -u demo:demo-pass localhost:18080/curation/api/v1/credentials | grep -c manual-demo-secret   # 0
   grep -c "manual-demo-secret\|AKLTmanualDemoKeyId0000" $D/daemon.log                               # 0
   ../.venv/bin/python -c "import pathlib,sys; print(any(b'manual-demo-secret' in p.read_bytes() for p in pathlib.Path(sys.argv[1]).glob('curator.db*')))" $D   # False
   ```

5. **列不出模型时手填；思考强度为空时请求里没有这个字段**

   ```bash
   B=$(curl -s -u demo:demo-pass -X POST -H 'Content-Type: application/json' localhost:18080/curation/api/v1/vlm-backends \
     -d '{"name":"demo-ark","kind":"ark","endpoint":"http://127.0.0.1:18181/v1","api_key":"demo-api-key-123456"}' \
     | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["id"]); print(b["verify_state"], b["models_listed"], b["last_verify_error"], file=sys.stderr)')
   curl -s -u demo:demo-pass -X POST -H 'Content-Type: application/json' localhost:18080/curation/api/v1/vlm-backends/$B/models \
     -d '{"model_name":"doubao-seed-2-0-pro-260215"}'
   curl -s -u demo:demo-pass -X POST -H 'Content-Type: application/json' localhost:18080/curation/api/v1/vlm-backends/$B/models \
     -d '{"model_name":"doubao-seed-2-0-lite-260215","reasoning_effort":"xhigh"}'
   curl -s -u demo:demo-pass -X POST -H 'Content-Type: application/json' localhost:18080/curation/api/v1/vlm-backends/$B/models \
     -d '{"model_name":"doubao-seed-2-0-lite-260215","reasoning_effort":"low"}'
   ```

   预期：后端 `unverified false 这个服务不提供模型列表（HTTP 404），请手动填写模型`；第一个模型 201，`reasoning_effort_levels`
   是 minimal / low / medium / high；`xhigh` 返回 400「……不能设为 xhigh（xhigh 在这个模型上等同于 high）」；`low` 201。
   替身终端里第一次最小调用的请求体没有 `reasoning_effort`，最后一次带 `"reasoning_effort": "low"`；终端里看不到 API Key。

6. **主密钥轮换**：停掉 Daemon（Ctrl-C），执行

   ```bash
   CURATOR_MASTER_KEY=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8= CURATOR_MASTER_KEY_NEXT=AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA= \
   CURATOR_DATA_DIR=$D ../.venv/bin/python -m daemon.secrets.rotate
   ```

   预期：`{"target_version": 2, "rotated": 2, "remaining": 0, "failed": [], "complete": true}`。再用新主密钥
   （`CURATOR_MASTER_KEY=AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA= CURATOR_MASTER_KEY_VERSION=2`，其余同第 2 步）启动，
   `POST .../vlm-backends/$B/verify` 返回 `ok`：换了主密钥，API Key 照样解得开。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/secrets     # 约 30 秒；假 TOS + 本机 HTTP 替身，不连外网
```

`test_no_secret_leaks.py` 是验收用例：一个场景走遍全部 W8 接口（成功、失败、非法请求），假 TOS 和模型服务替身都会在报错里回显密钥，
最后在所有响应、日志、审计事件和 SQLite 文件里搜埋下的密钥值。
