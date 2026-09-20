# 08 密钥管理与鉴权

## 1. 威胁模型（先说清楚在防谁）

本期是**单实例单租户**：能登录平台的人，就是这套 TOS 凭证和方舟 Key 的合法使用者。
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
- `payload_meta`（地区、endpoint、模型列表）明文存，列表页只读这一列，**永不触发解密**。

### 2.1 主密钥轮换

Chart 提供一个 Job：读旧 key 解密全表 → 用新 key 重新加密 → 原子替换。
本期提供脚本和文档，不做自动轮换。

## 3. 密钥的使用路径

```
任务运行 → Daemon 解密 → 写入子进程的环境变量 → CLI 从 env 读
```

三条纪律：

1. **绝不进 argv**：`ps` 在容器内外都可见。CLI 的所有凭证参数都是「环境变量名」而非值
   （v1 的 `--vlm-api-key-env` 就是这个设计，保留并推广到 TOS 凭证）。
2. **绝不进日志**：日志中间件对 `TOS_SECRET_KEY` / `ARK_API_KEY` / `Authorization` 做脱敏，
   统一打 `***`。异常栈也要过滤（Python 的异常里可能带上下文变量）。
3. **绝不回显**：API 响应里没有任何一个字段会包含密钥本体。编辑时留空 = 不修改。

## 4. 连通性校验

新建/更新密钥时立即校验一次，结果写 `last_verified_at` / `last_verify_error`：

| 类型 | 校验动作 | 失败的典型原因 |
|---|---|---|
| TOS | `HeadBucket` + 一次小对象写探针（写完即删） | AK/SK 错、桶不存在、地区填错、只读权限 |
| 方舟 | 拉模型列表 | Key 失效、网络不通 |
| 自定义端点 | `GET /v1/models`，失败则尝试一次最小 chat 请求 | 端点不通、模型名不对 |

失败**不阻止保存**（用户可能先填后开通权限），但在列表页打红色标记，
且新建任务时选到未校验通过的凭证会有二次确认。

## 5. 鉴权

### 5.1 本期实现

沿用 v1 的 HTTP Basic / htpasswd 中间件（`ui/auth.py`），三条要点一条不改：

1. **裸 ASGI 中间件，覆盖全部路由**，不用 `BaseHTTPMiddleware`
   （后者只经手 `scope["type"] == "http"`，SSE 走的是 http 没问题，但将来加 WS 会漏）。
2. **`/healthz` `/readyz` 豁免**：探针被 401 会让 pod 直接下线。
3. **常数时间比较**（`hmac.compare_digest` / bcrypt）：短路比较会泄漏密码前缀。

两种配置模式按优先级：htpasswd 多用户文件（推荐）> 单用户明文环境变量（兼容存量）。
配了 htpasswd 但文件读不到 = **拒绝所有请求**（fail-closed）。

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

### 5.3 任务与密钥的归属隔离

需求要求「任务密钥需要按用户隔离」。落地：

- `credential.owner_id` + `task.owner_id`，查询一律带 owner 过滤。
- 任务引用凭证时校验**归属一致**：不能引用别人的凭证（本期恒真，代码仍要写）。
- 删除凭证时检查是否被运行中的任务引用，被引用则拒绝（409）。

## 6. 预签名 URL 的安全边界

浏览器直连 TOS 取视频和证据帧（D：用户本来就有该桶访问权限）。仍要守两条：

1. **路径必须落在该任务的交付目录前缀内**，签名前做前缀校验，
   防止构造 `../` 或任意 path 签出整桶的对象。
2. **TTL 默认 30 分钟**，不提供长期链接。链接泄漏的窗口有限。

## 7. 审计

单租户下不做完整审计系统，但记录关键操作到 `event` 表（谁、什么时候、对哪个资源做了什么）：
创建/删除任务、创建/删除/更新密钥、执行裁决、重新导出。
保留 90 天，够回答「这个任务是谁起的」「这条裁决是谁点的」。
