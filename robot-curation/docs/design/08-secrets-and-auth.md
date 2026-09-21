# 08 密钥管理与鉴权

## 1. 威胁模型（先说清楚在防谁）

本期是**单实例单租户**：能登录平台的人，就是这套 TOS 访问密钥和方舟 API Key 的合法使用者。
所以加密防的不是「平台内的越权」，而是：

- PVC 快照、备份文件、误传的镜像层里带出明文密钥；
- 日志、报错、API 响应里意外回显密钥；
- 进程列表（`ps`）里出现密钥。

这三条决定了下面的所有设计，别把它当成一个多租户密钥托管系统来做。

## 2. 存储

```
K8s Secret (curator-master-key)
        │  env: CURATOR_MASTER_KEY (32 bytes, base64)
        ▼
  Daemon 启动时加载到内存 ──▶ AES-256-GCM ──▶ credential.payload_enc (SQLite)
```

- **主密钥只在环境变量里**，不落盘、不进 DB、不进日志。
- 主密钥缺失或长度不对 → **Daemon 拒绝启动**。半配的加密比不加密更危险
  （自以为锁了其实没锁，v1 的鉴权中间件里就是这条纪律，照搬）。
- 每条密钥独立随机 nonce，密文 = `nonce || ciphertext || tag`。
- `payload_meta`（地域、endpoint）明文存，列表页只读这一列，**永不触发解密**。

### 2.1 主密钥轮换

Pod 内的管理命令 `curator-admin rotate-master-key`：读旧 key 解密 → 用新 key 重新加密 → 逐行替换并把该行的
`key_version` +1。每行记着自己是哪一版主密钥加密的，轮换中途断了可以接着做，新旧两版在轮换期间同时可读。
不做成独立的 Job —— SQLite 在 RWO 的数据卷上，另起一个 Pod 挂不上它。步骤（写进 README）：
把新 key 写进 Secret 的 `masterKeyNext` → `kubectl exec` 执行轮换 → 把新 key 挪到 `masterKey`、重启。
本期只提供命令和文档，不做自动轮换。

## 3. 密钥的使用路径

```
任务运行 → Daemon 解密 → 写入子进程的环境变量 → CLI 从 env 读
```

三条纪律：

1. **绝不进 argv**：`ps` 在容器内外都可见。CLI 的所有凭证参数都是「环境变量名」而非值
   （v1 的 `--vlm-api-key-env` 就是这个设计，保留并推广到 TOS 访问密钥）。
2. **绝不进日志**：日志中间件对 `TOS_SECRET_KEY` / `ARK_API_KEY` / `Authorization` 做脱敏，
   统一打 `***`。异常栈也要过滤（Python 的异常里可能带上下文变量）。
3. **绝不回显**：API 响应里没有任何一个字段会包含密钥本体。编辑时留空 = 不修改。

## 4. 连通性校验与方舟参数

新建/更新时立即校验一次，结果写 `last_verified_at` / `last_verify_error`：

| 类型 | 校验动作 | 失败的典型原因 |
|---|---|---|
| TOS 访问密钥 | 只验**身份**：用这对密钥发一次签名请求（`ListBuckets`；没有列桶权限的，对用户填的「测试用存储桶」做 `HeadBucket`）。不在这里验读写权限 | AK/SK 错、地域填错、账号被禁用 |
| 方舟后端 | `GET {endpoint}/models`；不通则用一次最小 chat 请求探活 | Key 失效、网络不通、模型未开通 |
| 自定义端点 | 同上 | 端点不通、模型名不对 |

**身份和权限分开验。** 一对访问密钥本身不绑定任何存储桶：同一对密钥，对源数据桶可能只读，对交付桶可写。
保存时要求它「能写」会把只读的源数据密钥一律判成失败，而那恰恰是最常见的用法。
所以保存时只验身份；「对这个桶能不能读、对那个目录能不能写」是和具体任务绑定的，在任务开始前验（下一段）。
输入和输出各绑各的访问密钥，运行时经两组独立的环境变量传给 CLI（02 篇 §2），跨账号不会串。

**验证不过也能保存，但不能拿去跑任务**（D30）：

- 保存时的校验只决定标记：已验证 / 未验证 / 验证失败（附原因）。用户可能先填、后开通权限，所以不拦保存。
- 任务开始前有三项硬检查，和上面的标记无关，每次都真实执行：输入 TOS 能读、输出 TOS 能写（写探针）、
  VLM 能完成一次最小调用。任何一项不过，任务不允许开始（03 篇 §3）。
  重试、继续运行、重新导出、执行裁决之前，同样先过与之相关的检查。

**模型列表从哪来**（D8）。需求写的是「拿到 Key 以后，pull 出模型」。查证下来，方舟官方的「获取基础模型列表」
（`ListFoundationModels`）**只支持 Access Key 鉴权**，推理用的 API Key 调不了；OpenAI 兼容文档里也没有 `/models`。
需求方的结论：**添加后端时能拉出来就拉，拉不出来就让用户手填**。不为此引入方舟的管理 AK/SK，平台也不维护内置的模型清单。

1. 保存后端时试一次 `GET {endpoint}/models`（v1 既有做法）—— 自托管 vLLM 一定支持；方舟通了就用。
2. 拉不出来不算错，界面直接给手填：Model ID 或推理接入点 ID（`ep-…`）。

无论哪条路来的模型，保存前都用它发一次最小请求，确认账号已开通、接入点可用。

### 4.1 思考强度：方舟的 `reasoning_effort`

需求要求「参考 Doubao 的 API 文档，确定参数/模型列表传的是正确的」。
以下来自方舟《对话(Chat) API》与《深度思考》两篇官方文档（2026-09-20 查阅）：

- 字段是请求体顶层的 **`reasoning_effort`**（Chat API），取值 7 档：
  `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`。
  所有支持该字段的模型都接受全部 7 档，但会按模型**自动映射**到各自的有效档位：

| 模型 | 默认 | 映射 | 有效档位 |
|---|---|---|---|
| doubao-seed-2-1-pro / turbo 系列、doubao-seed-evolving | high | `minimal` 关闭思考；`none` → minimal；`xhigh` / `max` → high | minimal / low / medium / high |
| doubao-seed-2-0 pro / lite / mini 各版本、doubao-seed-1-8、doubao-seed-1-6-251015 | medium | 同上 | minimal / low / medium / high |
| GLM、DeepSeek 等第三方模型 | 各不相同 | 各不相同，见官方表 | 各不相同 |

- 另有一个开关 `thinking.type`（`enabled` / `disabled` / `auto`），用 OpenAI SDK 时经 `extra_body` 传。
  本期只用 `reasoning_effort`：它已经覆盖了「关闭思考」（`minimal`），两个字段同时传还要处理冲突。
- 响应的 `usage` 里有 `completion_tokens_details.reasoning_tokens` 和
  `prompt_tokens_details.cached_tokens`，Token 计量要分开记（01 篇 §2.6）。

落地规则：

1. 库里存 **API 原生取值**或 NULL，不另造枚举（01 篇 §2.2）。
2. **NULL = 请求里不带这个字段**，由模型走自己的默认。这是出厂默认，也是黄金对账的固定配置 ——
   v1 的请求体只有 model / temperature / max_tokens / messages 四项，从不传思考参数。
3. UI 的下拉只列所选模型的**有效档位**，外加「模型默认」。有效档位来自一张很小的思考强度映射表
   （按模型名前缀匹配，就是上面那张表，站点配置可覆盖）；匹配不上的模型，下拉给全 7 档并提示「以服务端映射为准」。
   这张表只管档位怎么显示，不是模型清单，不决定能选哪些模型。
4. 自定义端点不保证认识这个字段（vLLM 因模型而异），默认不传；用户显式选了才传。
5. 提高思考强度会明显拉长单次延迟。v1 各类调用的超时（60s / 120s）和 `max_tokens` 是按默认档位定的，
   用户调高档位时，界面提示同步放宽「各类调用超时」。

## 5. 鉴权

### 5.1 本期实现

沿用 v1 的 HTTP Basic / htpasswd 中间件（`ui/auth.py`），三条要点一条不改：

1. **裸 ASGI 中间件，覆盖全部路由**，不用 `BaseHTTPMiddleware`
   （后者只经手 `scope["type"] == "http"`，SSE 走的是 http 没问题，但将来加 WS 会漏）。
2. **`/healthz`、`/readyz` 豁免**：探针被 401 会让 pod 直接下线。根路径和挂载前缀下的两份都豁免。
3. **常数时间比较**（`hmac.compare_digest` / bcrypt）：短路比较会泄漏密码前缀；
   账号不存在时也照样算一轮哈希，把耗时抹平。

两种配置模式按优先级：htpasswd 多用户文件（推荐）> 单用户明文环境变量（兼容存量）。
配了 htpasswd 但文件读不到、或里面一个可用账号都没有 = **拒绝所有请求**（fail-closed）。
单用户模式要求用户名和密码**两个都配**；只配了一个视为没配 —— 半配的鉴权是最坏的情况。

搬运时保持不变的三样东西，它们是和现网其它组件的接口：

- 环境变量名：`CURATION_UI_HTPASSWD_FILE` / `CURATION_UI_USER` / `CURATION_UI_PASSWORD`
  （可以另加 `CURATOR_*` 别名，旧名必须继续认）；
- htpasswd 的哈希格式：bcrypt（`$2a$` / `$2b$` / `$2y$`）和 apr1，因为这张账号表是和 rerun viewer 的 nginx 共用的；
- realm 字符串 `Robot Data Curation`。同域、同 realm、同一张账号表，
  用户从 viewer 点「质检」跳过来才不用再登录一次。

### 5.2 为 IAM 预留的抽象

```python
class Principal(NamedTuple):
    owner_id: str
    display_name: str
    roles: frozenset[str]        # 本期恒为 {"owner"}

class AuthProvider(Protocol):
    def authenticate(self, scope) -> Principal | None: ...
    def challenge(self) -> Response: ...            # 401 时返回什么
```

- 本期唯一实现：`BasicAuthProvider`。
- 将来的 `VolcIAMProvider` 只需实现同一个接口；业务代码拿到的永远是 `Principal`。
- **所有表都带 `owner_id`，所有 Repository 查询都按它过滤**，从第一天就这么写。
  本期恒为 `default`，但代码路径是真的 —— 等接 IAM 时不需要翻改每一条查询。

### 5.3 归属与多账号

本期是单实例单租户：htpasswd 里可以有多个账号，但**它们共享全部任务、访问密钥和 VLM 后端**，
`owner_id` 恒为 `default`。账号名只用于留痕 —— 谁建的任务、谁点的裁决（`decided_by`）、审计表里的 `actor`。

即便如此，归属这条代码路径从第一天就写真的，接 IAM 时不用翻改：

- `credential.owner_id` + `task.owner_id`，查询一律带 owner 过滤。
- 任务引用访问密钥时校验**归属一致**（本期恒真）。
- 删除访问密钥或 VLM 后端：被非终态任务引用 → 409；只被历史任务引用 → 二次确认后允许，
  任务上的引用置空，报告页提示「访问密钥已删除」并允许重新绑定一个，否则这个任务的报告和视频就读不了了。

## 6. 预签名 URL 的安全边界

浏览器直连 TOS 取视频和证据帧（D16）。仍要守三条：

1. **路径必须落在两个前缀之一**：该任务的交付目录（用输出密钥签），或该任务的输入数据集（用输入密钥签）。
   后者是必须的 —— 被拒的条目只在源数据集里有画面。签名前做归一化和前缀校验，
   防止构造 `../` 或任意 path 签出整个存储桶的对象。新建任务页的 episode 预览同理，前缀是用户填的数据集地址。
2. **TTL 默认 30 分钟**，不提供长期链接。链接泄漏的窗口有限。
3. **用公网端点签名**。Pod 内访问 TOS 走内网端点，拿内网端点签出来的地址浏览器打不开。
   HuggingFace 缓存桶是匿名可读的，不签名，直接给地址。

## 7. 审计

单租户下不做完整审计系统，但记录关键操作到 `event` 表（谁、什么时候、对哪个资源做了什么）：
创建/删除任务、创建/删除/更新密钥、执行裁决、重新导出。
保留 90 天，够回答「这个任务是谁起的」「这条裁决是谁点的」。
